"""Per-turn hook that drives the handoff authoring flow.

The ``system_prompt`` hook fires every turn. It watches token usage against the
engine's *soft* threshold and, when crossed, flips the session into the
``authoring`` phase and injects a directive telling the agent to write its
handoff document — with full tool access, while it still has room. The
directive keeps being injected (Hermes hashes it and only rebuilds the system
prompt when it changes) until the agent calls ``finalize_handoff``, after which
the engine swaps the transcript and the phase returns to ``normal`` — at which
point this hook returns None and Hermes clears the injected directive.
"""

import logging
from typing import Any, Dict, List, Optional

from .state import PHASE_NORMAL, PHASE_AUTHORING, PHASE_READY

logger = logging.getLogger(__name__)

# Set by the /handoff slash command (hook.py has the session context the
# command handler lacks — see __init__.handoff_command). Single active session
# is assumed for the manual path; documented as a known limitation.
_FORCE_AUTHORING = False


def request_manual_handoff() -> None:
    """Called by the /handoff command to force authoring on the next turn."""
    global _FORCE_AUTHORING
    _FORCE_AUTHORING = True


def _resolve_engine(agent: Any) -> Optional[Any]:
    # The live, per-agent engine is at .context_compressor (Hermes deep-copies
    # the registered engine per agent). ._context_engine is NOT set by the host.
    engine = getattr(agent, "context_compressor", None) or getattr(agent, "_context_engine", None)
    if engine is None or getattr(engine, "name", None) != "handoff":
        return None
    return engine


def system_prompt_handler(
    agent: Any,
    session_id: str,
    conversation_history: List[Dict[str, Any]],
    **kwargs,
) -> Optional[Dict[str, Any]]:
    global _FORCE_AUTHORING

    engine = _resolve_engine(agent)
    if engine is None:
        return None
    store = getattr(engine, "store", None)
    if store is None:
        return None

    store.ensure_session(session_id)
    phase = store.get_phase(session_id)

    usage = 0.0
    if getattr(engine, "context_length", 0):
        usage = engine.last_prompt_tokens / engine.context_length

    forced = _FORCE_AUTHORING
    if phase == PHASE_NORMAL and (forced or usage >= engine.soft_ratio):
        _FORCE_AUTHORING = False
        store.set_phase(session_id, PHASE_AUTHORING)
        store.set_handoff_path(session_id, str(engine.handoff_path_for(session_id)))
        phase = PHASE_AUTHORING
        logger.info(
            "Handoff: entering authoring phase for %s (usage=%.0f%%, forced=%s)",
            session_id, usage * 100, forced,
        )

    if phase == PHASE_AUTHORING:
        return {"content": _directive(engine.handoff_path_for(session_id))}

    # normal / ready → inject nothing (host clears any prior directive).
    return None


# Name of the craft skill this engine defers to for HOW to write a good
# handoff. If the running profile has it, the agent loads its full body via the
# skill_view tool; if not, the compressed fallback in the directive carries.
HANDOFF_SKILL = "writing-a-self-handoff"


def _directive(path) -> str:
    return f"""\
🔴 CONTEXT HANDOFF REQUIRED — do this before anything else.

This session is near its context limit. Instead of a lossy summary, write a
handoff for your successor (next-you); the session then resets into a fresh
context seeded with it.

1. HOW to write it — load and follow the `{HANDOFF_SKILL}` skill (use your
   skill_view tool) if it is available. That skill is the source of truth for
   the craft; do not improvise past it.
2. STOP starting new work. Using your file-editing tools, write the COMPLETE
   handoff to:
       {path}
   Use your other tools freely to make it accurate — re-read key files, check
   `git log`/`git status`, re-run a test if a result is uncertain.
3. When — and only when — the document is complete on disk, call the
   `finalize_handoff` tool with confirm=true. Then STOP and end your turn.

If the `{HANDOFF_SKILL}` skill is unavailable, at minimum: write for a reader
who knows nothing of this session; LEAD WITH THE TRAPS you paid for (wins are
recoverable from `git log`); cover current verified state, key files/paths,
decisions + rationale, rejected approaches / dead ends, constraints & gotchas,
ordered next steps, and open questions; flag judgment calls as overrulable; and
name unfinished items honestly rather than tidying over them. Do not summarize
this instruction back to the user — just do it."""
