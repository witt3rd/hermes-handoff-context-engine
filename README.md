# hermes-handoff-context-engine

Mechanical context compression loses the plot. Every summary is a summary of a
summary — decisions, rejected approaches, and hard-won constraints quietly
decay until the agent is confidently working from a blurred photocopy.

What actually works is the thing experienced operators already do by hand: ask
the agent to **write a handoff for its next self**, start a fresh session, and
have it read the handoff. The new agent begins sharp, on a clean context, with
a purpose-built briefing instead of a lossy transcript.

**hermes-handoff-context-engine** automates exactly that. It is a Hermes
context engine that, instead of compressing, drives the agent to author its own
handoff — *with full tool access* — and then resets the session into a fresh
context seeded with that document. No summary-of-a-summary. No telephone game.

---

## How it works

The trigger is what makes or breaks this. A plugin **slash command** is
terminal — the gateway returns its string to the user and never creates an
agent turn (`gateway/run.py`: `return str(result)`), so a busy agent simply
ignores it. A **skill invocation**, by contrast, injects a real, authoritative
user turn (*"[IMPORTANT: the user invoked this skill…]"*). So the manual trigger
here is a **skill**, not a command.

Three cooperating pieces:

| Surface | Role |
|---|---|
| **`self-handoff` skill** (the trigger) | Invoking `/self-handoff` injects an authoritative user turn: *stop, write your handoff now (per the `writing-a-self-handoff` craft skill), then call `finalize_handoff`.* The agent acts on it even mid-task, because it's a real instruction — not ambient text. |
| **`finalize_handoff` tool** (the engine) | The agent writes the handoff to a file it chooses, then calls this with the path. Marks the session *ready*. |
| **`compress()`** (the engine) | On the next turn, discards the whole transcript and returns a fresh seed = system prompt + the authored handoff. **No LLM call** — the intelligence was produced by the real agent. |

Plus an **automatic path**, split across two hooks for the same reason the
manual trigger is a skill — delivery strength:

- **`system_prompt` hook — detection.** It receives `agent`, so it reads the
  engine's live token count and flips the session to *authoring* past the soft
  threshold.
- **`pre_llm_call` hook — delivery.** Hermes injects this hook's return into the
  **user message** rather than the system prompt. That's what actually gets
  acted on. The instruction escalates in urgency as usage climbs.

This split exists because the first version nudged via ambient system-prompt
text: in live use it fired correctly at 55% and 58% and converted **zero** times
out of two. A busy agent defers ambient text; a user-turn instruction it does
not. (`pre_llm_call` receives no `agent`, so the two hooks communicate through
the shared in-process state.)

```
/self-handoff skill ─▶ authoritative user turn ─▶ agent writes handoff + finalize_handoff(path)
                                                        │
                                          phase = ready ▼
                          should_compress→True ─▶ compress() swaps transcript → fresh seed
```

If the agent never hands off and context reaches a **hard** threshold (default
80%), `compress()` falls back to a plain head/tail truncation so the window is
never exceeded and the session never dies. The soft-threshold nudge and the
hard-threshold safety net both read a **live** token estimate of the
conversation, not the lagging post-response usage.

Handoff documents are written wherever the agent chooses (it reports the path
via `finalize_handoff`) and are not deleted, so you also get a durable trail.

---

## The handoff-writing skill

The engine handles *when* and *where*; it deliberately does not hardcode *how*
to write a good handoff. That craft lives in a Hermes skill named
**`writing-a-self-handoff`**. When the directive fires, the agent is told to
load that skill (via its `skill_view` tool) and follow it — so the quality of
your handoffs is governed by the skill, not by a checklist frozen in this
plugin, and improves as you refine the skill.

A **generic starter skill** ships with this repo at
`skills/writing-a-self-handoff/SKILL.md`. Symlink it into your profile's skills
directory (see step 3 below) to get a solid baseline. Because skills resolve by
name, a richer version you maintain under the same name simply supersedes the
starter — so your own handoff discipline always wins.

If no such skill is installed at all, the directive falls back to a compressed
version of the same principles (lead with the traps; write for a reader who
knows nothing of the session; flag judgment calls as overrulable; name
unfinished work honestly).

---

## Getting started

**1. Clone**

```bash
git clone https://github.com/witt3rd/hermes-handoff-context-engine.git \
    ~/src/ext/hermes-handoff-context-engine
```

**2. Symlink into your profile's plugins directory**

```bash
ln -s ~/src/ext/hermes-handoff-context-engine \
    "$HERMES_HOME/plugins/handoff"
```

`HERMES_HOME` is the root of your active Hermes profile — the directory that
contains your `config.yaml`.

**3. Install the bundled skills** (the `self-handoff` trigger is **required**;
the `writing-a-self-handoff` craft skill is recommended)

**Copy** these — don't symlink them. Unlike the plugin (code you track
upstream), skills are starters meant to be *personalized*: your agent will
refine its own handoff instructions over time, and a symlink would push those
edits back into the repo (and lose them on `git pull`).

