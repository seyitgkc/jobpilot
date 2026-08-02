"""Thin repository layer over SQLite. Readable SQL, no ORM.

Status changes are excluded on purpose: job.status is written only by
core.state.transition().
"""

from __future__ import annotations

import sqlite3

from ..util import utcnow
from . import events
from .models import Application, Artifact, Job


class NotFound(Exception):
    pass


def create_job(
    conn: sqlite3.Connection,
    url: str,
    actor: str,
    *,
    canonical_url: str | None = None,
    company: str | None = None,
    title: str | None = None,
    board: str | None = None,
    req_id: str | None = None,
    workday_flag: bool = False,
) -> Job:
    now = utcnow()
    with conn:
        cursor = conn.execute(
            "INSERT INTO job(url, canonical_url, company, title, board, req_id,"
            " status, workday_flag, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'captured', ?, ?, ?)",
            (url, canonical_url, company, title, board, req_id, int(workday_flag), now, now),
        )
        job_id = cursor.lastrowid
        events.append(
            conn,
            actor=actor,
            kind="job_created",
            job_id=job_id,
            payload={"url": url, "board": board, "workday_flag": workday_flag},
        )
    return get_job(conn, job_id)


def get_job(conn: sqlite3.Connection, job_id: int) -> Job:
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise NotFound(f"job {job_id}")
    return Job.from_row(row)


def list_jobs(conn: sqlite3.Connection, status: str | None = None) -> list[Job]:
    if status is None:
        rows = conn.execute("SELECT * FROM job ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM job WHERE status = ? ORDER BY id", (status,)
        ).fetchall()
    return [Job.from_row(row) for row in rows]


def set_job_gates(
    conn: sqlite3.Connection,
    job_id: int,
    actor: str,
    *,
    resume_glance_gate: str | None = None,
    submit_gate: str | None = None,
) -> Job:
    """Set per-job gate overrides; values are inherit|on|off."""
    job = get_job(conn, job_id)
    updates = {}
    if resume_glance_gate is not None:
        updates["resume_glance_gate"] = resume_glance_gate
    if submit_gate is not None:
        updates["submit_gate"] = submit_gate
    for value in updates.values():
        if value not in ("inherit", "on", "off"):
            raise ValueError(f"gate value must be inherit|on|off, got {value!r}")
    if not updates:
        return job
    assignments = ", ".join(f"{column} = ?" for column in updates)
    with conn:
        conn.execute(
            f"UPDATE job SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), utcnow(), job_id),
        )
        events.append(
            conn, actor=actor, kind="gates_changed", job_id=job_id, payload=updates
        )
    return get_job(conn, job_id)


def update_job_capture(
    conn: sqlite3.Connection,
    job_id: int,
    actor: str,
    *,
    canonical_url: str | None,
    company: str | None,
    title: str | None,
    board: str | None,
    req_id: str | None,
    workday_flag: bool,
) -> Job:
    get_job(conn, job_id)
    with conn:
        conn.execute(
            "UPDATE job SET canonical_url = ?, company = ?, title = ?, board = ?,"
            " req_id = ?, workday_flag = ?, updated_at = ? WHERE id = ?",
            (canonical_url, company, title, board, req_id, int(workday_flag), utcnow(), job_id),
        )
        events.append(
            conn,
            actor=actor,
            kind="job_captured",
            job_id=job_id,
            payload={
                "canonical_url": canonical_url,
                "company": company,
                "title": title,
                "board": board,
                "req_id": req_id,
                "workday_flag": workday_flag,
            },
        )
    return get_job(conn, job_id)


def set_tab_state(conn: sqlite3.Connection, job_id: int, tab_state: str, actor: str) -> Job:
    if tab_state not in ("none", "open", "parked", "lost"):
        raise ValueError(f"tab_state must be none|open|parked|lost, got {tab_state!r}")
    job = get_job(conn, job_id)
    if job.tab_state == tab_state:
        return job
    with conn:
        conn.execute(
            "UPDATE job SET tab_state = ?, updated_at = ? WHERE id = ?",
            (tab_state, utcnow(), job_id),
        )
        events.append(
            conn,
            actor=actor,
            kind="tab_state_changed",
            job_id=job_id,
            payload={"from": job.tab_state, "to": tab_state},
        )
    return get_job(conn, job_id)


def jobs_with_tab_state(conn: sqlite3.Connection, tab_state: str) -> list[Job]:
    rows = conn.execute(
        "SELECT * FROM job WHERE tab_state = ? ORDER BY id", (tab_state,)
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def add_artifact(
    conn: sqlite3.Connection,
    job_id: int,
    kind: str,
    path: str,
    sha256: str,
    actor: str,
) -> Artifact:
    get_job(conn, job_id)
    with conn:
        cursor = conn.execute(
            "INSERT INTO artifact(job_id, kind, path, sha256, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (job_id, kind, path, sha256, utcnow()),
        )
        artifact_id = cursor.lastrowid
        events.append(
            conn,
            actor=actor,
            kind="artifact_added",
            job_id=job_id,
            payload={"artifact_id": artifact_id, "kind": kind, "path": path},
        )
    row = conn.execute("SELECT * FROM artifact WHERE id = ?", (artifact_id,)).fetchone()
    return Artifact.from_row(row)


def list_artifacts(conn: sqlite3.Connection, job_id: int) -> list[Artifact]:
    rows = conn.execute(
        "SELECT * FROM artifact WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()
    return [Artifact.from_row(row) for row in rows]


def get_application(conn: sqlite3.Connection, job_id: int) -> Application | None:
    row = conn.execute(
        "SELECT * FROM application WHERE job_id = ?", (job_id,)
    ).fetchone()
    return None if row is None else Application.from_row(row)


def get_or_create_application(conn: sqlite3.Connection, job_id: int) -> Application:
    existing = get_application(conn, job_id)
    if existing is not None:
        return existing
    get_job(conn, job_id)
    conn.execute("INSERT INTO application(job_id) VALUES (?)", (job_id,))
    return get_application(conn, job_id)
