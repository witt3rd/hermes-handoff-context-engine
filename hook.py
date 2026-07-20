"""Automatic handoff triggering, split across two hooks.

Why two hooks. The reliable manual trigger is the ``self-handoff`` SKILL, because
invoking a skill injects a real, authoritative *user turn* the agent acts on even
mid-task. The automatic path originally tried to do the same job with a
``system_prompt`` nudge — ambient text appended to the system prompt — and the
live result was unambiguous: the nudge fired correctly at 55% and 58% of context
and converted **zero** times out of two. A busy agent defers ambient text.

So detection and delivery are split:

* ``system_prompt_handler`` — DETECTION. It receives ``agent``, so it can read the
  engine's authoritative token count and decide when the soft threshold is
  crossed. It flips the session to ``authoring`` and records usage. It injects
  only a short marker line.
* ``pre_llm_call_handler`` — DELIVERY. Hermes injects this hook's return value
  into the **user message** rather than the system prompt, which is the strongest
  channel available to a plugin. It does NOT receive ``agent``, so it reads the
  phase (and urgency) from the shared in-process state the detection hook wrote.

Ordering is safe: turn_context runs the system_prompt hook before the
pre_llm_call hook, so detection and delivery happen in the same turn.
"""

import logging
from typing import Any, Dict, List, Optional

from .state import PHASE_NORMAL, PHASE_AUTHORING, PHASE_READY

logger = logging.getLogger(__name__)

try:
    from agent.model_metadata import estimate_messages_tokens_rough
except Exception:  # pragma: no cover - defensive
    estimate_messages_tokens_rough = None

# Above this fraction of the context window the situation is no longer "wrap up
# at a natural pause" — a couple of big turns can hit the wall, so the injected
# instruction escalates to stop-now. Must sit BETWEEN soft_ratio and hard_ratio:
# below soft it would never be reached, above hard the truncation beats it.
URGENT_USAGE = 0.75


def _is_forked_agent(agent: Any) -> bool:
    """True for a background fork that SHARES the parent's session_id.

    Hermes' background review runs in a forked AIAgent that deliberately adopts
    the parent's ``session_id`` for prompt-cache warmth
    (``background_review.py``: ``review_agent.session_id = agent.session_id``),
    while reviewing the *pre-compression* conversation — so its context can be
    an order of magnitude larger than the live foreground session.

    Because this plugin keys all state by ``session_id``, a fork's token count
    would otherwise be attributed to the parent: the fork trips the soft
    threshold, the parent gets marked ``authoring``, and the *foreground* agent
    is told to hand off while it is still small. Observed live as a handoff loop
    — a session that had just reset to ~47k was told to hand off again minutes
    later on the fork's ~530k reading.

    ``_persist_disabled`` is the reliable marker: ``agent_init`` sets it False
    for every real agent and only the review fork sets it True — it exists
    precisely to stop a session_id-sharing fork from writing shared state, which
    is exactly what we must not do either.
    """
    if getattr(agent, "_persist_disabled", False):
        return True
    if getattr(agent, "_memory_write_origin", "") == "background_review":
        return True
    return False


# The background review's harness prompt. pre_llm_call receives no `agent`, so
# this is the only way to avoid injecting the handoff instruction into a review
# turn (which would make the reviewer try to write a handoff).
_REVIEW_PROMPT_MARKER = "review the conversation above and update the skill library"


def _resolve_engine(agent: Any) -> Optional[Any]:
    # The live per-agent engine is at .context_compressor (Hermes deep-copies the
    # registered engine per agent). ._context_engine is NOT set by the host.
    engine = getattr(agent, "context_compressor", None) or getattr(agent, "_context_engine", None)
    if engine is None or getattr(engine, "name", None) != "handoff":
        return None
    return engine


def _estimate_usage(engine: Any, conversation_history: List[Dict[str, Any]]) -> float:
    """Fraction of the context window currently in use.

    Source of truth, in order:

    1. ``last_preflight_tokens`` — the live request size the host passed to
       ``should_compress()``. This is the figure Hermes itself acts on.
    2. ``estimate_messages_tokens_rough`` — only until we've seen a preflight
       number (e.g. the first turn after a restart). It under-counts structured
       tool-result blocks badly: a real 812k-token session estimated under 600k
       here, which is exactly why it is not the primary source.
    3. ``last_prompt_tokens`` — last resort; lags a full turn behind.
    """
    ctx_len = getattr(engine, "context_length", 0) or 0
    if not ctx_len:
        return 0.0

    tokens = getattr(engine, "last_preflight_tokens", 0) or 0
    if not tokens and estimate_messages_tokens_rough and conversation_history:
        try:
            tokens = estimate_messages_tokens_rough(conversation_history)
        except Exception:
            tokens = 0
    if not tokens:
        tokens = getattr(engine, "last_prompt_tokens", 0) or 0

    return tokens / ctx_len if ctx_len else 0.0


