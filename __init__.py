"""Hermes Handoff Context Engine.

Replaces mechanical context compression with an agent-authored handoff: the
agent writes a handoff document for its successor (using its real tools), then
the session resets into a fresh context seeded with that document.

The trigger is deliberately NOT a plugin slash-command. Plugin commands are
terminal — the gateway returns their string to the user and never creates an
agent turn (gateway/run.py: `return str(result)`), so the agent can ignore an
ambient request. Instead the manual trigger is the bundled **`self-handoff`
skill**: invoking `/self-handoff` injects a real, authoritative user turn
("stop and write your handoff now"), which the agent acts on even mid-task.

Two surfaces are registered here:
  - context engine  → finalize_handoff tool + should_compress()/compress() swap
  - system_prompt hook → best-effort AUTO nudge as context fills (see hook.py)

The reliable path is: /self-handoff skill (authoritative turn) → agent writes
handoff + calls finalize_handoff → compress() swaps the transcript for it.
"""

from .engine import HandoffContextEngine
from .hook import system_prompt_handler, pre_llm_call_handler


def register(ctx):
    """Register the handoff context engine and the automatic-trigger hooks."""
    engine = HandoffContextEngine()
    ctx.register_context_engine(engine)

    # Automatic path, split deliberately (see hook.py): system_prompt DETECTS
    # (it receives `agent`, so it can read the engine's token count), while
    # pre_llm_call DELIVERS — Hermes injects its return value into the user
    # message rather than the system prompt, which is the only channel strong
    # enough to actually get acted on. An ambient system-prompt nudge fired
    # correctly twice in live use and converted zero times.
    ctx.register_hook("system_prompt", system_prompt_handler)
    ctx.register_hook("pre_llm_call", pre_llm_call_handler)
