"""In-process per-session handoff state.

The state is tiny — per session: a phase, the authored handoff's path, and a
swap counter — and it only needs to live for the span of a handoff inside a
running gateway. So it lives in a module-level dict guarded by a lock, not a
database.

Why a module global works despite Hermes deep-copying the engine per agent:
the copies (and the system_prompt hook) all import THIS module, so they share
this one ``_STATE`` dict. ``HandoffStore`` instances are stateless facades over
it — deep-copying one copies nothing that matters.

Deliberately NOT persistent: if the gateway restarts mid-handoff the state is
lost, which is harmless — just re-trigger ``/self-handoff``. The handoff
*documents* the agent writes are ordinary files and are unaffected.
"""

import threading
from typing import Dict, Optional

PHASE_NORMAL = "normal"
PHASE_AUTHORING = "authoring"
PHASE_READY = "ready"

_LOCK = threading.Lock()
_STATE: Dict[str, dict] = {}


def _entry(session_id: str) -> dict:
    e = _STATE.get(session_id)
    if e is None:
        e = {"phase": PHASE_NORMAL, "handoff_path": None, "swap_count": 0,
             "usage": 0.0, "urgent": False}
        _STATE[session_id] = e
    return e


class HandoffStore:
    """Stateless facade over the shared, in-process handoff-state dict."""

    def ensure_session(self, session_id: str) -> None:
        with _LOCK:
            _entry(session_id)

    def get_phase(self, session_id: str) -> str:
        with _LOCK:
            return _entry(session_id)["phase"]

    def set_phase(self, session_id: str, phase: str) -> None:
        with _LOCK:
            _entry(session_id)["phase"] = phase

    def get_handoff_path(self, session_id: str) -> Optional[str]:
        with _LOCK:
            return _entry(session_id)["handoff_path"]

    def set_handoff_path(self, session_id: str, path: Optional[str]) -> None:
        with _LOCK:
            _entry(session_id)["handoff_path"] = path

    def get_usage(self, session_id: str) -> float:
        """Context usage (0..1) recorded when authoring was triggered.

        Written by the system_prompt hook (which can see the engine) and read by
        the pre_llm_call hook (which cannot) to pick the urgency of the injected
        instruction.
        """
        with _LOCK:
            return _entry(session_id).get("usage", 0.0)

    def set_usage(self, session_id: str, usage: float) -> None:
        with _LOCK:
            _entry(session_id)["usage"] = usage

    def get_urgent(self, session_id: str) -> bool:
        """Whether the injected instruction should use the stop-now tier.

        Decided by the detection hook (which can see the engine's configured
        ``urgent_ratio``) and carried here because the delivery hook receives no
        ``agent`` and therefore cannot read the threshold itself.
        """
        with _LOCK:
            return bool(_entry(session_id).get("urgent", False))

    def set_urgent(self, session_id: str, urgent: bool) -> None:
        with _LOCK:
            _entry(session_id)["urgent"] = bool(urgent)

    def get_swap_count(self, session_id: str) -> int:
        with _LOCK:
            return _entry(session_id)["swap_count"]

    def increment_swap_count(self, session_id: str) -> None:
        with _LOCK:
            _entry(session_id)["swap_count"] += 1

    def reset(self, session_id: str) -> None:
        with _LOCK:
            _STATE[session_id] = {
                "phase": PHASE_NORMAL, "handoff_path": None,
                "swap_count": 0, "usage": 0.0, "urgent": False,
            }
