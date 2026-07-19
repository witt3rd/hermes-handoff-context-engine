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

The plugin registers four cooperating surfaces from a single `register(ctx)`:

| Surface | Role |
|---|---|
| **`system_prompt` hook** (every turn) | Watches token usage. At a **soft** threshold, flips the session into *authoring* and injects a directive: "write your handoff to `<path>`, then call `finalize_handoff`." |
| **`finalize_handoff` tool** | The agent calls it once the document is written. Marks the session *ready*. |
| **`compress()`** (the engine) | On the next turn, discards the whole transcript and returns a fresh seed = system prompt + the authored handoff. **No LLM call** — the intelligence was produced by the real agent. |
| **`/handoff` command** | Manual trigger for the same flow, any time you want a clean reset. |

The lifecycle is a three-phase state machine, stored per session in a small
SQLite file so it survives the per-agent deep-copy Hermes performs on context
engines:

```
normal ──(usage ≥ soft threshold, or /handoff)──▶ authoring
authoring ──(agent writes doc, calls finalize_handoff)──▶ ready
ready ──(should_compress→True, compress() swaps in the doc)──▶ normal
```

Because authoring happens at a **soft** threshold (default 60%), the agent still
has room and full tool access to write an accurate handoff — it can re-read
files, check `git`, and re-run tests while composing it. A **hard** threshold
(default 80%) is a safety net: if no handoff was produced in time, `compress()`
falls back to a plain head/tail truncation so the context window is never
exceeded.

Handoff documents are written to `<profile>/handoffs/<session>.md` and are not
deleted, so you also get a durable trail of every reset.

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

**3. Enable in `config.yaml`**

```yaml
plugins:
  enabled:
    - handoff            # alongside your existing plugins

context:
  engine: "handoff"      # replaces the built-in "compressor"
```

**4. Restart the gateway**

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

- **Manual `/handoff` and multi-session gateways.** The slash-command handler
  receives no session context, so it sets a process-level flag that the next
  turn to run the hook picks up. On a single active CLI session this is exact;
  on a busy multi-session gateway it may trigger on whichever session ticks
  first. The automatic threshold path is always per-session and unaffected.
- **The agent must comply.** Authoring depends on the agent following the
  injected directive. The prompt is forceful, but a determined agent can ignore
  it — in which case the hard-threshold safety net truncates rather than hands
  off.

---

## Relationship to hermes-observational-memory

A sibling project. Observational-memory keeps a curated ledger and projects it
deterministically into the summary. Handoff takes the opposite stance: don't
curate in the background, just have the agent write one excellent briefing at
the boundary and start clean. Pick the model that matches how you work.

---

## License

MIT
