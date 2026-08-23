---
name: install-handoff-engine
description: "Install the handoff context engine into a Hermes agent profile: plugin, engine selection, and the two skills."
version: 1.0.0
author: hermes-handoff-context-engine
license: MIT
metadata:
  hermes:
    category: ops
    tags: [install, plugin, context-engine, hermes, setup, profile]
---

# Installing the handoff context engine

The handoff context engine replaces Hermes' mechanical context compressor with
an agent-authored handoff: when the context window fills, the agent writes a
handoff document for its blind successor and the session resets into a fresh
context seeded with that document (plus the most recent turns).

This skill installs it into a Hermes agent profile — the directory holding that
profile's `config.yaml`, plugins, and skills (for the local house convention that
is `~/forge/profiles/<name>/`). Three things must all be true for the engine to
run: the plugin is installed **and enabled**, `context.engine` points at it, and
the two skills it needs are in the profile's skills directory.

## Mechanism, in one line

This is a **general plugin** that calls `ctx.register_context_engine(engine)` at
`register(ctx)` time — not a directory-discovered engine under
`plugins/context_engine/`. That means it installs through the normal plugin flow
(`hermes plugins install`), and once enabled it is selected by name via
`context.engine: handoff` in `config.yaml`.

## Step 1 — Install the plugin

### Official (recommended for everyone): `hermes plugins install`

```bash
hermes plugins install witt3rd/hermes-handoff-context-engine --enable
```

- The CLI clones the repo, checks out an **immutable pinned commit**, and places
  it under `~/.hermes/plugins/handoff/` (profile-scoped).
- `--enable` adds `handoff` to `plugins.enabled`. Without it you're prompted to
  enable; default is **no**.
- For a reproducible/pinned install, pass `--ref <full-40-char-sha>`.
- To update later: `hermes plugins update handoff`. To remove: `hermes plugins
  remove handoff`.

### Local dev (house convention only): symlink

Point the profile's plugins dir at a live checkout so edits apply without
re-running the installer:

```bash
ln -s ~/src/witt3rd/hermes-handoff-context-engine "$HERMES_HOME/plugins/handoff"
```

This is **not** how outside users install it — a symlink makes the profile
depend on that checkout and tracks the repo's working tree, not a pinned release.
Use it only while developing the engine on this box.

## Step 2 — Select the engine

The engine is **never auto-activated**. Set it explicitly in the profile's
`config.yaml`:

```yaml
context:
  engine: handoff        # replaces the built-in "compressor"
```

This both activates the engine and auto-enables its `finalize_handoff` tool — no
`platform_toolsets` change is needed (unless a platform's toolset list is
explicitly set to empty `[]`).

You can also set it interactively: `hermes plugins` → Provider Plugins → Context
Engine → pick `handoff`.

## Step 3 — Install the two skills

The engine and its auto-trigger rely on two skills that are loaded **by bare
name**: `self-handoff` (the manual `/self-handoff` trigger) and
`writing-a-self-handoff` (the authoring craft). Because they're resolved by name
rather than registered via `ctx.register_skill()`, they must be **copied** into
the profile's skills directory — they will not load from the plugin checkout.

```bash
cp -r "$HERMES_HOME/plugins/handoff/skills/self-handoff"           "$HERMES_HOME/skills/self-handoff"
cp -r "$HERMES_HOME/plugins/handoff/skills/writing-a-self-handoff" "$HERMES_HOME/skills/writing-a-self-handoff"
```

Copy, don't symlink. These are starters your agent personalizes over time; a
symlink would push those edits back into the plugin repo and lose them on update.

- **`self-handoff`** (required) — the trigger. `/self-handoff` injects an
  authoritative user turn: *stop, write your handoff now, then finalize*.
- **`writing-a-self-handoff`** (recommended) — the craft. If the profile already
  maintains its own skill under that name, skip the copy; the existing one wins
  by name.

## Step 4 — Restart the gateway

```bash
hermes -p <your-profile> gateway run --replace
# or, for a systemd-managed profile:
sudo systemctl restart hermes-gateway-<profile>
```

## Step 5 — Verify

1. `hermes plugins list` — `handoff` shows enabled.
2. `grep engine <profile>/config.yaml` — `context.engine: handoff`.
3. Check the profile's `skills/` has both handoff skills.
4. In a session, run `/self-handoff`. The agent writes a handoff, calls
   `finalize_handoff`, and the next turn wakes into a fresh context seeded with
   it.

On gateway startup the log should show the engine's threshold line
(`Handoff: thresholds soft=… urgent=… hard=… protect_last_n=… keep_tokens=…`),
which confirms the engine is live, not the built-in compressor.

## Optional configuration

All settings live under `context.handoff` in `config.yaml` and are optional:

```yaml
context:
  engine: handoff
  handoff:
    soft_ratio: 0.85       # request a handoff at this fraction of the window
    urgent_ratio: 0.85     # at/above this the instruction becomes "stop now"
    hard_ratio: 0.90       # safety net: lossy truncation if no handoff exists
    protect_last_n: 16     # tail kept if the safety net fires
    keep_tokens: 8000      # recent raw transcript retained through a swap (0 = full reset)
```

Note: the `compression.*` block configures the built-in compressor and does **not**
reach this plugin engine — only `context.handoff.*` and the `compression.enabled`
gate apply.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/self-handoff` not recognized | `self-handoff` skill not copied into `$HERMES_HOME/skills/` — redo step 3. |
| Engine not active despite `context.engine: handoff` | Plugin not in `plugins.enabled` — check `hermes plugins list`; re-enable. |
| No `finalize_handoff` tool | `context.engine` not set to `handoff`, or a platform toolset list is empty `[]`. |
| No `Handoff: thresholds` log at startup | Engine isn't selected; the built-in compressor is running instead. |