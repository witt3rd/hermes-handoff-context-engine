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
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.context_engine import ContextEngine

from .state import HandoffStore, PHASE_NORMAL, PHASE_AUTHORING, PHASE_READY

logger = logging.getLogger(__name__)

DEFAULT_SOFT_RATIO = 0.85
DEFAULT_HARD_RATIO = 0.90
DEFAULT_PROTECT_LAST_N = 16


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
        self.threshold_percent = self.hard_ratio

        logger.info(
            "Handoff: thresholds soft=%.2f urgent=%.2f hard=%.2f protect_last_n=%d%s",
            self.soft_ratio, self.urgent_ratio, self.hard_ratio, self.protect_last_n,
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
    ) -> List[Dict[str, Any]]:
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

        return self._safety_truncate(messages)

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

        seed: List[Dict[str, Any]] = [m for m in messages if m.get("role") == "system"]
        seed.append({
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
        })

        # Reset the machine: back to normal, forget the consumed document.
        self.store.set_phase(self.session_id, PHASE_NORMAL)
        self.store.set_handoff_path(self.session_id, None)
        self.store.increment_swap_count(self.session_id)
        self.compression_count += 1

        logger.info(
            "Handoff: swapped %d messages for authored handoff (%d chars) from %s",
            len(messages), len(content), path,
        )
        return seed

    def _safety_truncate(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Last-resort head/tail keep so the window is never exceeded."""
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        tail = non_system[-max(1, self.protect_last_n):]

        note = {
            "role": "user",
            "content": (
                "[CONTEXT SAFETY TRUNCATION] The context limit was reached before "
                "a handoff document was completed. Older turns were dropped. If you "
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
        # If you see this, the handoff never got authored — investigate why the
        # soft-threshold nudge didn't convert.
        logger.warning(
            "Handoff: LOSSY SAFETY TRUNCATION for %s — %d messages -> %d "
            "(preflight ~%s tokens). No authored handoff existed; context was "
            "chopped to the last %d messages.",
            self.session_id, len(messages), len(result),
            f"{self.last_preflight_tokens:,}" if self.last_preflight_tokens else "unknown",
            self.protect_last_n,
        )
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
