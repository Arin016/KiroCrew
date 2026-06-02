#!/usr/bin/env node
// Full secretary poll: discover active channels → fetch messages → fetch thread replies.
// Single Midway auth session, single Node process. Called by SecretaryPoller.
// Input: JSON on stdin with { checkpoints: { channel_id: "oldest_ts", ... }, watched_channels: [...] }
// Output: JSON with { channels: { channel_id: [message, ...] }, errors: [...] }

import { readFileSync, writeFileSync } from 'fs';
import { join } from 'path';

import { resolveSlackMcpPath } from './resolve_slack_mcp.mjs';

const slackMcpPath = resolveSlackMcpPath();
if (!slackMcpPath) { console.error('slack-mcp not configured'); process.exit(1); }
const { DependencyContainer } = await import(join(slackMcpPath, '..', 'dist', 'composition-root.js'));

// Suppress Slack MCP init logs that pollute stdout
const _log = console.log;
const _info = console.info;
const _warn = console.warn;
console.log = () => {};
console.info = () => {};
console.warn = () => {};

const input = JSON.parse(readFileSync(process.env.SECRETARY_INPUT || '/dev/stdin', 'utf8'));
const outputPath = process.env.SECRETARY_OUTPUT;
const checkpoints = input.checkpoints || {};
const watchedChannels = input.watched_channels || [];

const container = new DependencyContainer();
await container.initialize();
const api = container.dependencies.apiClient;

// Restore console for our output
console.log = _log;
console.info = _info;
console.warn = _warn;

const errors = [];
const result = {};

// Discover active channels — DMs always, channels only with mentions
let activeChannels = [];
try {
  const counts = await api.post('client.counts', {});
  for (const c of (counts.ims || [])) {
    if (parseFloat(c.latest || '0') > parseFloat(checkpoints[c.id] || '0')) activeChannels.push(c.id);
  }
  for (const c of (counts.mpims || [])) {
    if (parseFloat(c.latest || '0') > parseFloat(checkpoints[c.id] || '0')) activeChannels.push(c.id);
  }
  // Channels: only if mentioned (skip noisy unread channels)
  for (const c of (counts.channels || [])) {
    if (c.mention_count > 0) activeChannels.push(c.id);
  }
} catch (e) {
  errors.push(`client.counts failed: ${e.message}`);
}

// 2. Merge with explicit watched channels
const allChannels = [...new Set([...activeChannels, ...watchedChannels])];

// 3. Fetch messages + thread replies for each channel
for (const channelId of allChannels) {
  const oldest = checkpoints[channelId] || '0';
  const isDM = channelId.startsWith('D');
  try {
    // For DMs, look back 24h before checkpoint to catch thread parents (not full history)
    const DM_LOOKBACK_SECONDS = 86400;
    const dmOldest = oldest !== '0'
      ? String(parseFloat(oldest) - DM_LOOKBACK_SECONDS)
      : '0';
    const histOldest = isDM ? dmOldest : oldest;
    const hist = await api.post('conversations.history', {
      channel: channelId, oldest: histOldest, limit: isDM ? 100 : 50,
    });
    const allMessages = hist.messages || [];

    // Fetch thread replies in parallel (concurrency cap = 5)
    const threadReplies = [];
    const threadedMsgs = allMessages.filter(m => m.reply_count > 0);
    const THREAD_CONCURRENCY = 5;
    for (let i = 0; i < threadedMsgs.length; i += THREAD_CONCURRENCY) {
      const batch = threadedMsgs.slice(i, i + THREAD_CONCURRENCY);
      const results = await Promise.all(batch.map(async (msg) => {
        try {
          const thread = await api.post('conversations.replies', {
            channel: channelId, ts: msg.ts, limit: 50,
          });
          return (thread.messages || []).filter(
            r => r.ts !== msg.ts && parseFloat(r.ts) > parseFloat(oldest)
          );
        } catch (e) {
          errors.push(`replies ${channelId}:${msg.ts}: ${e.message}`);
          return [];
        }
      }));
      threadReplies.push(...results.flat());
    }

    // For non-DMs, filter messages to only those newer than checkpoint
    // (DMs used wider window just to find thread parents)
    const newMessages = allMessages.filter(m => parseFloat(m.ts) > parseFloat(oldest));
    // Deduplicate: thread replies may already appear in channel history
    const seen = new Set(newMessages.map(m => m.ts));
    const uniqueReplies = threadReplies.filter(r => !seen.has(r.ts));
    result[channelId] = [...newMessages, ...uniqueReplies];
  } catch (e) {
    errors.push(`history ${channelId}: ${e.message}`);
  }
}

// Write result to output file or stdout
const output = JSON.stringify({ channels: result, errors });
if (outputPath) {
  writeFileSync(outputPath, output);
} else {
  process.stdout.write(output + '\n');
}
process.exit(0);
