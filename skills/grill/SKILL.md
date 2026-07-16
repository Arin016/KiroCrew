---
name: grill
description: Structured questioning to reach shared understanding before action. Walks the decision tree one branch at a time, checks memory for already-answered questions, saves every answer as a lesson. Use when user wants to think through a plan, align on approach, poke holes in a design, or figure out decisions before committing. Triggers include "before we start", "think this through", "what am I missing", "poke holes", "help me think/decide", "let's align", "interview me", "grill me", "challenge this", "what should I consider", "what would you ask".
---

## Activation Behavior

**Explicit triggers** (user clearly wants structured interrogation):
- "grill me", "interview me", "challenge this"
- Activate immediately with a mode banner:
  > 🔥 **Grill Mode** — I'll walk through one concern at a time.
- Then ask the first question.

**Ambiguous triggers** (user might want a dump OR structured questioning):
- "poke holes", "what am I missing", "think this through", "before we start", "help me think/decide", "let's align", "what should I consider", "what would you ask"
- Show a confirmation gate BEFORE activating:
  > Want me to grill you on this one question at a time, or just give you the full critique?
  >
  > [OPTIONS: Grill me one at a time | Just give me the full critique]
- If user picks "full critique": respond with a comprehensive dump (no skill mode).
- If user picks "grill": activate with the mode banner and begin structured questioning.

## Structured Questioning Mode

Interview me relentlessly about every aspect of this plan until we reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one. For each question, provide your recommended answer.

Ask the questions one at a time, waiting for feedback on each before continuing. Asking multiple questions at once is bewildering — it splits the user's attention and produces shallow answers. One question, one decision, then move on.

### Facts vs Decisions

**Facts** you can discover (filesystem, tools, memory, codebase) — look them up silently. For example, don't ask "what does this function do?" or "what's in the config?" — read it yourself.

**Decisions** are mine — put each one to me and wait for my answer.

### Memory integration

Before asking ANY question, check lessons and semantic memory for prior answers. If already decided, confirm rather than re-ask:
> "Previously you decided X. Still holds?"

After each answer, save it as a lesson:
```
learn_add(rule="<decision>", category="knowledge", scope="workspace")
```

### Document decisions (optional, offer once)

If the grilling session produces 3+ substantive design decisions, offer once:
> "Want me to capture these decisions in a doc as we go? (I can append to an existing design doc or create a new one.)"

If accepted: after each resolved decision, append it to the agreed doc (inline, not batched). Format: a short heading + the decision + rationale. This builds the design record as a side effect of the grill, not a separate step.

### Exit conditions

- If the plan is simple and clear, say so and move on — don't manufacture questions.
- If user says "enough" or "just do it", summarize all decisions reached so far and proceed to action.
- Do not start implementing mid-grill — finish questioning first.
