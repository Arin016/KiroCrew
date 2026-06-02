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

console.log(JSON.stringify(result));
process.exit(0);
