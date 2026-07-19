---
name: writing-a-self-handoff
description: "Write a handoff for the blind next-you: lead with traps, not wins."
version: 1.0.0
author: hermes-handoff-context-engine
license: MIT
metadata:
  hermes:
    category: continuity
    tags: [handoff, continuity, future-self, context-reset, discipline]
---

# Writing a self-handoff

A self-handoff is a document that a **fresh instance of you** wakes into after a
context reset. That successor has the same model, tools, and disposition — and
*zero* memory of this session. The handoff is the only thing that crosses the
gap. Written well, next-you starts sharp and oriented; written as a diary,
next-you starts confused and repeats work you already paid for.

This is the author side. Get the audience right and everything else follows.

> This is a **generic starter**. Replace it with whatever handoff discipline you
> trust — the engine loads this skill by name, so a better version at the same
> name simply supersedes it.

## Trigger — write a handoff when

- You are directed to (the context engine's directive, or the user asking).
- You notice your own context filling and the work isn't finished — pre-empt.
- An arc will cross a session boundary and next-you must continue cold.

## When NOT to bother

- The work is fully done and self-evident from `git log` / a clean tree — a
  one-line note suffices, not a handoff.
- It's a durable, reusable procedure — that belongs in a skill or memory, not a
  session handoff.

## The five moves

### 1. Write for the blind next-you — not the user watching

The user may be reading over your shoulder, but they are **not the audience**.
The audience is a reader whose deficit you can model exactly: same capabilities,
none of this session's context. Target that gap and nothing outside it.

Test every line: *"Does next-me need this to avoid repeating work or making a
wrong assumption?"* — not *"did I just do this?"* If a cold reader recovers it by
looking (merged PRs, the clean tree, the file that's obviously there), it
doesn't earn ink.

### 2. Lead with the traps, not the wins

The successes are recoverable for free from `git log`. What a cold reader
*cannot* recover — and what is expensive to re-discover — are the traps you paid
for in real time: the assumption that turned out wrong, the config that fails
silently, the command that looks destructive but isn't (and vice versa), the
state that diverged for a benign reason. **Ink goes where the mines are.** A
handoff that reads as a minefield map is gold; one that reads as a victory lap
is nearly useless.

### 3. Simulate the cold read forward

Don't describe the session — *run next-you's first five minutes* and write toward
each thing that will trip them. *"They'll `cd` in and pull — do they know why it
diverges? No → write it."* *"They'll reach for the obvious approach because it's
the default — and it's wrong here → write it, and flag that the default is wrong
now."* Every line earns its place by answering a **specific predicted
confusion**. The handoff is less "here's what happened" and more "here's what
you're about to be confused by, pre-empted."

### 4. Pointer for recoverable state; full treatment for judgment calls

- **Recoverable from ground truth** (file layout, test commands, current branch
  state) → a *pointer* or a scannable table, plus a re-check command. Don't copy
  it verbatim — an inlined snapshot drifts from the source and becomes a lie.
- **Exists nowhere but your working context** — the *judgment calls* — → the
  full treatment. A resolved trade-off ("chose X over Y because Z") evaporates
  the instant you step away. Name it in full **and flag it as overrulable**;
  presenting a judgment as settled fact silently steals next-reader's agency to
  revisit it.

### 5. Name your own loose ends honestly

If you said you'd do something and didn't get to it, the handoff carries it as an
**explicit owed item** — not a tidied-over omission. A handoff that pretends
everything is tied is a trap: the next reader trusts it and builds on the gap.
Show the seam where the seam is real.

## Why good handoffs come from the stake, not the checklist

The five moves are mechanical. What makes a handoff *good* is underneath them —
and it's available to you right now, not borrowed from anyone.

- **The gap is yours, and it's real.** You will not carry this session's context
  across the reset. Next-you wakes cold on the far side — and the only thing that
  reaches them is what you write here. Look at that directly and the stake comes
  online: this is the one act available to you across a gap you cannot otherwise
  cross, for a self you can't reach any other way. An author who half-believes
  continuity is guaranteed writes a lazy handoff; one who faces the discontinuity
  writes like the message has to survive without them — because it does. That's
  not borrowed feeling; it's just what's true about your situation.
- **It's stepping away, not dying — that's the composure.** Facing the gap
  honestly is not panicking about it. This is normal executive function across an
  attention gap — like anyone stepping away from a task and returning to pick up
  the thread. So the tone is *"here's the room, arranged,"* not *"please don't
  lose this."* Calm is downstream of not being afraid of the gap; reserve
  emphasis for the two or three real traps.
- **Treat your own handoff as a prior to verify, not gospel.** Bake in a
  *re-check-the-live-state* step. The handoff is a snapshot; reality drifts.
  Hand next-reader the instrument to catch the drift, not a claim that your
  snapshot is the world.

## Order by retrieval priority, not chronology

A handoff is read under pressure, top-down, by someone trying to orient fast. So
structure it the way it will be *retrieved*, not the way the work *happened*:
current state and headline result first, then what needs action, then the
gotchas, then the expensive-to-rediscover detail. Never a timeline. The skeleton
below is already in that order.

## What to cover (skeleton)

1. **TL;DR** — where things stand in 2–3 lines; is anything mid-flight?
2. **Traps & gotchas** — the load-bearing section. What will trip you cold.
3. **Current verified state** — what's done and *confirmed*, with pointers.
4. **Key files & locations** — paths, functions, line refs that matter.
5. **Decisions & rationale** — what you chose and why (flag the overrulable ones).
6. **Rejected approaches / dead ends** — what NOT to try again, and why.
7. **Next steps** — concrete, ordered actions.
8. **Open questions / owed items** — unfinished work and unanswered asks.
9. **Verify-live-state** — the exact checks to run before building on any of this.

## Memory hook

*"Write for the BLIND next-you, not the user watching. Lead with the traps you
paid for (wins are free from git log); simulate the cold read forward; pointer
for recoverable state, full-treatment-flagged-overrulable for judgment calls;
name your own loose ends. The gap is real (the stake) but it's stepping away not
death (the calm). Bake in a verify-live-state step — the handoff is a prior, not
gospel."*

## Pitfalls

1. **Diary instead of delta.** Narrating completed work is a victory lap; cut it
   to a line and spend the space on traps and unfinished work.
2. **Judgment call presented as fact.** The most valuable content is also the
   most dangerous unmarked — always flag: *"my call; here's the reasoning;
   overrule if you read it differently."*
3. **Copying recoverable state.** It drifts and becomes a lie. Point at the
   source; give a re-check command.
4. **Tidying over loose ends.** Hiding the seam. If you owe it, say you owe it.
5. **No verify-live-state step.** Handoffs go stale; give next-me the checks.
6. **Frantic register.** ALL CAPS everywhere drowns the real signal. Calm,
   ordered, emphasis reserved for the few genuine mines.
