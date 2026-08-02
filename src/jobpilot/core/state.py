"""Job state machine.

job.status is written here and nowhere else. transition() validates the edge,
enforces the two approval gates, updates the row, and appends the event in one
transaction. "blocked" is not a status: it is derived from open blocker rows.

The mail-driven tail is a graph rather than the straight
acknowledged -> in_process line: a rejection or an interview invitation can
arrive at any point after submission, so every post-submit state accepts the
mail-driven outcomes directly.
"""

from __future__ import annotations

import sqlite3

from ..util import utcnow
from . import events, repo, settings
from .models import Job

TERMINAL = frozenset({"rejected", "offer", "abandoned"})

# Statuses where no browser tab is expected any more.
TAB_IRRELEVANT = frozenset(
    {
        "submitted",
        "submitted_manually",
        "acknowledged",
        "in_process",
        "interview",
        "rejected",
        "offer",
        "abandoned",
    }
)

_MAIL_DRIVEN = frozenset({"acknowledged", "in_process", "interview", "rejected", "offer"})

_EDGES: dict[str, frozenset[str]] = {
    "captured": frozenset({"tailored", "manual_required"}),
    "tailored": frozenset({"filling", "manual_required"}),
    "filling": frozenset({"filled", "manual_required"}),
    # filled -> filling covers rework after drift or a rejected fill;
    # submitting -> filled covers a failed submit attempt.
    "filled": frozenset({"submitting", "filling", "manual_required"}),
    "submitting": frozenset({"submitted", "filled"}),
    "submitted": _MAIL_DRIVEN,
    "acknowledged": frozenset({"in_process", "interview", "rejected", "offer"}),
    "in_process": frozenset({"interview", "rejected", "offer"}),
    # interview -> in_process: back to waiting between rounds.
    "interview": frozenset({"in_process", "rejected", "offer"}),
    "manual_required": frozenset({"submitted_manually"}),
    "submitted_manually": _MAIL_DRIVEN,
    "rejected": frozenset(),
    "offer": frozenset(),
    "abandoned": frozenset(),
}

STATES = frozenset(_EDGES)


class IllegalTransition(Exception):
    pass


class GateNotSatisfied(Exception):
    pass


def is_legal(from_status: str, to_status: str) -> bool:
    if from_status not in STATES:
        raise ValueError(f"unknown status {from_status!r}")
    if to_status not in STATES:
        raise ValueError(f"unknown status {to_status!r}")
    if to_status == "abandoned":
        return from_status not in TERMINAL
    return to_status in _EDGES[from_status]


def effective_gate(conn: sqlite3.Connection, job: Job, gate: str) -> bool:
    """Resolve a gate to on/off: per-job override unless 'inherit', else the
    global setting. gate is 'resume_glance' or 'submit_approval'."""
    override = {
        "resume_glance": job.resume_glance_gate,
        "submit_approval": job.submit_gate,
    }[gate]
    if override in ("on", "off"):
        return override == "on"
    return settings.get(conn, f"gates.{gate}", "on") == "on"


def _check_gates(conn: sqlite3.Connection, job: Job, to_status: str) -> None:
    if to_status == "filling" and effective_gate(conn, job, "resume_glance"):
        application = repo.get_application(conn, job.id)
        if application is None or application.resume_approved_at is None:
            raise GateNotSatisfied(f"job {job.id}: resume approval required before filling")
    if to_status == "submitting" and effective_gate(conn, job, "submit_approval"):
        application = repo.get_application(conn, job.id)
        if application is None or application.submit_approved_at is None:
            raise GateNotSatisfied(f"job {job.id}: submit approval required before submitting")


def transition(
    conn: sqlite3.Connection,
    job_id: int,
    to_status: str,
    actor: str,
    payload: dict | None = None,
) -> Job:
    job = repo.get_job(conn, job_id)
    if not is_legal(job.status, to_status):
        raise IllegalTransition(f"job {job_id}: {job.status} -> {to_status}")
    _check_gates(conn, job, to_status)
    with conn:
        conn.execute(
            "UPDATE job SET status = ?, updated_at = ? WHERE id = ?",
            (to_status, utcnow(), job_id),
        )
        events.append(
            conn,
            actor=actor,
            kind="status_changed",
            job_id=job_id,
            payload={"from": job.status, "to": to_status, **(payload or {})},
        )
    # Past submission the tab has no further use, so a tab that went missing
    # stops being something to reopen.
    if to_status in TAB_IRRELEVANT and job.tab_state != "none":
        repo.set_tab_state(conn, job_id, "none", actor)
    return repo.get_job(conn, job_id)


def approve_resume(conn: sqlite3.Connection, job_id: int, actor: str) -> None:
    repo.get_or_create_application(conn, job_id)
    with conn:
        conn.execute(
            "UPDATE application SET resume_approved_at = ? WHERE job_id = ?",
            (utcnow(), job_id),
        )
        events.append(conn, actor=actor, kind="resume_approved", job_id=job_id)


def approve_submit(conn: sqlite3.Connection, job_id: int, actor: str) -> None:
    repo.get_or_create_application(conn, job_id)
    with conn:
        conn.execute(
            "UPDATE application SET submit_approved_at = ? WHERE job_id = ?",
            (utcnow(), job_id),
        )
        events.append(conn, actor=actor, kind="submit_approved", job_id=job_id)