# -- DETECTION -------------------------------------------------------------

def system_prompt_handler(
    agent: Any,
    session_id: str,
    conversation_history: List[Dict[str, Any]],
    **kwargs,
) -> Optional[Dict[str, Any]]:
    # A background fork shares the parent's session_id but carries a different
    # (usually far larger) context. Never let its size decide the parent's fate.
    if _is_forked_agent(agent):
        return None

    engine = _resolve_engine(agent)
    if engine is None:
        return None
    store = getattr(engine, "store", None)
    if store is None:
        return None

    store.ensure_session(session_id)
    phase = store.get_phase(session_id)

    if phase != PHASE_NORMAL:
        # Keep usage fresh so the injected instruction's urgency tracks reality
        # as the session keeps growing while the agent hasn't handed off yet.
        if phase == PHASE_AUTHORING:
            store.set_usage(session_id, _estimate_usage(engine, conversation_history))
            return {"content": _marker()}
        return None

    usage = _estimate_usage(engine, conversation_history)
    if usage >= engine.soft_ratio:
        store.set_phase(session_id, PHASE_AUTHORING)
        store.set_usage(session_id, usage)
        logger.info(
            "Handoff: context at %.0f%% (~%s/%s tokens) for %s — requesting a "
            "self-handoff (instruction injected into the user turn).",
            usage * 100,
            f"{getattr(engine, 'last_preflight_tokens', 0):,}",
            f"{getattr(engine, 'context_length', 0):,}",
            session_id,
        )
        return {"content": _marker()}

    return None


# -- DELIVERY --------------------------------------------------------------

def pre_llm_call_handler(session_id: str = "", **kwargs) -> Optional[Dict[str, Any]]:
    """Inject the handoff instruction into the USER message.

    Returns ``{"context": ...}``; Hermes appends it to the user turn. This is the
    escalation that makes the automatic path actually convert — the same reason
    the manual ``/self-handoff`` skill works and an ambient system-prompt nudge
    does not.
    """
    if not session_id:
        return None

    # pre_llm_call gets no `agent`, so a forked review turn can't be identified
    # structurally — but it is recognisable by its harness prompt. Without this,
    # a legitimately-authoring parent would inject "write your handoff now" into
    # the *reviewer's* turn and the reviewer would try to write one.
    user_message = kwargs.get("user_message") or ""
    if _REVIEW_PROMPT_MARKER in str(user_message).lower():
        return None

    # No `agent` here — reach the shared state directly. Every engine deep-copy
    # and both hooks operate on the same module-level dict.
    from .state import HandoffStore

    store = HandoffStore()
    if store.get_phase(session_id) != PHASE_AUTHORING:
        return None

    usage = store.get_usage(session_id)
    return {"context": _instruction(usage)}


# -- Text ------------------------------------------------------------------

def _marker() -> str:
    """Short, hash-stable system-prompt marker. The imperative lives in the
    user-message injection; this only keeps the state visible in context."""
    return (
        "[Context handoff pending: this session has crossed its handoff threshold. "
        "Write your successor handoff and call `finalize_handoff` — see the "
        "instruction in the current turn.]"
    )


def _instruction(usage: float) -> str:
    pct = int(round(usage * 100))

    if usage >= URGENT_USAGE:
        head = (
            f"🛑 STOP — CONTEXT HANDOFF REQUIRED NOW. This session is at ~{pct}% of its "
            "context window and is within a turn or two of a hard limit. If you hit it, "
            "there is no graceful summary: the transcript is chopped to the last handful "
            "of messages and everything else is lost. Do not continue the current task."
        )
        pause = (
            "Do this THIS TURN, before anything else. If you are mid-step, that is fine "
            "and expected — capture the in-progress state in the handoff itself rather "
            "than trying to finish first."
        )
    else:
        head = (
            f"⚠️ CONTEXT HANDOFF REQUESTED. This session is at ~{pct}% of its context "
            "window. Rather than let it drift into a lossy truncation, hand off to a "
            "fresh instance of yourself now."
        )
        pause = (
            "Finish only what is needed to leave a clean state — do not start anything "
            "new — then do this before continuing."
        )

    return f"""{head}

{pause}

1. Write a COMPLETE successor handoff. Follow your `writing-a-self-handoff` skill
   for how to write it well (load it with `skill_view` if it isn't in context).
   Write for a reader with your capabilities and none of this session's memory:
   lead with the traps, the current verified state, key files/paths, decisions and
   dead-ends, ordered next steps, and anything you owe that isn't done.
2. Save it to a markdown file using your file-editing tools.
3. Call the `finalize_handoff` tool with `confirm: true` and `path:` set to the
   exact file you wrote.
4. Then stop and end your turn. The session resets into a fresh context seeded
   with your handoff, and next-you continues from it.

This document is the only thing that crosses the reset. Everything you learned
here that isn't in it is lost."""
