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

Detection also happens in the engine itself: ``should_compress()`` (before
every API call, mid tool-loop included) and ``compress()`` when gateway session
hygiene calls it below the hard net. All three go through
``engine.request_handoff``. Delivery is always this module's pre_llm_call.
"""

import logging
from typing import Any, Dict, List, Optional

from .state import PHASE_NORMAL, PHASE_AUTHORING, PHASE_READY

try:
    from .engine import HANDOFF_TEMPLATE
except Exception:  # pragma: no cover - defensive; engine always importable here
    HANDOFF_TEMPLATE = ""

logger = logging.getLogger(__name__)

try:
    from agent.model_metadata import estimate_messages_tokens_rough
except Exception:  # pragma: no cover - defensive
    estimate_messages_tokens_rough = None

# Fallback only. The live value is the engine's `urgent_ratio` (configurable via
# context.handoff.urgent_ratio and validated to sit within [soft, hard]). Above
# it the injected instruction escalates from "wrap up at a natural pause" to
# stop-now. With the trigger late (0.85) urgent defaults to == soft, because
# anything that fires is already close enough to the wall that "finish what
# you're doing first" is the wrong advice.
URGENT_USAGE = 0.85


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


def _estimate_usage(engine: Any, conversation_history: List[Dict[str, Any]]):
    """``(fraction, tokens, basis)`` of the context window currently in use.

    Source of truth, in order:

    1. ``engine.current_pressure()`` - the provider's own prompt_tokens when we
       have one since the last rotation, else the host's preflight figure
       corroborated against what the messages measure. Each carries its basis;
       only ``provider_reported`` is a measurement.
    2. ``estimate_messages_tokens_rough`` - only until the engine has seen any
       figure (e.g. the first turn after a restart). It under-counts structured
       tool-result blocks badly: a real 812k-token session estimated under 600k
       here, which is exactly why it is not the primary source.
    3. ``last_prompt_tokens`` - last resort; lags a full turn behind.
    """
    ctx_len = getattr(engine, "context_length", 0) or 0
    if not ctx_len:
        return 0.0, 0, None

    tokens, basis = 0, None
    pressure = getattr(engine, "current_pressure", lambda: None)()
    if pressure:
        tokens, basis = pressure
    if not tokens and estimate_messages_tokens_rough and conversation_history:
        try:
            tokens, basis = estimate_messages_tokens_rough(conversation_history), "engine_estimate"
        except Exception:
            tokens = 0
    if not tokens:
        tokens = max(0, getattr(engine, "last_prompt_tokens", 0) or 0)
        basis = "host_estimate" if tokens else None

    return tokens / ctx_len, tokens, basis


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
            # Refresh ONLY from a figure the engine itself has (provider usage,
            # or the corroborated host figure). This engine copy is often brand
            # new (the gateway builds an agent per message) with nothing yet;
            # falling back to the rough message estimate here overwrote the
            # request-time figure with a number several times too low (Forge
            # was told ~6% and ~62% while the log said 85%/86%).
            pressure = getattr(engine, "current_pressure", lambda: None)()
            ctx_len = getattr(engine, "context_length", 0) or 0
            if pressure and ctx_len:
                preflight, pbasis = pressure
                live = preflight / ctx_len
                store.set_usage(session_id, live, tokens=preflight,
                                context_length=ctx_len, basis=pbasis,
                                soft=engine.soft_ratio, hard=engine.hard_ratio)
                store.set_urgent(session_id,
                                 live >= getattr(engine, "urgent_ratio", URGENT_USAGE))
            return {"content": _marker()}
        return None

    usage, tokens, basis = _estimate_usage(engine, conversation_history)
    if usage >= engine.soft_ratio:
        # One entry point for every trigger so each request is logged and
        # ledgered identically (engine.request_handoff).
        engine.request_handoff(usage, "turn_start", tokens,
                               session_id=session_id, basis=basis)
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

    return {"context": _instruction(store.get_usage(session_id),
                                    store.get_urgent(session_id),
                                    store.get_last_handoff(session_id),
                                    store.get_usage_detail(session_id))}


# -- Text ------------------------------------------------------------------

def _marker() -> str:
    """Short, hash-stable system-prompt marker. The imperative lives in the
    user-message injection; this only keeps the state visible in context."""
    return (
        "[Context handoff pending: this session has crossed its handoff threshold. "
        "Write your successor handoff and call `finalize_handoff` — see the "
        "instruction in the current turn.]"
    )


def _usage_phrase(usage: float, detail: Optional[Dict[str, Any]] = None) -> str:
    """Say plainly what the percentage is a percentage OF, and how it was got.

    "~62% of its context window" left an agent unable to tell whether that was
    tokens, a message count, or a fraction of some threshold. Name the token
    figure, the window it is measured against, and whether it is the provider's
    own count or an estimate - and when the host's raw estimate was discounted,
    show both, so the agent never meets two "measured" numbers for one session.
    """
    pct = int(round(usage * 100))
    d = detail or {}
    tokens = int(d.get("tokens") or 0)
    ctx = int(d.get("context_length") or 0)
    if not tokens or not ctx:
        return f"at ~{pct}% of the model's context window"
    pct = int(round(tokens / ctx * 100))
    window = f"{pct}% of the model's {ctx:,}-token context window"
    reported = int(d.get("reported") or 0)
    if d.get("basis") == "provider_reported":
        phrase = (f"{tokens:,} tokens (the provider's own prompt-token count from "
                  f"its latest response) — {window}")
    else:
        who = "the host's" if d.get("basis") == "host_estimate" else "an engine"
        phrase = (f"an estimated ~{tokens:,} tokens ({who} estimate, not a "
                  f"provider count; the true size may differ) — {window}")
    if reported and reported >= tokens * 1.5:
        phrase += (f". The host's raw pre-send estimate was ~{reported:,} tokens, "
                   "but the messages do not measure up to that, so it was "
                   "discounted")
    return phrase


def _thresholds_phrase(detail: Optional[Dict[str, Any]]) -> str:
    d = detail or {}
    soft, hard = d.get("soft") or 0, d.get("hard") or 0
    if not soft or not hard:
        return ""
    return (f" A handoff is requested at {int(round(soft * 100))}% of the window; "
            f"the hard safety truncation fires at {int(round(hard * 100))}%.")


def _instruction(usage: float, urgent: bool, prior: Optional[str] = None,
                 detail: Optional[Dict[str, Any]] = None) -> str:
    phrase = _usage_phrase(usage, detail)
    limits = _thresholds_phrase(detail)

    if urgent:
        head = (
            f"🛑 STOP — CONTEXT HANDOFF REQUIRED NOW. This session's context is {phrase}, "
            f"and is approaching its hard limit.{limits} If you hit it, "
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
            f"⚠️ CONTEXT HANDOFF REQUESTED. This session's context is {phrase}.{limits} "
            "Rather than let it drift into a lossy truncation, hand off to a "
            "fresh instance of yourself now."
        )
        pause = (
            "Finish only what is needed to leave a clean state — do not start anything "
            "new — then do this before continuing."
        )

    prior_block = ""
    if prior:
        prior_block = f"""\n\nYour PREVIOUS handoff (from the last reset) is included below. This new document
supersedes it — carry forward anything still relevant: the objective, standing
decisions, unresolved work, blocked items, and still-live traps. Drop only what
is finished and no longer needed. You are NOT starting from nothing; you are
updating a prior.\n\n<prior-handoff>\n{prior}\n</prior-handoff>"""

    template_block = f"""
Write to this EXACT structure — keep every section, even when empty:

{HANDOFF_TEMPLATE}""" if HANDOFF_TEMPLATE else ""

    return f"""{head}

{pause}

1. Write a COMPLETE successor handoff. Follow your `writing-a-self-handoff` skill
   for the craft — lead with the traps, pointer recoverable state, flag judgment
   calls, name your loose ends — but fit the result to the structure below so
   it stays consistent across resets.{template_block}{prior_block}
2. Save it to a markdown file using your file-editing tools.
3. Call the `finalize_handoff` tool with `confirm: true` and `path:` set to the
   exact file you wrote.
4. Then stop and end your turn. The session resets into a fresh context seeded
   with your handoff plus your most recent turns, and next-you continues from
   there.

This document is the primary thing that crosses the reset. Everything you
learned here that isn't in it is lost."""
