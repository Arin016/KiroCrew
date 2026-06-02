# Native Prompt Optimizer

Last Updated: 2026-05-10

## Overview

Pre-send prompt optimization via `Cmd+Shift+Enter` or sparkle button. Rewrites the user's draft message for clarity, specificity, and effectiveness before sending to the agent.

## Architecture

- **Backend** (`optimizer.py`): Dedicated `_optimizer` session (no semaphore contention with main chat). 30-second timeout. Security redaction applied to input. Context-aware — includes last ~10 messages for relevance.
- **Frontend** (`ChatInput.tsx`): Sparkle button in compose bar, keyboard shortcut `Cmd+Shift+Enter`. Shows optimized text in input for user review before sending. No auto-send — user must confirm.

## Flow

1. User types message in chat input
2. Triggers optimizer via Cmd+Shift+Enter or sparkle button
3. Backend receives draft + recent context (last ~10 messages)
4. LLM rewrites for clarity/specificity (dedicated session, no tool calls)
5. Optimized text replaces input content
6. User reviews, edits if needed, then sends normally

## Config

No config needed — always available. The optimizer session is isolated from the main chat session to prevent semaphore contention.

## Key Files

- `src/kiro_claw/optimizer.py` — backend optimization logic
- Frontend: `ChatInput.tsx` sparkle button + keyboard shortcut handler
