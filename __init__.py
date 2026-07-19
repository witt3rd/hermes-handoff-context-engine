"""Hermes Handoff Context Engine.

Replaces mechanical context compression with an agent-authored handoff: at a
soft threshold the agent writes a handoff document for its successor (using its
real tools), then the session resets into a fresh context seeded with that
document. See engine.py and hook.py for the state machine.

Registers four surfaces from one register(ctx):
  - context engine  → owns should_compress()/compress() (the swap)
  - system_prompt hook → detects the threshold and injects the authoring directive
  - finalize_handoff tool → exposed by the engine; the agent calls it when done
  - /handoff command → manual trigger for the same flow
"""

from .engine import HandoffContextEngine
from .hook import system_prompt_handler, request_manual_handoff


def register(ctx):
    """Register the handoff context engine, hook, and command with Hermes."""
    engine = HandoffContextEngine()
    ctx.register_context_engine(engine)

    ctx.register_hook("system_prompt", system_prompt_handler)

    # Manual trigger. The command handler signature is fn(raw_args) -> str|None
    # and has no session context, so it only sets a flag; the system_prompt
    # hook (which has the live session) picks it up on the next turn.
    def handoff_command(raw_args: str = "") -> str:
        request_manual_handoff()
        return (
            "📝 Handoff requested. On the next turn I'll write a handoff document "
            "for my successor and then reset into a fresh context seeded with it."
        )

    try:
        ctx.register_command(
            "handoff",
            handoff_command,
            description="Write a handoff for a fresh session, then reset into it.",
        )
    except Exception:
        # Command registration is optional; the automatic threshold path still works.
        pass
