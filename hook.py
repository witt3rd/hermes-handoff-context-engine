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


def _directive(path) -> str:
    return f"""\
🔴 CONTEXT HANDOFF REQUIRED — do this before anything else.

This session is approaching its context limit. Rather than lose context to a
lossy summary, you will write a handoff for your successor (the next you), then
the session will reset into a fresh context seeded with that handoff.

INSTRUCTIONS
1. STOP starting new work. Finish only what is needed to leave a clean state.
2. Using your file-editing tools, write a COMPLETE handoff document to:
       {path}
   Use your other tools freely to make it accurate — re-read key files, check
   `git log`/`git status`, re-run a test if a result is uncertain.
3. The handoff must let a fresh agent continue with NO other context. Cover:
   - Task & goal: what we're ultimately trying to achieve.
   - Current state: what is done, what works, what is verified.
   - Key files & locations: paths, functions, line references that matter.
   - Decisions & rationale: what we chose and why.
   - Rejected approaches / dead ends: what NOT to try again, and why.
   - Constraints & gotchas: environment, conventions, hard-won facts.
   - Next steps: the concrete, ordered actions to take next.
   - Open questions / pending user asks still unanswered.
4. When — and only when — the document is complete on disk, call the
   `finalize_handoff` tool with confirm=true. Then STOP and end your turn.

Write for a capable agent who knows nothing about this conversation. Be
specific and factual; prefer verbatim paths, commands, and errors over
paraphrase. Do not summarize this instruction back to the user — just do it."""
