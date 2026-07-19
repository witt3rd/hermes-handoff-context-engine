---
name: self-handoff
description: "Reset into a fresh session NOW: write a successor handoff, then finalize it."
version: 1.0.0
author: hermes-handoff-context-engine
license: MIT
metadata:
  hermes:
    category: continuity
    tags: [handoff, continuity, context-reset, finalize, priority]
---

# Self-handoff — reset into a fresh session, now

Invoking this skill is a direct instruction: **stop what you are doing and hand
off to a fresh instance of yourself.** This is the priority. Whatever task you
were mid-way through, pause it — the handoff exists precisely to carry that
in-progress state across the reset. Do not finish other work first.

Do this, in order:

1. **Write the handoff.** Follow your `writing-a-self-handoff` skill for *how* to
   write it well (load it with `skill_view` if it isn't already in context).
   Write for a blind next-you: lead with the traps, name the current verified
   state, the key files/paths, the decisions and dead-ends, and the concrete
   next steps. Capture where you are *right now*, mid-task and all.

2. **Save it to a file.** Write it to a markdown file you choose — e.g.
   `~/.hermes/handoffs/self-handoff-<short-slug>.md` or anywhere convenient.
   Use your file-editing tools. Make it complete and self-contained.

3. **Finalize.** Call the `finalize_handoff` tool with:
   - `confirm: true`
   - `path:` the exact path of the file you just wrote.

4. **Stop.** After `finalize_handoff` returns, end your turn. Do not start new
   work — the session is about to reset into a fresh context seeded with your
   handoff, and your next self will pick up from the document.

If the `finalize_handoff` tool is not available, the handoff context engine
isn't active in this session — in that case just write the handoff file and tell
the user its path, so they can reset manually.
