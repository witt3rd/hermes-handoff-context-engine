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

        # -- Thresholds ------------------------------------------------------
        # soft: ask the agent to author its handoff while it still has room and
        #       full tool access. hard: safety net so we never blow the window.
        self.soft_ratio = 0.60
        self.hard_ratio = 0.80
        # Host preflight math uses threshold_percent; point it at the hard net.
        self.threshold_percent = self.hard_ratio

        # We fully own the returned message list, so head/tail protection is a
        # no-op for the handoff swap; it only matters for the safety fallback.
        self.protect_first_n = 0
        self.protect_last_n = 8

        # -- Session-scoped resources ---------------------------------------
        self.store: Optional[HandoffStore] = None
        self.session_id: Optional[str] = None
        self.hermes_home: Optional[Path] = None
        self.handoff_dir: Optional[Path] = None

    @property
    def name(self) -> str:
        return self._name

    # -- Lifecycle ---------------------------------------------------------

    def on_session_start(self, session_id: str, **kwargs) -> None:
        self.session_id = session_id

        home_arg = kwargs.get("hermes_home")
        self.hermes_home = Path(home_arg) if home_arg else Path.home() / ".hermes"

        base_dir = self.hermes_home
        if kwargs.get("profile"):
            base_dir = self.hermes_home / "profiles" / kwargs["profile"]
        base_dir.mkdir(parents=True, exist_ok=True)

        self.handoff_dir = base_dir / "handoffs"
        self.handoff_dir.mkdir(parents=True, exist_ok=True)

        self.store = HandoffStore(base_dir / "handoff_state.db")
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
        return system_msgs + [note] + tail

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
