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

Plus a best-effort automatic path: a **`system_prompt` hook** watches *live*
context usage and, past a soft threshold, nudges the agent toward a handoff.
The nudge is weaker than the skill (a system-prompt line can be deferred), so
it's a backstop, not the main path.

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

```bash
# REQUIRED — the manual trigger. /self-handoff resolves to this skill.
ln -s ~/src/ext/hermes-handoff-context-engine/skills/self-handoff \
    "$HERMES_HOME/skills/self-handoff"

# RECOMMENDED — how to write a good handoff. Skip if you maintain your own
# writing-a-self-handoff skill; yours takes over automatically (resolved by name).
ln -s ~/src/ext/hermes-handoff-context-engine/skills/writing-a-self-handoff \
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

| Setting | Default | Description |
|---|---|---|
| `context.engine` | `"compressor"` | Set to `"handoff"` to activate |
| `plugins.enabled` | `[]` | Must include `"handoff"` |
| `soft_ratio` (constant in `engine.py`) | `0.60` | Fraction of context at which the agent is asked to author its handoff |
| `hard_ratio` (constant in `engine.py`) | `0.80` | Safety net; truncates if no handoff was produced by here |

The soft/hard ratios are currently constants in `engine.py`. Leave headroom
between `soft` and `hard` so the agent has room and tools to write a good
handoff before the safety net trips.

Unlike the built-in compressor (and unlike observational-memory's background
passes), **the handoff engine makes no side-channel LLM calls of its own** — the
handoff is authored by the primary agent in a normal turn, so it uses your main
model and full toolset.

---

## Known limitations

- **The automatic (threshold) path is best-effort.** The `system_prompt` nudge
  is weaker than the skill turn; a busy agent may defer it. It exists as a
  backstop. For a guaranteed handoff, invoke the `/self-handoff` skill — that's
  an authoritative user turn. If neither fires and context hits the hard
  threshold, the safety-net truncation keeps the session alive (but that reset
  is lossy — the whole point is to hand off *before* then).

---

## Relationship to hermes-observational-memory

A sibling project. Observational-memory keeps a curated ledger and projects it
deterministically into the summary. Handoff takes the opposite stance: don't
curate in the background, just have the agent write one excellent briefing at
the boundary and start clean. Pick the model that matches how you work.

---

## License

MIT
