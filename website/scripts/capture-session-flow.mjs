/**
 * Screenshot + video harness for the "Session Flow" header in the Context
 * Breakdown side-panel tab (Developer Mode).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server, with every /api/** call answered from fixtures and the /api/ws
 * websocket intercepted by Playwright. No gateway, no dashboard token, no
 * subagents actually spawned — the client code under test is unmodified, only
 * the network is stubbed.
 *
 * SessionFlow renders ONLY when the slot has BOTH a context-trace AND >=1
 * sub-agent in the live Redux subagents map (see SessionFlow.tsx). So the scene
 * is built in two moves:
 *   1. GET /api/telemetry/context-trace?slot=chat-1 -> a realistic
 *      babysit-to-green ContextTrace (drives the main node's mini-bar + the
 *      per-turn rows below).
 *   2. After load, push three `subagent_snapshot` frames (distinct `started`,
 *      so SessionFlow's start-time sort yields spawn order) followed by three
 *      `subagent_done` frames — two completed, one failed — exactly as the
 *      gateway streams them into the live socket. The reducer folds them into
 *      chat.subagents for the active slot, which selectSlotSubagents feeds to
 *      SessionFlow.
 *
 * The Context tab is a Developer-Mode-only side-panel view opened from the +
 * menu, so it is reached by pre-seeding localStorage the way the panel persists
 * itself: mc-dev-mode=1, mc-active-slot-chat=<slot>, mc-activity-open:<slot>,
 * and an mc-panel-tabs:<slot> bucket whose active tab is the `context` view.
 *
 * Usage: node scripts/capture-session-flow.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

// Output dir: first CLI arg, else $KIROCREW_SCRATCH if set, else the OS temp
// dir — never a hardcoded developer-specific path.
const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || tmpdir(), 'session-flow-cap')
const SLOT = 'chat-1'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Babysit PR #5966 to green',
  running: true,
  last_message: 'Spawned 3 review agents, waiting for results…',
  messages: 24,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 24,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 900, content: 'Drive PR #5966 to green and review it.' },
    { role: 'assistant', ts: Date.now() / 1000 - 890, content: 'Spawned 3 review agents, waiting for results…' },
  ],
}

// ── The context trace ──────────────────────────────────────────────────────
// Matches the ContextTrace interface in ContextBreakdownPanel.tsx exactly.
// Block sizes are in CHARS; the requested composition is expressed as a share
// of the injected total and scaled to a realistic ~92k-char window.
const INJECTED = 92000
const SHARE = {
  loaded_skill: 0.48,
  memory: 0.14,
  agent_instructions: 0.09,
  lessons: 0.08,
  skill_index: 0.07,
  semantic_memory: 0.05,
  your_message: 0.03,
  // remainder (~0.06) spread over a couple of smaller repeating blocks so the
  // bar has a believable tail rather than a single dominant slab.
  episodic_memory: 0.035,
  critical_rules: 0.015,
  surface: 0.006,       // an every_turn member -> merged into the every_turn bucket
  request_header: 0.004, // every_turn member
}
const chars = frac => Math.round(INJECTED * frac)

const totals = Object.fromEntries(Object.entries(SHARE).map(([k, f]) => [k, chars(f)]))
const userChars = totals.your_message
// injected_chars is the sum of everything Kiro Crew injected (incl. the user's
// own text, which the panel then breaks back out).
const injectedChars = Object.values(totals).reduce((a, b) => a + b, 0)

/** Build one turn's block map as a fraction of that turn's own size, using the
 *  same relative composition as the whole-session totals. */
function turnBlocks(turnChars) {
  const out = {}
  for (const [k, f] of Object.entries(SHARE)) out[k] = Math.round(turnChars * f)
  return out
}

const WINDOW = 1_000_000
// A climbing context_used across the turns — the window filling turn by turn.
const trace = {
  slot: SLOT,
  turns: [
    // Session-start turn: the big one (loads all the skills/memory).
    { ts: '2026-08-26T15:30:00Z', phase: 'session_start', blocks: turnBlocks(58000), total_chars: 58000, context_used: 61000, context_window: WINDOW, model: 'claude-opus-5', credits: 2.4, duration_ms: 41000 },
    { ts: '2026-08-26T15:36:00Z', phase: 'per_turn', blocks: turnBlocks(9200), total_chars: 9200, context_used: 88000, context_window: WINDOW, model: 'claude-opus-5', credits: 1.1, duration_ms: 18000 },
    { ts: '2026-08-26T15:44:00Z', phase: 'per_turn', blocks: turnBlocks(6400), total_chars: 6400, context_used: 132000, context_window: WINDOW, model: 'claude-opus-5', credits: 0.9, duration_ms: 15500 },
    { ts: '2026-08-26T15:52:00Z', phase: 'per_turn', blocks: turnBlocks(11800), total_chars: 11800, context_used: 205000, context_window: WINDOW, model: 'claude-opus-5', credits: 1.6, duration_ms: 96000 },
    { ts: '2026-08-26T16:05:00Z', phase: 'per_turn', blocks: turnBlocks(4200), total_chars: 4200, context_used: 268000, context_window: WINDOW, model: 'claude-opus-5', credits: 0.7, duration_ms: 12000 },
  ],
  totals,
  injected_chars: injectedChars,
  user_chars: userChars,
  // kiro-cli's own prompt + tool catalogue Kiro Crew did NOT inject — surfaced
  // hatched + labelled as an estimate. Non-zero as required.
  estimated_other_chars: 148000,
  peak_context_used: 268000,
  context_window: WINDOW,
  window_days: 7,
}

