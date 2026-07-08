#!/usr/bin/env node
// Returns channels with recent activity via Slack's client.counts API.
// Uses slack-mcp's auth infrastructure (Midway SAML → xoxc token).
// Output: JSON with {channels: [{id, mention_count}], dms: [...], mpims: [...]}
// Note: returns ALL recently-active channels, not just unread ones.
// The secretary's own last_read_ts handles deduplication.

import { join } from 'path';

import { resolveSlackMcpPath } from './resolve_slack_mcp.mjs';

const slackMcpPath = resolveSlackMcpPath();
if (!slackMcpPath) { console.error('slack-mcp not configured'); process.exit(1); }
const { DependencyContainer } = await import(join(slackMcpPath, '..', 'dist', 'composition-root.js'));

const _log = console.log;
const _info = console.info;
const _warn = console.warn;
console.log = () => {}; console.info = () => {}; console.warn = () => {};

const container = new DependencyContainer();
await container.initialize();
const api = container.dependencies.apiClient;

console.log = _log; console.info = _info; console.warn = _warn;

try {
  const r = await api.post('client.counts', {});

  // DMs/MPIMs: all with any activity (secretary's last_read_ts handles dedup).
  // Channels: only with @mentions (too noisy otherwise — 52+ unread channels).
  const result = {
    channels: (r.channels || []).filter(c => c.mention_count > 0).map(c => ({
      id: c.id,
      mention_count: c.mention_count || 0,
    })),
    dms: (r.ims || []).filter(c => c.latest).map(c => ({
      id: c.id,
      latest: c.latest || '0',
    })),
    mpims: (r.mpims || []).filter(c => c.latest).map(c => ({
      id: c.id,
      latest: c.latest || '0',
    })),
  };

  // stdout is async when piped; exiting before it flushes makes process.exit()
  // truncate the JSON mid-string at the pipe buffer boundary (~512B). Write
  // first and exit only from the completion callback. Guard against EPIPE
  // (caller closed the pipe early): surface it on stderr so the failure
  // isn't invisible, then exit non-zero so the caller treats output as absent.
  process.stdout.on('error', (err) => {
    process.stderr.write(`slack_unreads: stdout write failed: ${err.message}\n`);
    process.exit(1);
  });
  process.stdout.write(JSON.stringify(result) + '\n', () => process.exit(0));
} catch (err) {
  process.stderr.write(`slack_unreads: API call failed: ${err.message}\n`);
  process.exit(1);
}
