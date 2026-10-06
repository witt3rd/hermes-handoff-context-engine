"""Handoff context engine.

Instead of mechanically summarizing the transcript at the context limit, this
engine orchestrates the workflow that actually works in practice:

  1. A *soft* threshold is crossed → the ``system_prompt`` hook (hook.py) tells
     the agent to write a handoff document for its successor, using its real
     tools (re-read files, ``git log``, run tests). Phase: ``authoring``.
  2. The agent writes the doc and calls the ``finalize_handoff`` tool exposed
     here. Phase: ``ready``.
  3. On the next turn ``should_compress()`` returns True and ``compress()``
     discards the whole transcript and returns a fresh seed containing only the
     system prompt + the authored handoff. Phase: ``normal``.

No LLM call happens inside ``compress()`` — the intelligence was produced by the
real agent in step 1. ``compress()`` just swaps in the file it wrote.

A *hard* threshold acts as a safety net: if the agent never produced a handoff
(ignored the directive, ran out of room), ``compress()`` falls back to a plain
head/tail truncation so the context window is never exceeded.

The host also calls ``compress()`` from places that never consult
``should_compress()`` — chiefly gateway *session hygiene*, which compacts at a
hardcoded 85% of the window before the agent turn starts. Below the hard net
that call is deferred into a handoff request instead of a truncation (see
``compress``). Every request, deferral, swap and truncation is appended to
``$HERMES_HOME/handoffs/events.jsonl`` so a failure is visible after the fact.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.context_engine import ContextEngine

from .state import HandoffStore, PHASE_NORMAL, PHASE_AUTHORING, PHASE_READY

logger = logging.getLogger(__name__)

try:
    from agent.model_metadata import estimate_messages_tokens_rough as _host_estimate
except Exception:  # pragma: no cover - host helper is optional
    _host_estimate = None

DEFAULT_SOFT_RATIO = 0.85
DEFAULT_HARD_RATIO = 0.90
DEFAULT_PROTECT_LAST_N = 16
# Recent raw transcript retained verbatim across a handoff swap, so the
# successor has immediate working context (mirrors opencode's `keep.tokens`).
DEFAULT_KEEP_TOKENS = 8000
# Ceiling on the tail the LOSSY safety truncation retains, as a fraction of the
# window. It is the last line of defence, so it must actually shrink the
# transcript: 16 retained messages once measured ~1.19M tokens.
TRUNCATION_TAIL_RATIO = 0.10
# How far the host's ROUGH pre-send request estimate may exceed what the
# messages themselves measure (system prompt + tool schemas + known noise) before
# we stop believing it. The host documents 2-3x over-counts on heavy sessions;
# Forge (2026-10-05) saw ~17x: a 1.18M "request" whose 15 messages measured ~10k
# and whose real provider count was 69k. Past this factor the figure is treated
# as noise, never as context pressure: chopping messages cannot remove a phantom.
ROUGH_TRUST_FACTOR = 3
# Fixed per-request overhead (system prompt + tool schemas) assumed when no
# provider-reported reading has yet revealed the real one. The host's own
# comment puts 50+ tools at 20-30K; this leaves room for a fat system prompt.
ASSUMED_OVERHEAD_TOKENS = 60_000
# A truncation that keeps at least this fraction of the transcript's size is not
# relief, it is a rotation: it renames the session and frees nothing.
NO_RELIEF_KEPT_FRACTION = 0.9
BASIS_PROVIDER = "provider_reported"   # the API's own prompt_tokens (real)
BASIS_HOST_ESTIMATE = "host_estimate"   # the host's rough / stored figure
BASIS_ENGINE_ESTIMATE = "engine_estimate"  # our own char-based estimate
_IMAGE_TOKENS = 1500
_IMAGE_PARTS = {"image", "image_url", "input_image"}
# Fields the host never sends to the provider (nor counts), plus our own marker.
_NOT_SENT = {"_anthropic_content_blocks", "reasoning_details",
             "_compressed_summary"}


def _load_settings() -> Dict[str, Any]:
    """Read ``context.handoff.*`` from the active profile's config.yaml.

    Deliberately a private namespace rather than reusing ``compression.*``:
    when a plugin engine is active Hermes forwards NONE of the compression
    settings to it (agent_init.py — "external engines own compaction policy"),
    and ``compression.threshold`` means one threshold where this engine has two
    with different semantics. Only ``compression.enabled`` still matters, and it
    gates compaction entirely — if it is false this engine is never called.
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
    except Exception:
        return {}
    ctx = cfg.get("context") if isinstance(cfg, dict) else None
    if not isinstance(ctx, dict):
        return {}
    settings = ctx.get("handoff")
    return settings if isinstance(settings, dict) else {}


SWAP_MARKER = "[CONTEXT HANDOFF — FRESH SESSION SEEDED FROM AGENT-AUTHORED HANDOFF]"
COMPRESSED_SUMMARY_METADATA_KEY = "_compressed_summary"

FINALIZE_TOOL_NAME = "finalize_handoff"

