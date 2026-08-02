"""Append-only audit timeline. Every mutation elsewhere in core records one.

append() only executes the INSERT; the calling mutation owns the transaction
so the event commits atomically with the change it describes.
"""

from __future__ import annotations

import sqlite3

from ..util import to_json, utcnow
from .models import Event

ACTORS = frozenset({"claude", "owner", "browserd", "worker", "admin"})


def append(
    conn: sqlite3.Connection,
    *,
    actor: str,
    kind: str,
    job_id: int | None = None,
    payload: dict | None = None,
) -> int:
    if actor not in ACTORS:
        raise ValueError(f"unknown actor {actor!r}, expected one of {sorted(ACTORS)}")
    cursor = conn.execute(
        "INSERT INTO event(job_id, ts, actor, kind, payload) VALUES (?, ?, ?, ?, ?)",
        (job_id, utcnow(), actor, kind, to_json(payload)),
    )
    return cursor.lastrowid


def timeline(conn: sqlite3.Connection, job_id: int) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM event WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()
    return [Event.from_row(row) for row in rows]