// ── The three sub-agents (spawn order via distinct `started`) ───────────────
const now = () => Date.now() / 1000
const SUBS = [
  {
    id: 'sa-gpt',
    agent: 'gpt-review',
    model: 'gpt-5.6-sol',
    task: 'Mirror the GPT review gate over the working diff; report Critical/High findings only.',
    started: now() - 300,          // earliest -> first in the tree
    tool_count: 12,
    elapsed: 84000,
    outcome: 'completed',
  },
  {
    id: 'sa-opus',
    agent: 'opus-review',
    model: 'claude-opus-5',
    task: 'Independent Opus review pass over the same diff; deep design + correctness read.',
    started: now() - 250,
    tool_count: 9,
    elapsed: 96000,
    outcome: 'completed',
  },
  {
    id: 'sa-verify',
    agent: 'verify-repro',
    model: 'claude-opus-5',
    task: 'Reproduce the reported crash on the fix branch and confirm the pin test fails on base.',
    started: now() - 200,          // latest -> last in the tree
    tool_count: 4,
    elapsed: 14200,
    outcome: 'failed',
  },
]

const FIXED_API = makeFixedApi(PROJECT)

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1280, height: 1000 },
    deviceScaleFactor: 2,          // dense 10–12px node type renders soft at 1x
    recordVideo: { dir: OUT, size: { width: 1280, height: 1000 } },
  })
  const page = await context.newPage()

  // Keep the socket handle so we can push server->client frames after load.
  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  const scene = { theme: 'dark' }

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    // The Session Flow prerequisite: the per-session context trace.
    if (path === '/api/telemetry/context-trace') return json(route, trace)
    return handleBootRoute(route, path, { project: PROJECT, theme: scene.theme, fixedApi: FIXED_API })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  // Cold-load with Developer Mode on and the Context panel tab already open on
  // this slot (seeded the way the panel persists itself), so SidePanel mounts
  // ContextBreakdownTab with view==='context'.
  await page.addInitScript(([t, slot]) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-dev-mode', '1')                 // gates the Context view
    localStorage.setItem('mc-active-slot-chat', slot)        // ChatPage restores this slot
    localStorage.setItem('mc-activity-open:' + slot, 'true') // side panel open
    localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
      tabs: [{ id: 'context', kind: 'context', title: 'Context' }],
      activeId: 'context',
    }))
  }, [scene.theme, SLOT])

  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  const send = async (type, data, settle = 250) => {
    if (!wsServer) throw new Error('websocket route never bound')
    wsServer.send(JSON.stringify({ type, data }))
    await page.waitForTimeout(settle)
  }

  // Spawn the tree the way the gateway streams it: a running snapshot per agent
  // (in spawn order), then the terminal done frame for each.
  for (const s of SUBS) {
    await send('subagent_snapshot', {
      id: s.id, slot: SLOT, task: s.task, agent: s.agent, model: s.model,
      streaming: '', last_tool: '', started: s.started, tool_count: s.tool_count,
    }, 500)
  }
  // A short beat where all three are still "running" reads well on the video.
  await page.waitForTimeout(1200)
  for (const s of SUBS) {
    await send('subagent_done', {
      slot: SLOT, id: s.id, elapsed: s.elapsed, outcome: s.outcome,
      task: s.task, agent: s.agent, model: s.model,
    }, 600)
  }
  await page.waitForTimeout(1200)

  // ── Stale-bundle / real-render guard: assert on the RENDERED page ──────────
  const body = await page.locator('body').innerText()
  // The header + pills carry an `uppercase` CSS transform, which innerText
  // reflects ("SESSION FLOW"), so those two are matched case-insensitively;
  // the agent names and "This session" have no transform and match verbatim.
  const lower = body.toLowerCase()
  const need = ['Session Flow', 'This session', 'gpt-review', 'opus-review', 'verify-repro']
  const missing = need.filter(s => !lower.includes(s.toLowerCase()))
  // At least one status pill (running/done/ended) must be on screen.
  const pills =
    (await page.getByText('done', { exact: true }).count()) +
    (await page.getByText('ended', { exact: true }).count()) +
    (await page.getByText('running', { exact: true }).count())
  console.log('assert: missing=', JSON.stringify(missing), 'pills=', pills)
  if (missing.length) throw new Error(`Session Flow render incomplete — missing: ${missing.join(', ')} (stale bundle?)`)
  if (pills < 1) throw new Error('No status pill rendered on the sub-agent nodes (stale bundle?)')

  // Bring the Session Flow header into frame and screenshot the whole app.
  await page.getByText('Session Flow', { exact: true }).scrollIntoViewIfNeeded()
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/session-flow.png` })
  console.log('wrote', `${OUT}/session-flow.png`)

  // A last few seconds on the settled tree so the recorded clip lands 5–12s.
  await page.waitForTimeout(2500)

  // Finalize the video: its path resolves only after the context closes.
  await context.close()
  const webm = await page.video().path()
  await browser.close()
  srv.close()
  console.log('VIDEO_WEBM', webm)
}

main().catch(err => { console.error(err); process.exit(1) })
