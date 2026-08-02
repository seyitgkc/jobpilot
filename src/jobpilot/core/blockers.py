"""Blocker rows: anything the system cannot resolve on its own.

A job is blocked iff it has blockers with status open or answered; job.status
keeps the substantive state underneath.
"""

from __future__ import annotations

import sqlite3

from ..util import to_json, utcnow
from . import events, repo
from .models import Blocker

KINDS = frozenset(
    {
        "question_personal",
        "question_subjective",
        "missing_answer",
        "captcha",
        "login_required",
        "extraction_failed",
        "drift",
        "workday_manual",
        "submit_approval",
        "resume_approval",
        "session_expired",
        "other",
    }
)


def raise_blocker(
    conn: sqlite3.Connection,
    job_id: int,
    kind: str,
    actor: str,
    *,
    question: str | None = None,
    field_spec: dict | None = None,
    page_context: dict | None = None,
    proposed_action: dict | None = None,
    screenshot_artifact_id: int | None = None,
) -> Blocker:
    if kind not in KINDS:
        raise ValueError(f"unknown blocker kind {kind!r}")
    repo.get_job(conn, job_id)
    with conn:
        cursor = conn.execute(
            "INSERT INTO blocker(job_id, kind, question, field_spec, page_context,"
            " proposed_action, screenshot_artifact_id, status, raised_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (
                job_id,
                kind,
                question,
                to_json(field_spec),
                to_json(page_context),
                to_json(proposed_action),
                screenshot_artifact_id,
                utcnow(),
            ),
        )
        blocker_id = cursor.lastrowid
        events.append(
            conn,
            actor=actor,
            kind="blocker_raised",
            job_id=job_id,
            payload={"blocker_id": blocker_id, "kind": kind, "question": question},
        )
    return get_blocker(conn, blocker_id)


def get_blocker(conn: sqlite3.Connection, blocker_id: int) -> Blocker:
    row = conn.execute("SELECT * FROM blocker WHERE id = ?", (blocker_id,)).fetchone()
    if row is None:
        raise repo.NotFound(f"blocker {blocker_id}")
    return Blocker.from_row(row)


def list_open(conn: sqlite3.Connection, job_id: int | None = None) -> list[Blocker]:
    if job_id is None:
        rows = conn.execute(
            "SELECT * FROM blocker WHERE status IN ('open', 'answered') ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM blocker WHERE job_id = ? AND status IN ('open', 'answered')"
            " ORDER BY id",
            (job_id,),
        ).fetchall()
    return [Blocker.from_row(row) for row in rows]


def is_blocked(conn: sqlite3.Connection, job_id: int) -> bool:
    return len(list_open(conn, job_id)) > 0
