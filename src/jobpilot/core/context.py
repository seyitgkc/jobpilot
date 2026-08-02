"""Per-job working memory and job-scoped context assembly.

Isolation is structural: every function takes one job_id and queries only that
job's rows, so nothing from another job can enter a prompt or a decision.
Updates supersede rather than edit, keeping the timeline auditable. context.md
under the job's directory is a regenerated render; the DB is the record.
"""

from __future__ import annotations

import sqlite3

from ..config import DataPaths
from ..util import utcnow
from . import events, repo
from .models import ContextRow

KINDS = frozenset({"note", "decision", "owner_pref", "constraint", "followup", "summary"})

_SECTION_ORDER = ("summary", "owner_pref", "decision", "constraint", "followup", "note")
_SECTION_TITLES = {
    "summary": "Summary",
    "owner_pref": "Owner preferences",
    "decision": "Decisions",
    "constraint": "Constraints",
    "followup": "Follow-ups",
    "note": "Notes",
}


class IsolationError(Exception):
    pass


def append(
    conn: sqlite3.Connection,
    paths: DataPaths,
    job_id: int,
    kind: str,
    content: str,
    source: str,
    actor: str = "claude",
) -> ContextRow:
    if kind not in KINDS:
        raise ValueError(f"unknown context kind {kind!r}")
    repo.get_job(conn, job_id)
    with conn:
        cursor = conn.execute(
            "INSERT INTO job_context(job_id, kind, content, source, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (job_id, kind, content, source, utcnow()),
        )
        row_id = cursor.lastrowid
        events.append(
            conn,
            actor=actor,
            kind="context_appended",
            job_id=job_id,
            payload={"context_id": row_id, "kind": kind},
        )
    render_md(conn, paths, job_id)
    return _get(conn, row_id)


def supersede(
    conn: sqlite3.Connection,
    paths: DataPaths,
    job_id: int,
    old_id: int,
    new_content: str,
    source: str,
    actor: str = "claude",
) -> ContextRow:
    old = _get(conn, old_id)
    if old.job_id != job_id:
        raise IsolationError(
            f"context row {old_id} belongs to job {old.job_id}, not job {job_id}"
        )
    if old.superseded_by is not None:
        raise ValueError(f"context row {old_id} is already superseded")
    with conn:
        cursor = conn.execute(
            "INSERT INTO job_context(job_id, kind, content, source, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (job_id, old.kind, new_content, source, utcnow()),
        )
        new_id = cursor.lastrowid
        conn.execute(
            "UPDATE job_context SET superseded_by = ? WHERE id = ?", (new_id, old_id)
        )
        events.append(
            conn,
            actor=actor,
            kind="context_superseded",
            job_id=job_id,
            payload={"old_id": old_id, "new_id": new_id, "kind": old.kind},
        )
    render_md(conn, paths, job_id)
    return _get(conn, new_id)


def set_summary(
    conn: sqlite3.Connection,
    paths: DataPaths,
    job_id: int,
    content: str,
    source: str,
    actor: str = "claude",
) -> ContextRow:
    """Refresh the rolling summary row, superseding the previous one."""
    current = [row for row in active(conn, job_id) if row.kind == "summary"]
    if current:
        return supersede(conn, paths, job_id, current[-1].id, content, source, actor)
    return append(conn, paths, job_id, "summary", content, source, actor)


def active(conn: sqlite3.Connection, job_id: int) -> list[ContextRow]:
    rows = conn.execute(
        "SELECT * FROM job_context WHERE job_id = ? AND superseded_by IS NULL"
        " ORDER BY id",
        (job_id,),
    ).fetchall()
    return [ContextRow.from_row(row) for row in rows]


def _get(conn: sqlite3.Connection, row_id: int) -> ContextRow:
    row = conn.execute("SELECT * FROM job_context WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise repo.NotFound(f"context row {row_id}")
    return ContextRow.from_row(row)


def assemble(conn: sqlite3.Connection, job_id: int) -> dict:
    """Everything a session needs to work this one job. Shared stores (facts,
    answer bank, active rules) are fetched separately; nothing here can reach
    another job's rows."""
    job = repo.get_job(conn, job_id)
    open_blockers = conn.execute(
        "SELECT COUNT(*) FROM blocker WHERE job_id = ? AND status IN ('open', 'answered')",
        (job_id,),
    ).fetchone()[0]
    return {
        "job": job,
        "application": repo.get_application(conn, job_id),
        "artifacts": repo.list_artifacts(conn, job_id),
        "context": active(conn, job_id),
        "open_blocker_count": open_blockers,
    }


def render_md(conn: sqlite3.Connection, paths: DataPaths, job_id: int) -> None:
    """Regenerate jobs/{id}/context.md from active rows."""
    job = repo.get_job(conn, job_id)
    rows = active(conn, job_id)
    heading = f"Job {job.id}"
    if job.title or job.company:
        title = job.title or "unknown role"
        company = job.company or "unknown company"
        heading = f"Job {job.id}: {title} at {company}"
    lines = [f"# {heading}", "", f"Status: {job.status}", f"URL: {job.url}", ""]
    by_kind: dict[str, list[ContextRow]] = {}
    for row in rows:
        by_kind.setdefault(row.kind, []).append(row)
    for kind in _SECTION_ORDER:
        entries = by_kind.get(kind)
        if not entries:
            continue
        lines.append(f"## {_SECTION_TITLES[kind]}")
        lines.append("")
        for entry in entries:
            suffix = f" ({entry.source})" if entry.source else ""
            lines.append(f"- [{entry.created_at}] {entry.content}{suffix}")
        lines.append("")
    directory = paths.job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "context.md").write_text("\n".join(lines).rstrip() + "\n")
