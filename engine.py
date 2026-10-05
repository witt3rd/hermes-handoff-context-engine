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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.context_engine import ContextEngine

from .state import HandoffStore, PHASE_NORMAL, PHASE_AUTHORING, PHASE_READY

logger = logging.getLogger(__name__)

DEFAULT_SOFT_RATIO = 0.85
DEFAULT_HARD_RATIO = 0.90
DEFAULT_PROTECT_LAST_N = 16
# Recent raw transcript retained verbatim across a handoff swap, so the
# successor has immediate working context (mirrors opencode's `keep.tokens`).
DEFAULT_KEEP_TOKENS = 8000


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


class HandoffContextEngine(ContextEngine):
    """Context engine that swaps the transcript for an agent-authored handoff."""

    def __init__(self):
        self._name = "handoff"

        # -- Token state read directly by run_agent.py (ABC contract) --------
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.threshold_tokens = 0
        self.context_length = 0
        self.compression_count = 0

        # The AUTHORITATIVE live request size, captured from the preflight
        # number the host passes to should_compress(). This is the same figure
        # Hermes uses for its own "Pre-API compression: ~N tokens >= threshold"
        # decision, so it is exact where our own estimates are not:
        # last_prompt_tokens lags a turn, and estimate_messages_tokens_rough()
        # badly under-counts structured tool-result blocks. The soft-threshold
        # nudge (hook.py) reads THIS.
        self.last_preflight_tokens = 0

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

        # -- Session-scoped resources ---------------------------------------
        self.store: Optional[HandoffStore] = None
        self.session_id: Optional[str] = None
        self.hermes_home: Optional[Path] = None
        self.handoff_dir: Optional[Path] = None

    @property
    def name(self) -> str:
        return self._name

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
        self.compression_count = self.store.get_swap_count(session_id)

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        self.session_id = None

    def on_session_reset(self) -> None:
        if self.store and self.session_id:
            self.store.reset(self.session_id)
        self.last_prompt_tokens = 0
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
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
        self.last_completion_tokens = usage.get("completion_tokens", 0)
        self.last_total_tokens = usage.get("total_tokens", 0)

    # -- Compaction trigger ------------------------------------------------

    def should_compress(self, prompt_tokens: int = None) -> bool:
        """Only fire once a handoff is ready to swap, or as a hard safety net.

        Crucially this returns False while the agent is still authoring — the
        directive lives in the system prompt (hook.py), not here, so the agent
        keeps its full transcript and tools until it finalizes.
        """
        # Capture the host's authoritative live request size before anything
        # else — the soft-threshold nudge in hook.py depends on it, and this is
        # the only place Hermes hands it to us. Record it even when we go on to
        # return False (the common case, which is exactly when the nudge needs
        # a fresh number).
        self._host_consulted = True
        if prompt_tokens:
            self.last_preflight_tokens = prompt_tokens

        if not self.store or not self.session_id:
            return False

        phase = self.store.get_phase(self.session_id)
        if phase == PHASE_READY:
            return True

        tokens = prompt_tokens or self.last_prompt_tokens
        if tokens and self.context_length and tokens >= self.context_length * self.hard_ratio:
            logger.warning(
                "Handoff: hard threshold reached in phase '%s' without a ready "
                "handoff; safety fallback will truncate.", phase,
            )
            return True

        # Detect the soft crossing HERE as well as in the system_prompt hook.
        # The hook runs once per user turn; this runs before every API call,
        # including mid tool-loop, so a long turn that crosses soft is recorded
        # the moment it happens. Delivery still waits for the next turn's
        # pre_llm_call (no fork-safe mid-turn injection channel exists yet).
        # Background-review forks run with compression disabled, so the host
        # never calls this on a fork — its context cannot trip the parent.
        if (phase == PHASE_NORMAL and tokens and tokens > 0 and self.context_length
                and tokens >= self.context_length * self.soft_ratio):
            self.request_handoff(tokens / self.context_length, "should_compress", tokens)
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

        tokens = current_tokens or self.last_preflight_tokens or max(0, self.last_prompt_tokens)
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
                self.request_handoff(usage, "host_compaction", tokens)
            requested = self.store.get_phase(self.session_id) == PHASE_AUTHORING
            logger.warning(
                "Handoff: deferred host compaction at %.0f%% (~%s/%s tokens) for %s "
                "— below the %.0f%% hard net, so the transcript is kept%s.",
                usage * 100, f"{tokens:,}", f"{self.context_length:,}",
                self.session_id, self.hard_ratio * 100,
                " and a self-handoff is requested" if requested else "",
            )
            self.record_event("host_compaction_deferred", tokens=tokens,
                              usage=round(usage, 4), phase=phase,
                              handoff_requested=requested)
            return messages

        return self._safety_truncate(messages, tokens=tokens, caller=caller)

    def request_handoff(self, usage: float, source: str, tokens: int = 0,
                        session_id: Optional[str] = None) -> bool:
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
        self.store.set_usage(sid, usage)
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
        self.record_event("handoff_requested", source=source, tokens=tokens,
                          usage=round(usage, 4), session_id=sid)
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
        tail = self._recent_tail([m for m in messages if m.get("role") != "system"])

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
        self.store.increment_swap_count(self.session_id)
        self.compression_count += 1
        self.record_event("handoff_swapped", messages_in=len(messages),
                          messages_out=len(seed), handoff_chars=len(content))

        logger.info(
            "Handoff: swapped %d messages for authored handoff (%d chars) from %s "
            "keeping %d recent messages (~%s tokens)",
            len(messages), len(content), path, len(tail),
            f"{self.keep_tokens:,}" if self.keep_tokens else "none",
        )
        return seed

    def _recent_tail(self, non_system: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep the most recent messages up to ``keep_tokens`` verbatim.

        Mirrors opencode's `keep.tokens`: the handoff swap no longer discards
        the entire transcript — the tail's immediate working context (the last
        tool round, the in-flight edit) crosses the reset raw, so the successor
        is not groping for where it was. At least one message is always kept,
        even if a single turn exceeds the budget.
        """
        if not self.keep_tokens:
            return []
        total = 0
        kept = []
        for msg in reversed(non_system):
            cost = self._estimate_tokens(msg)
            if kept and total + cost > self.keep_tokens:
                break
            kept.append(msg)
            total += cost
        kept.reverse()
        return kept

    def _estimate_tokens(self, msg: Dict[str, Any]) -> int:
        """Rough char-based token estimate for a single message dict."""
        content = msg.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        return max(1, len(text) // 4)

    def _safety_truncate(
        self,
        messages: List[Dict[str, Any]],
        tokens: int = 0,
        caller: str = "in-turn",
    ) -> List[Dict[str, Any]]:
        """Last-resort head/tail keep so the window is never exceeded."""
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        tail = non_system[-max(1, self.protect_last_n):]

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
        note = {
            "role": "user",
            "content": (
                f"[CONTEXT SAFETY TRUNCATION] {why} Older turns were dropped. If you "
                "need continuity, write a handoff now and call finalize_handoff."
            ),
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        }

        # Reset phase so the next crossing starts the authoring flow cleanly.
        self.store.set_phase(self.session_id, PHASE_NORMAL)
        self.store.set_handoff_path(self.session_id, None)
        self.compression_count += 1

        result = system_msgs + [note] + tail
        # Loud on purpose: this is the lossy path this plugin exists to avoid.
        # handoff_requested=False means the trigger never fired (look at who
        # called compress, and when); True means the agent did not convert.
        logger.warning(
            "Handoff: LOSSY SAFETY TRUNCATION for %s — %d messages -> %d "
            "(~%s tokens, %s caller, handoff %s). No authored handoff existed; "
            "context was chopped to the last %d messages.",
            self.session_id, len(messages), len(result),
            f"{tokens:,}" if tokens else "unknown", caller,
            "requested but not finalized" if requested else "NEVER requested",
            self.protect_last_n,
        )
        self.record_event("lossy_truncation", tokens=tokens, caller=caller,
                          handoff_requested=requested,
                          messages_in=len(messages), messages_out=len(result))
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
