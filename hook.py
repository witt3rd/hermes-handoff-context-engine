"""Per-turn AUTO nudge toward a handoff as context fills.

The reliable, authoritative trigger is the bundled ``self-handoff`` skill — a
real user turn the agent can't shrug off. This hook is only the best-effort
AUTOMATIC path: as live context approaches the limit it nudges the agent to
write a handoff and call ``finalize_handoff``. A system-prompt nudge is
inherently weaker than the skill turn (a busy agent may defer it), which is
exactly why the manual trigger is a skill and this is only a backstop. If the
agent ignores the nudge and context reaches the hard limit, the engine's
safety-net compaction (engine.compress) keeps the session from dying.

Token accounting note: we estimate usage from the LIVE conversation
(``estimate_messages_tokens_rough``) rather than the engine's
``last_prompt_tokens``, which lags a turn behind and badly undercounts when a
single turn adds large tool results.
"""

import logging
from typing import Any, Dict, List, Optional

from .state import PHASE_NORMAL, PHASE_AUTHORING, PHASE_READY

logger = logging.getLogger(__name__)

try:
    from agent.model_metadata import estimate_messages_tokens_rough
except Exception:  # pragma: no cover - defensive
    estimate_messages_tokens_rough = None


def _resolve_engine(agent: Any) -> Optional[Any]:
    # The live per-agent engine is at .context_compressor (Hermes deep-copies
    # the registered engine per agent). ._context_engine is NOT set by the host.
    engine = getattr(agent, "context_compressor", None) or getattr(agent, "_context_engine", None)
    if engine is None or getattr(engine, "name", None) != "handoff":
        return None
    return engine


def _estimate_usage(engine: Any, conversation_history: List[Dict[str, Any]]) -> float:
    ctx_len = getattr(engine, "context_length", 0) or 0
    if not ctx_len:
        return 0.0
    tokens = 0
    if estimate_messages_tokens_rough and conversation_history:
        try:
            tokens = estimate_messages_tokens_rough(conversation_history)
        except Exception:
            tokens = 0
    # Fall back to the (lagging) last_prompt_tokens only if we can't estimate.
    if not tokens:
        tokens = getattr(engine, "last_prompt_tokens", 0) or 0
    return tokens / ctx_len if ctx_len else 0.0


def system_prompt_handler(
    agent: Any,
    session_id: str,
    conversation_history: List[Dict[str, Any]],
    **kwargs,
) -> Optional[Dict[str, Any]]:
    engine = _resolve_engine(agent)
    if engine is None:
        return None
    store = getattr(engine, "store", None)
    if store is None:
        return None

    store.ensure_session(session_id)
    phase = store.get_phase(session_id)

    # Once a handoff is ready/authoring is in flight, don't add more nudges;
    # the skill flow / compress swap take over.
    if phase != PHASE_NORMAL:
        if phase == PHASE_AUTHORING:
            return {"content": _nudge()}
        return None

    usage = _estimate_usage(engine, conversation_history)
    if usage >= engine.soft_ratio:
        store.set_phase(session_id, PHASE_AUTHORING)
        logger.info(
            "Handoff: context at %.0f%% for %s — nudging toward a self-handoff.",
            usage * 100, session_id,
        )
        return {"content": _nudge()}

    return None


def _nudge() -> str:
    return (
        "⚠️ CONTEXT IS FILLING UP. Soon this session will hit its limit and lose "
        "context to a lossy reset. Before that happens, wrap up cleanly and hand "
        "off to your successor: write a complete handoff (follow your "
        "`writing-a-self-handoff` skill), save it to a file, and call the "
        "`finalize_handoff` tool with that path — the session will then reset into "
        "a fresh context seeded with your handoff. Prefer doing this at the next "
        "natural pause rather than mid-step. (The user can also trigger it "
        "explicitly with the `/self-handoff` skill.)"
    )
