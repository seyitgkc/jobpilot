"""The answer bank: reusable answers keyed by a normalized question.

Job-specific answers are never banked; sensitive values are stored but
redacted wherever they would be echoed (field_fill, events, chat).
"""

from __future__ import annotations

import re
import sqlite3

from ..providers.schemas import AnswerBank, AnswerEntry
from ..util import utcnow
from . import events

_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalize(question: str) -> str:
    text = _NON_WORD.sub(" ", question.lower())
    return _SPACES.sub(" ", text).strip()


def set_answer(
    conn: sqlite3.Connection,
    question: str,
    answer: str,
    actor: str,
    *,
    answer_type: str = "text",
    sensitive: bool = False,
    scope: str = "global",
    created_from_blocker_id: int | None = None,
) -> None:
    key = normalize(question)
    if not key:
        raise ValueError("question normalizes to nothing")
    with conn:
        conn.execute(
            "INSERT INTO answer_bank(question_norm, question_raw, answer,"
            " answer_type, sensitive, scope, created_from_blocker_id, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(question_norm) DO UPDATE SET"
            " question_raw = excluded.question_raw, answer = excluded.answer,"
            " answer_type = excluded.answer_type, sensitive = excluded.sensitive,"
            " scope = excluded.scope, updated_at = excluded.updated_at",
            (
                key, question, answer, answer_type, int(sensitive), scope,
                created_from_blocker_id, utcnow(),
            ),
        )
        events.append(
            conn, actor=actor, kind="answer_banked",
            payload={"question": key, "sensitive": sensitive, "scope": scope},
        )


def get_answer(conn: sqlite3.Connection, question: str) -> AnswerEntry | None:
    row = conn.execute(
        "SELECT * FROM answer_bank WHERE question_norm = ?", (normalize(question),)
    ).fetchone()
    if row is None:
        return None
    return _entry(row)


def _entry(row: sqlite3.Row) -> AnswerEntry:
    return AnswerEntry(
        question=row["question_raw"] or row["question_norm"],
        answer=row["answer"] or "",
        answer_type=row["answer_type"],
        sensitive=bool(row["sensitive"]),
        scope=row["scope"],
    )


def bank(conn: sqlite3.Connection, company: str | None = None) -> AnswerBank:
    """Entries visible to one job: global scope plus the job's company scope."""
    rows = conn.execute("SELECT * FROM answer_bank ORDER BY question_norm").fetchall()
    entries = []
    company_scope = f"company:{company.lower()}" if company else None
    for row in rows:
        if row["scope"] == "global" or (
            company_scope and row["scope"].lower() == company_scope
        ):
            entries.append(_entry(row))
    return AnswerBank(entries=entries)


def is_sensitive_answer(conn: sqlite3.Connection, value: str) -> bool:
    rows = conn.execute(
        "SELECT answer FROM answer_bank WHERE sensitive = 1"
    ).fetchall()
    return any(row["answer"] == value for row in rows)