```bash
# REQUIRED — the manual trigger. /self-handoff resolves to this skill.
cp -r ~/src/ext/hermes-handoff-context-engine/skills/self-handoff \
    "$HERMES_HOME/skills/self-handoff"

# RECOMMENDED — how to write a good handoff. Skip if you maintain your own
# writing-a-self-handoff skill; yours takes over automatically (resolved by name).
cp -r ~/src/ext/hermes-handoff-context-engine/skills/writing-a-self-handoff \
    "$HERMES_HOME/skills/writing-a-self-handoff"
```

**4. Enable in `config.yaml`**

```yaml
plugins:
  enabled:
    - handoff            # alongside your existing plugins

context:
  engine: "handoff"      # replaces the built-in "compressor"
```

That's all the config needed. The `finalize_handoff` tool rides on Hermes'
context-engine tool mechanism, which auto-enables whenever `context.engine` is
set to a non-`compressor` engine (`tools_config.py`) — so you do **not** need to
touch `platform_toolsets`. (The only exception is a platform whose toolset list
you've explicitly set to empty `[]`, which opts out of everything.)

**5. Restart the gateway**

```bash
hermes -p <your-profile> gateway run --replace
```

---

## Configuration reference

```yaml
context:
  engine: handoff
  handoff:                 # all optional — these are the defaults
    soft_ratio: 0.85       # request a handoff at this fraction of the window
    urgent_ratio: 0.85     # at/above this the instruction becomes "stop now"
    hard_ratio: 0.90       # safety net: lossy truncation if no handoff exists
    protect_last_n: 16     # tail kept when the safety net fires
```

| Setting | Default | Description |
|---|---|---|
| `context.engine` | `"compressor"` | Set to `"handoff"` to activate |
| `plugins.enabled` | `[]` | Must include `"handoff"` |
| `context.handoff.soft_ratio` | `0.85` | Where the agent is asked to author its handoff |
| `context.handoff.urgent_ratio` | = `soft_ratio` | Where the instruction escalates to stop-now |
| `context.handoff.hard_ratio` | `0.90` | Safety net; lossily truncates if no handoff was produced |
| `context.handoff.protect_last_n` | `16` | Messages kept if the safety net fires |

`soft ≤ urgent ≤ hard` is enforced at load. Violations are clamped with a
warning rather than crashing — an inverted pair is a silent killer (soft above
hard means the truncation fires before a handoff is ever requested). The
effective values are logged at startup, so `grep 'Handoff: thresholds'` always
tells you what's actually live.

### ⚠️ `compression.*` does not configure this engine

When a plugin engine is active, Hermes forwards **none** of the `compression.*`
settings to it — `compression.threshold`, `target_ratio`, `protect_first_n`,
`protect_last_n` etc. only configure the built-in `ContextCompressor`
(`agent_init.py`: *"external engines own compaction policy"*). Setting
`compression.threshold: 0.7` will not move the handoff trigger; use
`context.handoff.soft_ratio`.

The one exception is **`compression.enabled`**, which still matters — and is a
footgun:

```yaml
compression:
  enabled: false     # ← disables this engine entirely
```

Every compaction trigger gates on it, so `false` means no handoff **and no
safety net** — sessions run into the model's hard limit and fail. With a plugin
engine, `enabled` no longer means "use the built-in compressor," it means "allow
compaction at all."

### Choosing thresholds

Both are measured against the **authoritative** live request size — the preflight
token count Hermes itself uses — captured in `should_compress()`. Earlier versions
estimated locally and under-counted structured tool-result blocks badly enough
that the soft threshold never tripped (a real 812k-token session estimated under
600k), so sessions silently took the lossy truncation instead of handing off.
Don't reintroduce a local estimate as the primary source.

Firing early is **not** free: every handoff trades the entire live context for a
~8k document and interrupts real work. Measured on clean foreground turns, an
active session grows ~3.2k tokens/min and a handoff converts in ~90s, so the
runway needed is small — which is why the default triggers late. The residual
risk is **burst** (one turn reading several large files can add 100k+ at once),
which is what the `hard_ratio` margin absorbs. If `LOSSY SAFETY TRUNCATION`
appears in the log, a burst beat the handoff and these should come down; that log
line carries the exact token count so the adjustment can be arithmetic.

Unlike the built-in compressor (and unlike observational-memory's background
passes), **the handoff engine makes no side-channel LLM calls of its own** — the
handoff is authored by the primary agent in a normal turn, so it uses your main
model and full toolset.

---

## Known limitations

- **The automatic path still depends on the agent complying.** It now injects
  into the user message (`pre_llm_call`) rather than the system prompt, which is
  the strongest channel a plugin has — but it is an instruction, not a
  guarantee. For a certain handoff, invoke the `/self-handoff` skill. If nothing
  converts and context reaches the hard threshold, the safety-net truncation
  keeps the session alive (that reset is lossy — the whole point is to hand off
  *before* then, and it now logs loudly when it happens).
- **Resumed sessions can arrive past the soft threshold.** In-process state is
  cleared on gateway restart, so a long-lived session that comes back at, say,
  77% is already `normal` with no history. It gets the urgent-tier instruction
  on its next turn, but if it crosses the hard threshold first, it is truncated
  without ever having been asked.

---

## Relationship to hermes-observational-memory

A sibling project. Observational-memory keeps a curated ledger and projects it
deterministically into the summary. Handoff takes the opposite stance: don't
curate in the background, just have the agent write one excellent briefing at
the boundary and start clean. Pick the model that matches how you work.

---

## License

MIT
