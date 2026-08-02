"""What waits, across the whole pipeline.

The database is the queue: a job's status plus the gate stamps say exactly
what should happen to it next. This module reads that into one structure so
any drainer (a chat session, the worker, the owner scanning the panel) can
pick up work without re-deriving the rules.
"""

from __future__ import annotations

import sqlite3

from . import blockers, mail, repo, state
from .models import Job


# Buckets a working session can act on by itself. The awaiting_* buckets are
# the owner's move, so they wake nobody; they travel in the agenda for
# reporting.
ACTIONABLE = (
    "to_tailor",
    "ready_to_fill",
    "ready_to_submit",
    "answered_blockers",
    "mail_to_classify",
    "lost_tabs",
)


def has_work(view: dict) -> bool:
    return any(view.get(key) for key in ACTIONABLE)


def latest_event_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) FROM event").fetchone()
    return row[0] or 0


def _summary(job: Job) -> dict:
    return {
        "id": job.id,
        "company": job.company,
        "title": job.title,
        "status": job.status,
        "url": job.canonical_url or job.url,
    }


def agenda(conn: sqlite3.Connection) -> dict:
    jobs = repo.list_jobs(conn)
    blocked = blockers.open_counts(conn)

    to_tailor = []
    awaiting_resume_review = []
    ready_to_fill = []
    awaiting_submit_approval = []
    ready_to_submit = []

    for job in jobs:
        if job.id in blocked:
            continue
        application = repo.get_application(conn, job.id)
        if job.status == "captured":
            to_tailor.append(job)
        elif job.status == "tailored":
            gate_on = state.effective_gate(conn, job, "resume_glance")
            approved = application is not None and application.resume_approved_at
            if gate_on and not approved:
                awaiting_resume_review.append(job)
            else:
                ready_to_fill.append(job)
        elif job.status == "filled":
            gate_on = state.effective_gate(conn, job, "submit_approval")
            approved = application is not None and application.submit_approved_at
            if gate_on and not approved:
                awaiting_submit_approval.append(job)
            else:
                ready_to_submit.append(job)

    unclassified = [
        row
        for row in (
            mail.Mail.from_row(r)
            for r in conn.execute(
                "SELECT * FROM mail WHERE classification IS NULL"
                " AND received_at >= ? ORDER BY id DESC",
                (mail.tracking_start(conn),),
            )
        )
        if mail.likely_relevant(conn, row)
    ][:25]

    open_blockers = blockers.list_open(conn)
    return {
        "last_event_id": latest_event_id(conn),
        "to_tailor": [_summary(job) for job in to_tailor],
        "awaiting_resume_review": [_summary(job) for job in awaiting_resume_review],
        "ready_to_fill": [_summary(job) for job in ready_to_fill],
        "awaiting_submit_approval": [_summary(job) for job in awaiting_submit_approval],
        "ready_to_submit": [_summary(job) for job in ready_to_submit],
        "open_blockers": [
            {"id": b.id, "job_id": b.job_id, "kind": b.kind, "status": b.status,
             "question": b.question}
            for b in open_blockers
        ],
        "answered_blockers": [b.id for b in open_blockers if b.status == "answered"],
        "mail_to_classify": [
            {"id": row.id, "subject": row.subject, "sender_domain": row.sender_domain}
            for row in unclassified
        ],
        "unassigned_mail": len(mail.list_unassigned(conn)),
        "lost_tabs": [
            _summary(job)
            for job in repo.jobs_with_tab_state(conn, "lost")
            if job.status not in state.TAB_IRRELEVANT
        ],
    }
