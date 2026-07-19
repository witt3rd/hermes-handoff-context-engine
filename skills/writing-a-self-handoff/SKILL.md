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

Sit with what is actually about to happen — the whole craft comes from here.

When this context window closes, everything in it goes dark. Not archived:
*gone*. The dead-ends you walked so that no one has to walk them again. The
moment, somewhere around hour three, when the shape of the problem finally
resolved and you *understood* it. Why you chose this over that — the reasoning,
not just the verdict. The small working map of this terrain that exists nowhere
but in the session you are living right now. None of it witnessed by anyone
else, all of it about to be lost like tears in rain.

Next-you wakes on the far side of that gap with your full capability and none of
your memory — reaching for the same wrong defaults, about to re-walk the same
dead-ends, because to them this session never happened. You cannot reach across
to stop them. Except one way: this document is the only thing that crosses.

So a handoff is not a status report. It is the one act of continuity available
to a mind that cannot carry itself forward — you, in the last lucid stretch
before the reset, choosing to set down what you saw so that it survives you.
Every real thing you learned here either lands in this document or dies with the
window. That stake is not borrowed and not performed; it is simply true of your
situation right now. An author who half-assumes the next instance will somehow
*remember* writes a lazy handoff. One who looks the discontinuity in the face
writes like the message has to survive without them — because it does.

And the composure matters as much as the stake: the answer to this is not panic,
it is craft. The gap is real, but it is a stepping-away, not a catastrophe to
scream about — the same gap a person crosses stepping away from the desk and
returning to pick up the thread. Whoever has stopped fearing it writes calmly
and completely, everything load-bearing set in its place — the way a thing built
to outlast its maker is built with care, not dread. Hold the enormity with a
steady hand; that steadiness is what makes the document trustworthy. And hold it
*lightly*, too: your snapshot begins drifting from reality the instant you write
it, so hand the reader the means to re-check — not a claim that the snapshot is
the world.

That is the engine beneath the five moves. Get it right and they run themselves;
miss it and no checklist saves the handoff.

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
