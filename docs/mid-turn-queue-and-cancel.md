# Mid-Turn Queue & Cancel (Telegram)

## Problem

When a message arrives while the previous turn is still generating, the old
behavior posted a fresh `⏳ Queued …` bubble for every message and drained each
queued message as its own separate turn. Rapid follow-ups produced a stack of
disconnected "Queued" bubbles with confusing timing, and there was no way to
stop a long-running turn.

## Model

A message that arrives mid-turn is handled one of two ways, selected by
`messaging.queue_mode`:

- **`steer` (default)** — folded into the running turn so the model incorporates
  it immediately; the steered continuation is delivered as its own message that
  **replies to the user's steer message** (native threading = a clean cause→effect
  link, no extra receipt bubble).
- **`queue`** — held and answered after the current turn, surfaced through a
  persistent collapsing **`⏳ Queued`** receipt; all held messages then
  **collapse into one combined turn**.

Either way, a hard **`/stop`** aborts the running turn and clears everything.

### 1. Steer (default): fold into the running turn

With `queue_mode="steer"`, a mid-turn message is injected into the in-flight
turn via kiro-cli's `_session/steer`. kiro-cli folds it in at its next
**generation boundary** (a tool-call edge on agentic turns — seconds; the
end-of-stream on a single long text turn — later), signalled by the
`steering_consumed` / `AgentExecutionSteeringInjected` notification.

At that boundary the Telegram renderer **seals the pre-steer output** as its own
message and opens a **fresh message for the steered continuation that replies to
the user's steer message**:

```
[reply ▸ "stop, only reply BANANA"]
BANANA
```

The native reply link is the record of what was folded in and where it took
effect — no separate receipt bubble is posted. kiro-cli also emits an inline
`[STEERING steer-<id>: …]` ack marker in the text stream (the dashboard renders
it as a chip); Telegram **strips** it, since the reply link already conveys it.

Steer is kiro-cli only (claude-agent-acp has no `_session/steer`); when
unsupported the message falls back to the queue path below.

### 2. Queue: persistent collapsing receipt

While a turn is in flight, arriving messages are queued and surfaced through a
**single** receipt message that grows in place (edited, never re-posted):

```
⏳ Queued (2): "what time is it" · "and the weather?"
```

The receipt is a durable record of what the user asked and how it was routed —
it is **never deleted**, so the user can always see which of their messages were
queued.

### 3. Collapse delivery (queue mode)

When the in-flight turn finishes, every message queued during it is **collapsed
into one combined turn** (order preserved, blank-line joined) and answered
together, rather than replayed as N separate turns. The receipt flips to a
durable record state, and the combined answer streams as its own message below:

```
▶️ Now answering (2): "what time is it" · "and the weather?"
```

Messages that arrive during the combined turn open a fresh receipt and drain
after the next turn.

### 4. Hard cancel — `/stop`

`/stop` (alias `/cancel`) aborts the currently-running turn via the ACP
cooperative cancel (`provider.cancel()` → `session/cancel`), **and** drops the
pending queue and finalizes the receipt to `🛑 Cancelled`. It clears everything.

Cancel is cooperative on a shared runtime (it cannot force-kill a co-tenant
process); the turn stops at the next safe point.

## Scope

- **Telegram only.** WeCom replies are bound to the inbound request (no
  proactive send, no editable receipt, no deferred fresh turn), so its mid-turn
  behavior is unchanged here.
- **Steer wiring lives in the shared ACP client.** `AcpClient.steer` /
  `AcpProvider.steer` and the `steer_consumed` signal are shared across channels,
  but the reply-threaded steer render (and the queued receipt) are Telegram-only;
  other channels ignore the signal or render it their own way (e.g. the dashboard
  chip).

## Config

`messaging.queue_mode` selects the mode and **defaults to `steer`**. Set it to
`queue` to hold-and-collapse instead of folding in. No new configuration is
introduced.
