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
import time
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
             "usage": 0.0, "urgent": False, "last_handoff": None,
             "usage_detail": None, "swapped_at": None}
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
            e = _entry(session_id)
            e["phase"] = phase
            if phase == PHASE_NORMAL:
                # A new cycle must not inherit the last cycle's measured figure.
                e["usage_detail"] = None

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

    def set_usage(self, session_id: str, usage: float, tokens: int = 0,
                  context_length: int = 0, basis: Optional[str] = None,
                  soft: float = 0.0, hard: float = 0.0,
                  reported: int = 0) -> None:
        """Record how full the context is, and where that number came from.

        ``basis`` is ``"provider_reported"`` (the API's own prompt_tokens -
        the only truly measured figure), ``"host_estimate"`` (a rough or stored
        figure from the host: preflight estimate, gateway hygiene) or
        ``"engine_estimate"`` (ours, or the host's figure capped by what the
        messages measure). ``reported`` is the host's raw figure when it
        differs, so the text can show both. A provider-reported figure is never
        displaced by an estimate: the instruction text must not quietly swap the
        real number for a cruder one.
        """
        with _LOCK:
            e = _entry(session_id)
            old = e.get("usage_detail")
            if (old and old.get("basis") == "provider_reported"
                    and basis != "provider_reported"):
                return
            e["usage"] = usage
            e["usage_detail"] = {"tokens": int(tokens or 0),
                                 "context_length": int(context_length or 0),
                                 "basis": basis, "soft": soft, "hard": hard,
                                 "reported": int(reported or 0)}

    def get_usage_detail(self, session_id: str) -> dict:
        """``{usage, tokens, context_length, basis, soft, hard}`` for the text."""
        with _LOCK:
            e = _entry(session_id)
            return {"usage": e.get("usage", 0.0), "tokens": 0, "context_length": 0,
                    "basis": None, "soft": 0.0, "hard": 0.0,
                    **(e.get("usage_detail") or {})}

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

    def get_last_handoff(self, session_id: str) -> Optional[str]:
        """The content of the handoff from the most recent swap.

        Feeds the layered-prior lift: when the agent next authors a handoff, it
        receives this as its predecessor document so still-relevant objectives,
        decisions, and work carry forward across swaps instead of starting cold.
        """
        with _LOCK:
            return _entry(session_id).get("last_handoff")

    def set_last_handoff(self, session_id: str, content: Optional[str]) -> None:
        with _LOCK:
            _entry(session_id)["last_handoff"] = content

    def get_swapped_at(self, session_id: str) -> Optional[float]:
        with _LOCK:
            return _entry(session_id).get("swapped_at")

    def mark_swapped(self, session_id: str) -> None:
        with _LOCK:
            e = _entry(session_id)
            e["swap_count"] += 1
            e["swapped_at"] = time.time()

    def inherit(self, old_session_id: str, new_session_id: str) -> None:
        """Carry lineage state across a host session-id rotation.

        Hermes mints a new session_id on every compaction (swap or truncation)
        and tells the engine via ``on_session_start(boundary_reason=
        "compression", old_session_id=...)``. Everything keyed by session_id
        that must outlive the swap — the layered prior, the swap count and the
        swap time used to spot chained handoffs — moves with it. Phase, usage
        and urgency deliberately do not: the new session starts normal.
        """
        if old_session_id == new_session_id:
            return
        with _LOCK:
            old = _STATE.get(old_session_id)
            if old is None:
                return
            new = _entry(new_session_id)
            for key in ("last_handoff", "swapped_at"):
                if old.get(key) is not None:
                    new[key] = old[key]
            new["swap_count"] = max(new["swap_count"], old["swap_count"])

    def set_real_reading(self, session_id: str, prompt_tokens: int, own_tokens: int) -> None:
        """Remember the provider's last prompt size for this session, paired with
        our own size of the same request. The gateway builds a fresh engine per
        message, so without this every message starts blind and falls back to the
        host's rough estimate (which under-counted Augur's 289K session as 138K)."""
        with _LOCK:
            _entry(session_id)["real_reading"] = (int(prompt_tokens), int(own_tokens))

    def get_real_reading(self, session_id: str):
        with _LOCK:
            return _entry(session_id).get("real_reading")

    def clear_real_reading(self, session_id: str) -> None:
        with _LOCK:
            _entry(session_id).pop("real_reading", None)

    def reset(self, session_id: str) -> None:
        with _LOCK:
            _STATE[session_id] = {
                "phase": PHASE_NORMAL, "handoff_path": None,
                "swap_count": 0, "usage": 0.0, "urgent": False,
                "last_handoff": None, "usage_detail": None,
                "swapped_at": None,
            }
