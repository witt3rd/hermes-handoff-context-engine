# handoff context engine — two steps to finish

`hermes plugins install` cloned this plugin and (if you said yes) added it to
`plugins.enabled`. Two things remain — deliberately, because an installer
shouldn't make them for you:

## 1. Select the engine

In your profile's `config.yaml`:

```yaml
context:
  engine: handoff        # replaces the built-in "compressor"
```

That's what activates it. The `finalize_handoff` tool auto-enables along with
the engine — **no `platform_toolsets` change is needed** (unless you've
explicitly set a platform's toolset list to empty `[]`).

## 2. Install the two skills — copy, don't symlink

Skills are starters your agent personalizes over time, so **copy** them (a
symlink would push the agent's edits back into the plugin repo and lose them on
update). From the installed plugin directory
(`~/.hermes/plugins/hermes-handoff-context-engine/` by default):

```bash
cp -r ./skills/self-handoff           "$HERMES_HOME/skills/self-handoff"
cp -r ./skills/writing-a-self-handoff "$HERMES_HOME/skills/writing-a-self-handoff"
```

- **`self-handoff`** (required) — the trigger. `/self-handoff` makes the agent
  write a handoff and reset into a fresh context seeded with it.
- **`writing-a-self-handoff`** (recommended) — the craft of a good handoff. Skip
  it if you already maintain your own skill by that name; yours wins by name.

## 3. Restart the gateway, then try it

```bash
hermes -p <your-profile> gateway run --replace   # or: sudo systemctl restart hermes-gateway-<profile>
```

Then, in a session, run **`/self-handoff`**. The agent writes a handoff, calls
`finalize_handoff`, and wakes the next turn into a fresh context seeded with it.

No database, no third-party dependencies — pure standard library.
