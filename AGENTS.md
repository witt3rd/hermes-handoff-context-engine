# hermes-handoff-context-engine — AGENTS.md

Agent operating charter for this repo. If you are a coding agent working here,
this is your ground truth. Scope: anyone touching `engine.py`, `hook.py`,
`state.py`, the `skills/`, or the install docs.

## Goals — what this repo is for

Mechanical context compression loses the plot: every summary is a summary of a
summary, and decisions, rejected approaches, and hard-won constraints decay
until the agent works from a blurred photocopy. This engine automates the
thing experienced operators already do by hand — **the agent writes a handoff
for its next self, the session resets, and the successor reads it and starts
sharp**.

The standing goal: replace lossy compression with an agent-authored handoff as
the default when Hermes context fills, while never letting the window blow.

## Merits — what is load-bearing, protect these

- **No LLM call inside `compress()`.** The intelligence must come from the real
  agent using its real tools (re-read files, `git log`, run tests) — never from
  a separate summarization call. `compress()` only swaps in the file the agent
  wrote. Do not regress this into a summary-producing engine.
- **The trigger is a skill, not a slash command.** A plugin slash command is
  terminal (`gateway/run.py` returns its string; no agent turn), so a busy agent
  ignores it. A skill invocation injects a real authoritative user turn. The
  manual trigger is `self-handoff`; the automatic path deliberately splits into
  a `system_prompt` *detection* hook and a `pre_llm_call` *delivery* hook because
  an ambient system-prompt nudge fired correctly twice and converted **zero**
  times in live use. Only a user-turn instruction gets acted on.
- **Soft/hard threshold discipline.** soft asks for a handoff while the agent
  still has room; hard is a lossy safety net so the window is never exceeded.
  `soft <= urgent <= hard` is enforced (an inverted pair silently kills the
  engine). Thresholds are calibrated to real measured token rates — do not
  casually re-tune them.
- **The three lifted behaviors** (from opencode's `session/compaction.ts`) are
  now part of the contract: the enforced handoff **template**, the **layered
  prior** (previous handoff carried forward), and the **retained recent tail**
  (`keep_tokens`). The handoff is primary; layered prior and raw tail are
  continuity aids on top of it, not replacements.
- **`finalize_handoff` is a real tool**, not a fake step — the agent writes the
  document with its own file tools, then calls the tool with the path. Phase
  machine: `normal → authoring → ready`, stored in shared in-process state.

## Concepts

- **Fork awareness.** Background review forks share the parent's `session_id`
  but carry a far larger context. Never let a fork's token count trip the
  parent's threshold — that manifested as a live handoff loop. `_is_forked_agent`
  in `hook.py` is the guard.
- **State is in-process, not persistent.** `state.py` is a module-global dict
  under a lock (the `HandoffStore` facade). A gateway restart mid-handoff loses
  it harmlessly — re-trigger `/self-handoff`. The handoff *documents* the agent
  writes are ordinary files and are unaffected.
- **Copy skills, don't symlink.** The two skills (`self-handoff`,
  `writing-a-self-handoff`) are starters the agent personalizes; the engine
  loads them **by bare name**, so they must be copied into `$HERMES_HOME/skills/`
  — a symlink pushes the agent's edits back into this repo.

## Mechanisms

### Layout

| Path | Purpose |
|---|---|
| `engine.py` | `HandoffContextEngine(ContextEngine)` — thresholds, `should_compress`, `compress`, `_swap_in_handoff`, `_safety_truncate`, the `finalize_handoff` tool, the handoff template. |
| `hook.py` | Automatic trigger split across `system_prompt_handler` (detection) and `pre_llm_call_handler` (delivery), plus the `_instruction` directive text. |
| `state.py` | In-process `HandoffStore` (phase, handoff path, swap count, usage, urgent, `last_handoff`). |
| `plugin.yaml` | Manifest. Name: `handoff`. Pure stdlib, no deps. |
| `__init__.py` | `register(ctx)` — `ctx.register_context_engine()` + the two hooks. |
| `skills/` | `self-handoff` (trigger), `writing-a-self-handoff` (craft), `install-handoff-engine` (install runbook). |
| `after-install.md` | **Repo root, not in skills/** — Hermes auto-displays it after `hermes plugins install` (`hermes_cli/plugins_cmd.py` reads `plugin_dir / "after-install.md"`). Do not move it. |
| `README.md` | Human/install-facing guide (this is a distributed plugin; README is kept for outside users, AGENTS.md is the agent charter). |

### Conventions & commands

- **No third-party dependencies.** Pure Python standard library. Do not add a
  dependency without a strong reason — it breaks the plugin's easy install.
- **No comments unless they earn their place.** When you add code, follow the
  existing style: dense docstrings explaining *why* (the pitfalls, the measured
  rates, the live bugs) rather than what.
- **Settings** are namespaced under `context.handoff.*` in `config.yaml`
  (`soft_ratio`, `urgent_ratio`, `hard_ratio`, `protect_last_n`, `keep_tokens`).
  `compression.*` does NOT reach a plugin engine — only `compression.enabled`
  gates it. Don't "fix" the engine to read `compression.*`.
- **Syntax check** before you consider a change done:

  ```bash
  python -c "import ast; [ast.parse(open(f).read()) for f in ['engine.py','hook.py','state.py']]"
  ```

  (The files use relative imports and import `agent.context_engine`, so a plain
  `python` run won't import — an AST parse is the cheap smoke test.)

### Git discipline

Active iteration on `master`, small frequent commits. Sync with `origin/master`
before starting. Stage only intended files; never `git add -A` across the repo.
Do not commit unless asked.

## Caretaker loop

As the agent caretaker of this repo, keep it clean, healthy, organized,
recoverable: run the syntax check after edits, keep the AGENTS.md and docs
truthful and current, and commit genuinely with `why` in the message. When you
learn a non-obvious repo fact (a footgun, a live bug), record it here or in the
repo's skill rather than letting it die with the session.

## References

- Install runbook for a fresh Hermes profile: `skills/install-handoff-engine/SKILL.md`.
- The `writing-a-self-handoff` craft skill governs *how* the agent authors the
  handoff; the enforced template in `engine.py` governs its shape.