# The handoff template the agent is asked to author to. Lifted from opencode's
# compaction SUMMARY_TEMPLATE so the authored document has a stable, machine-
# parseable shape across swaps — which is what makes the layered prior (carry
# forward still-relevant sections) and the recent-tail retention composable.
HANDOFF_TEMPLATE = """## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Important Details
- [constraints/preferences, decisions and why, important facts/assumptions, exact context needed to continue, or "(none)"]

## Work State
### Completed
- [finished work, verified facts, or changes made; otherwise "(none)"]

### Active
- [current work, partial changes, or investigation state; otherwise "(none)"]

### Blocked
- [blockers, failing commands, or unknowns; otherwise "(none)"]

## Next Move
1. [immediate concrete action, or "(none)"]
2. [next action if known, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]"""


def _own_message_tokens(msg: Dict[str, Any]) -> int:
    """Char-based estimate of everything the provider receives for ``msg``.

    The old estimate looked at ``content`` only. The bulk of a message can sit in
    other fields the host sends and counts — tool_call arguments, the
    ``api_content`` sidecar that substitutes for ``content``, reasoning — so a
    tail of "~8,000 tokens" was really most of a million. Mirrors the host's
    wire shadow: base64 images cost a flat rate instead of their characters.
    """
    sidecar = msg.get("api_content")
    sidecar_wins = (isinstance(sidecar, str) and bool(sidecar)
                    and msg.get("role") in ("user", "assistant"))
    images = 0
    shadow: Dict[str, Any] = {}
    for key, value in msg.items():
        if key in _NOT_SENT:
            continue
        if key == "api_content":
            if sidecar_wins:
                shadow["content"] = value
            continue
        if key == "content":
            if sidecar_wins:
                continue
            if isinstance(value, list):
                cleaned = []
                for part in value:
                    if isinstance(part, dict) and part.get("type") in _IMAGE_PARTS:
                        images += 1
                    else:
                        cleaned.append(part)
                value = cleaned
        shadow[key] = value
    try:
        text = json.dumps(shadow, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(shadow)
    return max(1, len(text) // 4) + images * _IMAGE_TOKENS


def _message_tokens(msg: Dict[str, Any]) -> int:
    """Conservative size of one message: the larger of our estimate and the host's."""
    cost = _own_message_tokens(msg)
    if _host_estimate is not None:
        try:
            cost = max(cost, int(_host_estimate([msg])))
        except Exception:
            pass
    return cost


def _request_tokens(messages: List[Dict[str, Any]]) -> int:
    """Our own conservative size of an entire message list (system prompt included)."""
    return sum(_message_tokens(m) for m in messages if isinstance(m, dict))


def _drop_orphan_tool_results(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop leading tool results whose assistant tool_call was cut away."""
    i = 0
    while i < len(msgs) and msgs[i].get("role") == "tool":
        i += 1
    return msgs[i:]


class HandoffContextEngine(ContextEngine):
    """Context engine that swaps the transcript for an agent-authored handoff."""

    def __init__(self):
        self._name = "handoff"

        # -- Token state read directly by run_agent.py (ABC contract) --------
        self._last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.threshold_tokens = 0
        self.context_length = 0
        self.compression_count = 0

        # Provenance bookkeeping. The host hands us numbers of THREE different
        # kinds through one channel and never says which is which:
        #   * the provider's own prompt_tokens (update_from_response) - real;
        #   * a ROUGH pre-send request estimate (should_compress(prompt_tokens)
        #     at turn start / pre-API) - an estimate that can be many times the
        #     truth, and which the host writes into last_prompt_tokens too;
        #   * the gateway's stored last_prompt_tokens (hygiene, "actual") - a
        #     replay of whichever of the above it saw last.
        # The old engine called all of them "measured". Now only the first is.
        self.last_real_prompt_tokens = 0   # provider-reported, since last rotation
        self.awaiting_real_usage = False   # rotated; no provider reading yet
        self.last_rough_request_tokens = 0  # host's rough estimate of the last request
        self._own_request_tokens = 0       # our size of the last request (select_context)
        self._own_at_real = 0              # ... when the last real reading arrived
        self._deferral_logged = False

        # The live request-size figure the soft-threshold nudge (hook.py) reads,
        # AFTER corroboration: provider-reported where we have it, otherwise the
        # host's estimate capped by what the messages themselves measure.
        self.last_preflight_tokens = 0
        self.last_preflight_basis: Optional[str] = None

        # True once the host has consulted should_compress() on THIS engine copy.
        # The host runs should_compress() before every API call of a turn, so a
        # copy that has never been consulted belongs to an out-of-turn caller —
        # gateway session hygiene's throwaway agent, a manual /compress. See
        # compress() for why that distinction decides truncate-vs-defer.
        self._host_consulted = False

        # -- Thresholds ------------------------------------------------------
        # soft: ask the agent to author its handoff while it still has room and
        #       full tool access. hard: safety net so we never blow the window.
        #
        # Sizing the runway. Measured on clean foreground turns (a session's own
        # API calls, excluding background-review forks that share the session_id
        # and read a far larger pre-compression transcript): ~3.2k tokens/min.
        # A handoff converts in ~90s, so authoring costs ~5k tokens of growth.
        #
        # An earlier revision used ~36k/min and set soft to 0.50/0.65. That rate
        # was contaminated by fork readings — off by 10x — and the over-provisioned
        # runway cost real working context: firing early is NOT free. Every handoff
        # trades the entire live context for a ~7-9k document and interrupts work
        # to do it.
        #
        # So trigger late. At 0.85 on a 1M window there is still ~150k to the wall
        # — ~45 minutes of clean growth for something that takes 90 seconds.
        #
        # The residual risk is BURST, not average rate: one turn reading several
        # large files can add 100k+ at once and leap a threshold outright. That is
        # what hard_ratio's margin is for — 0.90 leaves 100k before the model's
        # limit. If you see `LOSSY SAFETY TRUNCATION` in the log, a burst beat the
        # handoff and these should come down; the log line carries the exact token
        # count so that decision can be arithmetic rather than guesswork.
        #
        # Overridable via `context.handoff.*` in config.yaml — see _apply_settings.
        # (Note: `compression.*` does NOT reach a plugin engine; only
        # `compression.enabled` matters, and it gates compaction entirely.)
        self.soft_ratio = DEFAULT_SOFT_RATIO
        self.hard_ratio = DEFAULT_HARD_RATIO
        self.urgent_ratio = DEFAULT_SOFT_RATIO
        # Host preflight math uses threshold_percent; point it at the hard net.
        self.threshold_percent = self.hard_ratio
        self.keep_tokens = DEFAULT_KEEP_TOKENS

        # We fully own the returned message list, so head/tail protection is a
        # no-op for the handoff swap; it only matters for the safety fallback —
        # where a bigger tail meaningfully softens an already-lossy chop.
        self.protect_first_n = 0
        self.protect_last_n = DEFAULT_PROTECT_LAST_N

        # Apply config overrides last so they win over every default above.
        self._apply_settings()
        self._guard_ready = True

        # -- Session-scoped resources ---------------------------------------
        self.store: Optional[HandoffStore] = None
        self.session_id: Optional[str] = None
        self.hermes_home: Optional[Path] = None
        self.handoff_dir: Optional[Path] = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def last_prompt_tokens(self) -> int:
        return self._last_prompt_tokens

    @last_prompt_tokens.setter
    def last_prompt_tokens(self, value: Any) -> None:
        """Accept host writes, but never let an estimate pose as provider usage.

        The host's turn prologue writes its ROUGH preflight estimate into
        ``last_prompt_tokens`` (turn_context.py), and the gateway persists that
        field as the session's "actual" prompt size, which hygiene replays at
        the next message. A phantom 1.2M estimate therefore became a stored
        "actual" 854k and fed every later decision. Provider readings arrive
        through ``update_from_response`` (which writes the backing field
        directly); a positive write that corroboration would discount is ours
        to refuse. 0 and -1 (the host's "compression just ran" sentinel) always
        pass.
        """
        try:
            v = int(value or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0 and getattr(self, "_guard_ready", False):
            if self._pressure(v)[0] < v:
                logger.info(
                    "Handoff: ignoring host write last_prompt_tokens=%s - not "
                    "provider-reported and not corroborated by the messages.",
                    f"{v:,}",
                )
                return
        self._last_prompt_tokens = v

    # -- Settings ----------------------------------------------------------

    def _apply_settings(self) -> None:
        """Load `context.handoff.*` overrides, validating the ordering invariant.

        soft <= urgent <= hard must hold. Violations are silent killers: with
        soft above hard the safety net truncates before a handoff is ever
        requested, and with urgent outside the band its tier is unreachable
        (we shipped exactly that bug once). Clamp rather than crash — but say so
        loudly, and always log the effective values so there is never ambiguity
        about which numbers are live.
        """
        s = _load_settings()

        def _num(key, default, cast=float):
            try:
                return cast(s[key]) if key in s else default
            except (TypeError, ValueError):
                logger.warning(
                    "Handoff: context.handoff.%s=%r is not a number; using %r.",
                    key, s.get(key), default,
                )
                return default

        soft = _num("soft_ratio", DEFAULT_SOFT_RATIO)
        hard = _num("hard_ratio", DEFAULT_HARD_RATIO)
        urgent = _num("urgent_ratio", soft)
        protect = _num("protect_last_n", DEFAULT_PROTECT_LAST_N, int)
        keep = _num("keep_tokens", DEFAULT_KEEP_TOKENS, int)

        if not 0.0 < soft < 1.0 or not 0.0 < hard < 1.0:
            logger.warning(
                "Handoff: ratios must be between 0 and 1 (got soft=%s hard=%s); "
                "falling back to defaults.", soft, hard,
            )
            soft, hard, urgent = DEFAULT_SOFT_RATIO, DEFAULT_HARD_RATIO, DEFAULT_SOFT_RATIO
        if soft >= hard:
            lowered = max(0.01, round(hard - 0.05, 4))
            logger.warning(
                "Handoff: soft_ratio (%s) must be below hard_ratio (%s), or the "
                "safety net truncates before a handoff is ever requested; "
                "lowering soft to %s.", soft, hard, lowered,
            )
            soft = lowered
        if not soft <= urgent <= hard:
            clamped = min(max(urgent, soft), hard)
            logger.warning(
                "Handoff: urgent_ratio (%s) must sit within [%s, %s]; clamping to %s.",
                urgent, soft, hard, clamped,
            )
            urgent = clamped

        self.soft_ratio = soft
        self.hard_ratio = hard
        self.urgent_ratio = urgent
        self.protect_last_n = max(1, protect)
        self.keep_tokens = max(0, keep)
        self.threshold_percent = self.hard_ratio

        logger.info(
            "Handoff: thresholds soft=%.2f urgent=%.2f hard=%.2f protect_last_n=%d keep_tokens=%d%s",
            self.soft_ratio, self.urgent_ratio, self.hard_ratio, self.protect_last_n,
            self.keep_tokens,
            "" if s else " (defaults — no context.handoff block in config.yaml)",
        )

    # -- Lifecycle ---------------------------------------------------------

    def on_session_start(self, session_id: str, **kwargs) -> None:
        self.session_id = session_id

        home_arg = kwargs.get("hermes_home")
        self.hermes_home = Path(home_arg) if home_arg else Path.home() / ".hermes"

        # Default directory for the fallback handoff path. The agent normally
        # chooses its own path (reported via finalize_handoff); this is only the
        # default used when it doesn't.
        self.handoff_dir = self.hermes_home / "handoffs"
        try:
            self.handoff_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        self.store = HandoffStore()
        self.store.ensure_session(session_id)
        old_id = kwargs.get("old_session_id")
        if kwargs.get("boundary_reason") == "compression" and old_id:
            self.store.inherit(old_id, session_id)
        self.compression_count = self.store.get_swap_count(session_id)

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        self.session_id = None

    def on_session_reset(self) -> None:
        if self.store and self.session_id:
            self.store.reset(self.session_id)
        self._forget_size_readings()
        self._last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
    ) -> None:
        self.context_length = context_length or 200000
        self.threshold_tokens = int(self.context_length * self.hard_ratio)

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        self._last_prompt_tokens = prompt
        self.last_completion_tokens = usage.get("completion_tokens", 0)
        self.last_total_tokens = usage.get("total_tokens", 0)
        if prompt > 0:
            # The only provider-reported number this engine ever sees. Pair it
            # with our own size of the request that produced it, so later
            # growth is projected from a real anchor (see _pressure).
            self.last_real_prompt_tokens = prompt
            self._own_at_real = self._own_request_tokens
            self.awaiting_real_usage = False
            self.last_preflight_tokens = prompt
            self.last_preflight_basis = BASIS_PROVIDER

    def note_request_rough_estimate(self, rough_tokens: int) -> None:
        """Host's rough estimate of the request about to be sent. Diagnostic only."""
        try:
            self.last_rough_request_tokens = max(0, int(rough_tokens))
        except (TypeError, ValueError):
            self.last_rough_request_tokens = 0

    def select_context(self, request_messages, **kwargs):
        """Observe the assembled request; never replace it.

        Runs before every provider call, before the host's pre-API pressure
        check, so it is where we learn what the messages themselves measure -
        the yardstick that exposes an inflated rough estimate.
        """
        try:
            self._own_request_tokens = _request_tokens(request_messages or [])
        except Exception:
            self._own_request_tokens = 0
        return None

    def _forget_size_readings(self) -> None:
        """Drop every size figure that described the pre-rotation transcript."""
        self.last_real_prompt_tokens = 0
        self.awaiting_real_usage = True
        self.last_preflight_tokens = 0
        self.last_preflight_basis = None
        self.last_rough_request_tokens = 0
        self._own_request_tokens = 0
        self._own_at_real = 0
        self._deferral_logged = False
        # Same sentinel the host parks after its own compaction: "no real
        # usage yet". Never leave the pre-swap figure for the gateway to
        # persist as the session's "actual" size.
        self._last_prompt_tokens = -1

    def _pressure(self, reported: int = 0, messages: Optional[List[Dict[str, Any]]] = None):
        """``(tokens, basis)``: the context size to act on, and where it came from.

        ``reported`` is whatever the host passed (rough preflight estimate, or
        the provider's figure echoed back); ``messages`` the transcript when we
        hold it. In order of trust:

        1. A provider-reported reading since the last rotation, plus growth
           measured by our own estimate since that reading. (Mirrors the host's
           ``should_defer_preflight_to_real_usage``: real usage beats a rough
           estimate that is "2-3x real" on heavy sessions.)
        2. The host's figure, capped at ``ROUGH_TRUST_FACTOR`` x what the
           messages themselves measure plus fixed overhead. Truncating or
           handing off removes messages only; a figure the messages cannot
           account for is not pressure this engine can relieve.
        3. The host's figure as-is when we cannot measure the messages.
        """
        reported = int(reported or 0)
        own = _request_tokens(messages) if messages else self._own_request_tokens

        real = self.last_real_prompt_tokens
        if real > 0 and not self.awaiting_real_usage:
            if reported == real:
                return real, BASIS_PROVIDER
            growth = max(0, own - self._own_at_real) if (own and self._own_at_real) else 0
            return real + growth, BASIS_PROVIDER

        if not reported:
            return 0, None
        if own:
            overhead = (max(0, self.last_real_prompt_tokens - self._own_at_real)
                        if self.last_real_prompt_tokens and self._own_at_real
                        else ASSUMED_OVERHEAD_TOKENS)
            cap = ROUGH_TRUST_FACTOR * (own + overhead)
            if reported > cap:
                return cap, BASIS_ENGINE_ESTIMATE
        return reported, BASIS_HOST_ESTIMATE

    def current_pressure(self):
        """Best current ``(tokens, basis)`` without a fresh host figure, or None."""
        real = self.last_real_prompt_tokens
        if real > 0 and not self.awaiting_real_usage:
            return self._pressure(real)
        if self.last_preflight_tokens:
            return self.last_preflight_tokens, (self.last_preflight_basis
                                                or BASIS_HOST_ESTIMATE)
        return None

    def should_defer_preflight_to_real_usage(self, rough_tokens: int) -> bool:
        """Tell the host to trust our corroborated figure over its rough one.

        Without this the base class answers False, the host believes its rough
        estimate, calls should_compress() with it, and on a fresh engine copy
        (no select_context yet) that was ~1.18M "tokens" against a real 69k:
        a lossy truncation on every message. Deferral is declined once the
        corroborated figure reaches soft, so the soft trigger still runs.
        """
        if not rough_tokens or not self.context_length:
            return False
        if rough_tokens < self.context_length * self.hard_ratio:
            return False
        tokens, _ = self._pressure(rough_tokens)
        if tokens >= self.context_length * self.soft_ratio:
            return False
        if not self._deferral_logged:
            self._deferral_logged = True
            logger.warning(
                "Handoff: host's rough estimate ~%s is not corroborated "
                "(%s: ~%s; messages measure ~%s) - deferring to that instead.",
                f"{int(rough_tokens):,}", self._pressure(rough_tokens)[1],
                f"{int(tokens):,}", f"{self._own_request_tokens:,}",
            )
            self.record_event("preflight_deferred_to_real_usage",
                              reported_tokens=int(rough_tokens), tokens=int(tokens),
                              basis=self._pressure(rough_tokens)[1],
                              real_prompt_tokens=self.last_real_prompt_tokens,
                              own_tokens=self._own_request_tokens)
        return True

    # -- Compaction trigger ------------------------------------------------

    def should_compress(self, prompt_tokens: int = None) -> bool:
        """Only fire once a handoff is ready to swap, or as a hard safety net.

        Crucially this returns False while the agent is still authoring — the
        directive lives in the system prompt (hook.py), not here, so the agent
        keeps its full transcript and tools until it finalizes.
        """
        # Record what the host handed us before anything else - the soft nudge
        # in hook.py depends on a fresh number, even when we return False. But
        # corroborate it first: this argument is a ROUGH preflight estimate on
        # some calls and the provider's real count on others, and only the
        # latter is ever "measured" (see _pressure).
        self._host_consulted = True
        tokens, basis = self._pressure(prompt_tokens)
        if prompt_tokens:
            self.last_preflight_tokens = tokens
            self.last_preflight_basis = basis

        if not self.store or not self.session_id:
            return False

        phase = self.store.get_phase(self.session_id)
        if phase == PHASE_READY:
            return True

        if not tokens:
            tokens, basis = self.last_prompt_tokens, BASIS_PROVIDER
            tokens = max(0, tokens)
        if tokens and self.context_length and tokens >= self.context_length * self.hard_ratio:
            logger.warning(
                "Handoff: hard threshold reached in phase '%s' without a ready "
                "handoff (%s tokens, %s); safety fallback will truncate.",
                phase, f"{tokens:,}", basis,
            )
            return True

        # Detect the soft crossing HERE as well as in the system_prompt hook.
        # The hook runs once per user turn; this runs before every API call,
        # including mid tool-loop, so a long turn that crosses soft is recorded
        # the moment it happens. Delivery still waits for the next turn's
        # pre_llm_call (no fork-safe mid-turn injection channel exists yet).
        # Background-review forks run with compression disabled, so the host
        # never calls this on a fork - its context cannot trip the parent.
        if (phase == PHASE_NORMAL and tokens and tokens > 0 and self.context_length
                and tokens >= self.context_length * self.soft_ratio):
            self.request_handoff(tokens / self.context_length, "should_compress",
                                 tokens, basis=basis,
                                 reported=int(prompt_tokens or 0))
        return False

    def has_content_to_compress(self, messages: List[Dict[str, Any]]) -> bool:
        # Manual /compress: only meaningful once a handoff is ready.
        if not self.store or not self.session_id:
            return False
        return self.store.get_phase(self.session_id) == PHASE_READY

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
        focus_topic: str = None,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        """Swap in a ready handoff; otherwise defer or truncate.

        The deferral exists because of gateway session hygiene. It runs before
        the agent turn at a HARDCODED 85% of the window (not configurable, not
        routed through should_compress), on a throwaway agent bound to the live
        session_id, and calls this method directly. Our soft trigger is also
        0.85 but is only checked once the turn starts — so hygiene always won,
        and every crossing became a lossy truncation with no handoff ever
        requested (Forge, Sep-Oct 2026: 37 hygiene truncations, 0 requests).

        So: when an out-of-turn caller (this copy was never consulted via
        should_compress) asks below the hard net, return the transcript
        unchanged and request the handoff. Hermes treats an unchanged return as
        a clean no-op ("no progress", no rewrite), and the turn that follows
        delivers the instruction via pre_llm_call. In-turn callers keep the
        truncation: they are context-overflow recovery, where a no-op ends the
        turn with "cannot compress further". ``force`` (manual /compress) also
        keeps it — the operator asked for room now.
        """
        if not self.store or not self.session_id:
            logger.warning("Handoff: no store/session; returning messages unchanged")
            return messages

        phase = self.store.get_phase(self.session_id)
        if phase == PHASE_READY:
            swapped = self._swap_in_handoff(messages)
            if swapped is not None:
                return swapped
            # Authored file missing/empty — fall through to safety truncation.
            logger.warning("Handoff: phase 'ready' but document unusable; truncating")

        reported = int(current_tokens or 0)
        tokens, basis = self._pressure(reported, messages)
        if not tokens:
            tokens = self.last_preflight_tokens or max(0, self.last_prompt_tokens)
            basis = self.last_preflight_basis or BASIS_HOST_ESTIMATE
        caller = "in-turn" if self._host_consulted else ("manual" if force else "out-of-turn")
        if (caller == "out-of-turn" and phase != PHASE_READY and tokens
                and self.context_length
                and tokens < self.context_length * self.hard_ratio):
            # Hygiene also fires on message count alone
            # (compression.hygiene_hard_message_limit), at any token level. Keep
            # the transcript either way, but only ask for a handoff once soft
            # is reached — firing early costs the whole live context.
            usage = tokens / self.context_length
            if usage >= self.soft_ratio:
                self.request_handoff(usage, "host_compaction", tokens,
                                     basis=basis, reported=reported)
            requested = self.store.get_phase(self.session_id) == PHASE_AUTHORING
            logger.warning(
                "Handoff: deferred host compaction at %.0f%% (~%s/%s tokens) for %s "
                "— below the %.0f%% hard net, so the transcript is kept%s.",
                usage * 100, f"{tokens:,}", f"{self.context_length:,}",
                self.session_id, self.hard_ratio * 100,
                " and a self-handoff is requested" if requested else "",
            )
            self.record_event("host_compaction_deferred", tokens=tokens,
                              usage=round(usage, 4), phase=phase, basis=basis,
                              reported_tokens=reported,
                              handoff_requested=requested)
            return messages

        return self._safety_truncate(messages, tokens=tokens, caller=caller,
                                     basis=basis, reported=reported)

    def request_handoff(self, usage: float, source: str, tokens: int = 0,
                        session_id: Optional[str] = None,
                        basis: Optional[str] = None,
                        reported: int = 0) -> bool:
        """Move the session normal -> authoring; True if newly requested.

        Single entry point for every trigger (turn-start hook, should_compress,
        deferred host compaction) so each request is logged and ledgered the
        same way. Usage and urgency refresh even when already authoring.
        """
        sid = session_id or self.session_id
        if not self.store or not sid:
            return False
        phase = self.store.get_phase(sid)
        if phase == PHASE_READY:
            return False
        if basis is None:
            basis = {"host_compaction": BASIS_HOST_ESTIMATE}.get(
                source, BASIS_ENGINE_ESTIMATE)
        self.store.set_usage(sid, usage, tokens=tokens,
                             context_length=self.context_length, basis=basis,
                             soft=self.soft_ratio, hard=self.hard_ratio,
                             reported=reported)
        self.store.set_urgent(sid, usage >= self.urgent_ratio)
        if phase == PHASE_AUTHORING:
            return False
        self.store.set_phase(sid, PHASE_AUTHORING)
        logger.info(
            "Handoff: context at %.0f%% (~%s/%s tokens) for %s — requesting a "
            "self-handoff via %s (instruction injected into the user turn).",
            usage * 100, f"{tokens:,}" if tokens else "?",
            f"{self.context_length:,}", sid, source,
        )
        # A request soon after a swap means the reset did not buy much room (the
        # Forge chain: handoff 2 only ~15 minutes after handoff 1). Record how
        # soon so a chain is visible in events.jsonl, not inferred from logs.
        chain: Dict[str, Any] = {}
        swaps = self.store.get_swap_count(sid)
        swapped_at = self.store.get_swapped_at(sid)
        if swaps and swapped_at:
            chain = {"swaps_in_lineage": swaps,
                     "seconds_since_swap": int(time.time() - swapped_at)}
        self.record_event("handoff_requested", source=source, tokens=tokens,
                          usage=round(usage, 4), session_id=sid, basis=basis,
                          reported_tokens=int(reported or 0),
                          real_prompt_tokens=self.last_real_prompt_tokens,
                          own_tokens=self._own_request_tokens, **chain)
        return True

    def record_event(self, event: str, **fields: Any) -> None:
        """Append one line to ``<handoff_dir>/events.jsonl``. Never raises.

        The durable record of what the engine did. State is in-process and
        lost on restart, and the WARNING log is buried in a busy agent.log —
        the Forge diagnosis started from a stale ``handoff_state.db`` left by
        the pre-in-process version, which this engine no longer writes. Counts
        and phases only; never transcript or handoff content.
        """
        if not self.handoff_dir:
            return
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "session_id": self.session_id, "event": event,
               "context_length": self.context_length, **fields}
        try:
            with open(self.handoff_dir / "events.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError as exc:
            logger.debug("Handoff: could not record event %s: %s", event, exc)

    def _swap_in_handoff(self, messages: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        path_str = self.store.get_handoff_path(self.session_id)
        path = Path(path_str) if path_str else self.handoff_path_for(self.session_id)

        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("Handoff: could not read %s: %s", path, exc)
            return None
        if not content:
            return None

        # Record the handoff for the layered prior before consuming it, so the
        # NEXT authoring cycle can carry still-relevant state forward.
        self.store.set_last_handoff(self.session_id, content)

        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        tail = self._recent_tail(non_system)
        tail_tokens = sum(_message_tokens(m) for m in tail)

        handoff_user = {
            "role": "user",
            "content": (
                f"{SWAP_MARKER}\n\n"
                "The previous session reached its context limit. Rather than a "
                "lossy summary, your predecessor (you) wrote the handoff below. "
                "Treat it as ground truth, orient yourself, and continue the "
                "work from here.\n\n"
                "---\n\n"
                f"{content}"
            ),
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        }

        # Assemble [system] + [handoff seed] + [recent raw tail], preserving
        # user/assistant alternation. If the retained tail starts with a user
        # message, fold the handoff seed into it rather than doubling up user
        # turns; otherwise prepend the seed as its own user message.
        if tail and tail[0].get("role") == "user":
            first = dict(tail[0])
            first["content"] = f"{handoff_user['content']}\n\n{first.get('content', '')}"
            if COMPRESSED_SUMMARY_METADATA_KEY not in first:
                first[COMPRESSED_SUMMARY_METADATA_KEY] = True
            seed = system_msgs + [first] + tail[1:]
        else:
            seed = system_msgs + [handoff_user] + tail

        # Reset the machine: back to normal, forget the consumed document.
        self.store.set_phase(self.session_id, PHASE_NORMAL)
        self.store.set_handoff_path(self.session_id, None)
        self.store.mark_swapped(self.session_id)
        self.compression_count += 1
        self._forget_size_readings()
        # The size the NEW session starts at, measured on what we return — the
        # number that decides whether this reset bought any room.
        seed_tokens = sum(_message_tokens(m) for m in seed if m.get("role") != "system")
        self.record_event("handoff_swapped", messages_in=len(messages),
                          messages_out=len(seed), handoff_chars=len(content),
                          tail_messages=len(tail), tail_tokens=tail_tokens,
                          tail_dropped=len(non_system) - len(tail),
                          seed_tokens=seed_tokens)

        logger.info(
            "Handoff: swapped %d messages for authored handoff (%d chars) from %s "
            "keeping %d recent messages (~%s tokens, budget %s); new session "
            "starts at ~%s tokens excluding system prompt and tools",
            len(messages), len(content), path, len(tail), f"{tail_tokens:,}",
            f"{self.keep_tokens:,}", f"{seed_tokens:,}",
        )
        return seed

    def _recent_tail(self, non_system: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep the most recent messages up to ``keep_tokens`` verbatim.

        Mirrors opencode's `keep.tokens`: the handoff swap does not discard the
        entire transcript — the tail's immediate working context crosses the
        reset raw. The budget is a real ceiling: sizes come from
        ``_message_tokens`` (every field the provider receives, not just
        ``content``) and a single message over budget is dropped rather than
        force-kept. The handoff document is what carries state; a tail that
        restarts the session at or above soft only chains the next handoff.
        """
        return self._bounded_tail(non_system, self.keep_tokens)

    def _bounded_tail(self, non_system: List[Dict[str, Any]], budget: int,
                      max_messages: Optional[int] = None) -> List[Dict[str, Any]]:
        """Newest-first walk under a token budget, cut at a clean boundary."""
        if not budget:
            return []
        total = 0
        kept: List[Dict[str, Any]] = []
        for msg in reversed(non_system):
            if max_messages is not None and len(kept) >= max_messages:
                break
            cost = _message_tokens(msg)
            if total + cost > budget:
                break
            kept.append(msg)
            total += cost
        kept.reverse()
        return _drop_orphan_tool_results(kept)

    def _safety_truncate(
        self,
        messages: List[Dict[str, Any]],
        tokens: int = 0,
        caller: str = "in-turn",
        basis: Optional[str] = None,
        reported: int = 0,
    ) -> List[Dict[str, Any]]:
        """Last-resort head/tail keep so the window is never exceeded."""
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        # Bound by size as well as count: the last N messages can themselves be
        # most of a million tokens (Forge: 16 messages -> 17, ~1.19M, no relief).
        budget = int(self.context_length * TRUNCATION_TAIL_RATIO) or DEFAULT_KEEP_TOKENS * 10
        tail = self._bounded_tail(non_system, budget,
                                  max_messages=max(1, self.protect_last_n))

        # A truncation that keeps (nearly) everything is a rotation, not relief:
        # it mints a new session id, frees nothing, and the next message does it
        # again. Forge, 2026-10-05: 16->17, 18->17, 17->17 messages, a session
        # rotated every few seconds on a phantom ~1.18M figure (the messages
        # measured ~10k) until the gateway lost track of the live id and
        # "session storage could not be written". Where the messages are
        # already within the retained budget there is nothing to chop - leave
        # the transcript alone and say so.
        total_tokens = sum(_message_tokens(m) for m in non_system)
        kept_tokens = sum(_message_tokens(m) for m in tail)
        if total_tokens and kept_tokens >= total_tokens * NO_RELIEF_KEPT_FRACTION:
            logger.warning(
                "Handoff: NOT truncating %s - %d messages measure ~%s tokens "
                "and the truncation would keep ~%s of them; the host's figure "
                "(~%s, %s) is not something chopping messages can relieve.",
                self.session_id, len(messages), f"{total_tokens:,}",
                f"{kept_tokens:,}", f"{tokens:,}" if tokens else "unknown",
                basis or "unknown",
            )
            self.record_event("truncation_skipped_no_relief", tokens=tokens,
                              caller=caller, basis=basis,
                              reported_tokens=reported, own_tokens=total_tokens,
                              kept_tokens=kept_tokens, messages_in=len(messages))
            return messages

        # Say which failure this is. The old note always claimed a handoff
        # "was not completed", which read as an agent that ignored a request
        # even when no request had ever been made — exactly the misreading
        # that hid the hygiene pre-emption.
        requested = self.store.get_phase(self.session_id) == PHASE_AUTHORING
        if requested:
            why = ("A handoff was requested but not finalized before the context "
                   "limit was reached.")
        else:
            why = ("The context limit was reached before a handoff was ever "
                   "requested (no handoff instruction reached this session).")
        oversized = ""
        if len(tail) < min(len(non_system), max(1, self.protect_last_n)):
            oversized = (" Some recent messages were too large to keep and were "
                         "dropped too.")
        note = {
            "role": "user",
            "content": (
                f"[CONTEXT SAFETY TRUNCATION] {why} Older turns were dropped.{oversized} If you "
                "need continuity, write a handoff now and call finalize_handoff."
            ),
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        }

        # Reset phase so the next crossing starts the authoring flow cleanly.
        self.store.set_phase(self.session_id, PHASE_NORMAL)
        self.store.set_handoff_path(self.session_id, None)
        self.compression_count += 1
        self._forget_size_readings()

        result = system_msgs + [note] + tail
        tail_tokens = sum(_message_tokens(m) for m in tail)
        # Loud on purpose: this is the lossy path this plugin exists to avoid.
        # handoff_requested=False means the trigger never fired (look at who
        # called compress, and when); True means the agent did not convert.
        logger.warning(
            "Handoff: LOSSY SAFETY TRUNCATION for %s — %d messages -> %d "
            "(~%s tokens, %s caller, handoff %s). No authored handoff existed; "
            "context was chopped to the last %d messages, bounded to ~%s tokens "
            "(kept %d, ~%s tokens).",
            self.session_id, len(messages), len(result),
            f"{tokens:,}" if tokens else "unknown", caller,
            "requested but not finalized" if requested else "NEVER requested",
            self.protect_last_n, f"{budget:,}", len(tail), f"{tail_tokens:,}",
        )
        self.record_event("lossy_truncation", tokens=tokens, caller=caller,
                          basis=basis, reported_tokens=reported,
                          own_tokens=total_tokens,
                          handoff_requested=requested,
                          messages_in=len(messages), messages_out=len(result),
                          tail_messages=len(tail), tail_tokens=tail_tokens)
        return result

    # -- Tool surface: finalize_handoff ------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": FINALIZE_TOOL_NAME,
                "description": (
                    "Call this ONLY after you have finished writing your COMPLETE "
                    "handoff document to the path given in the handoff directive. "
                    "It resets the session into a fresh context seeded with that "
                    "handoff. After calling it, stop working and end your turn — do "
                    "not start new work, the transcript is about to be replaced."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "confirm": {
                            "type": "boolean",
                            "description": "Must be true to confirm the handoff document is complete.",
                        },
                        "path": {
                            "type": "string",
                            "description": (
                                "Absolute path of the handoff markdown file you wrote. "
                                "Optional — defaults to the path from the directive."
                            ),
                        },
                    },
                    "required": ["confirm"],
                },
            }
        ]

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs) -> str:
        if name != FINALIZE_TOOL_NAME:
            return json.dumps({"error": f"Unknown context engine tool: {name}"})
        if not self.store or not self.session_id:
            return json.dumps({"error": "Handoff engine has no active session."})
        if not args.get("confirm"):
            return json.dumps({
                "status": "not_confirmed",
                "message": "Set confirm=true once the handoff document is fully written.",
            })

        path_str = args.get("path") or self.store.get_handoff_path(self.session_id)
        path = Path(path_str) if path_str else self.handoff_path_for(self.session_id)

        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        if not content:
            return json.dumps({
                "status": "missing",
                "message": (
                    f"No non-empty handoff found at {path}. Write the complete "
                    "handoff document there first, then call finalize_handoff again."
                ),
            })

        self.store.set_handoff_path(self.session_id, str(path))
        self.store.set_phase(self.session_id, PHASE_READY)
        logger.info("Handoff: finalized for session %s at %s", self.session_id, path)
        self.record_event("handoff_finalized", path=str(path))
        return json.dumps({
            "status": "ready",
            "message": (
                "Handoff accepted. The session will now reset into a fresh context "
                "seeded with your handoff. Stop here and end your turn."
            ),
        })

    # -- Helpers -----------------------------------------------------------

    def handoff_path_for(self, session_id: str) -> Path:
        base = self.handoff_dir or (Path.home() / ".hermes" / "handoffs")
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id)
        return base / f"{safe}.md"
