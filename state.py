"""SQLite-backed per-session handoff state.

The store holds ONLY a filesystem path — never a live connection — so the
engine that owns it stays cheaply deep-copyable. Hermes deep-copies the
context engine per child agent (agent_init.py), and every copy points at the
same DB file, making this the single shared source of truth for a session's
handoff phase across all engine copies and the ``system_prompt`` hook.

Phases
------
``normal``    — business as usual; nothing to hand off yet.
``authoring`` — the agent has been told to write its handoff and is doing so
                (with full tool access); the directive is being injected every
                turn until it finalizes.
``ready``     — the agent called ``finalize_handoff``; the next ``compress()``
                will swap the transcript for the authored document.
"""

import sqlite3
from pathlib import Path
from typing import Optional

PHASE_NORMAL = "normal"
PHASE_AUTHORING = "authoring"
PHASE_READY = "ready"


class HandoffStore:
    """Manages per-session handoff phase and the authored document's path."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS handoff_state (
                    session_id   TEXT PRIMARY KEY,
                    phase        TEXT DEFAULT 'normal',
                    handoff_path TEXT,
                    swap_count   INTEGER DEFAULT 0
                );
                """
            )
            conn.commit()

    def ensure_session(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO handoff_state (session_id) VALUES (?);",
                (session_id,),
            )
            conn.commit()

    def get_phase(self, session_id: str) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT phase FROM handoff_state WHERE session_id = ?;",
                (session_id,),
            ).fetchone()
            return row["phase"] if row and row["phase"] else PHASE_NORMAL

    def set_phase(self, session_id: str, phase: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO handoff_state (session_id, phase) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET phase = excluded.phase;",
                (session_id, phase),
            )
            conn.commit()

    def get_handoff_path(self, session_id: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT handoff_path FROM handoff_state WHERE session_id = ?;",
                (session_id,),
            ).fetchone()
            return row["handoff_path"] if row else None

    def set_handoff_path(self, session_id: str, path: Optional[str]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO handoff_state (session_id, handoff_path) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET handoff_path = excluded.handoff_path;",
                (session_id, path),
            )
            conn.commit()

    def get_swap_count(self, session_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT swap_count FROM handoff_state WHERE session_id = ?;",
                (session_id,),
            ).fetchone()
            return row["swap_count"] if row and row["swap_count"] else 0

    def increment_swap_count(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE handoff_state SET swap_count = swap_count + 1 WHERE session_id = ?;",
                (session_id,),
            )
            conn.commit()

    def reset(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE handoff_state SET phase = 'normal', handoff_path = NULL, "
                "swap_count = 0 WHERE session_id = ?;",
                (session_id,),
            )
            conn.commit()
