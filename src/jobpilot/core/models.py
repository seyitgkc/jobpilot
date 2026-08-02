"""Row dataclasses for the tables core currently manipulates."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


def _cols(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


@dataclass(frozen=True)
class Job:
    id: int
    url: str
    canonical_url: str | None
    company: str | None
    title: str | None
    board: str | None
    req_id: str | None
    status: str
    workday_flag: bool
    resume_glance_gate: str
    submit_gate: str
    tab_state: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        data = _cols(row)
        data["workday_flag"] = bool(data["workday_flag"])
        return cls(**data)


@dataclass(frozen=True)
class Artifact:
    id: int
    job_id: int
    kind: str
    path: str
    sha256: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Artifact":
        return cls(**_cols(row))


@dataclass(frozen=True)
class Application:
    id: int
    job_id: int
    resume_artifact_id: int | None
    tailor_plan_artifact_id: int | None
    form_schema_artifact_id: int | None
    selector_map: str | None
    method: str | None
    filled_at: str | None
    resume_approved_at: str | None
    submit_approved_at: str | None
    submitted_at: str | None
    confirmation_ref: str | None
    submitted_by: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Application":
        return cls(**_cols(row))


@dataclass(frozen=True)
class Event:
    id: int
    job_id: int | None
    ts: str
    actor: str
    kind: str
    payload: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        return cls(**_cols(row))


@dataclass(frozen=True)
class Rule:
    id: int
    kind: str
    scope: str
    text: str
    pattern: str | None
    enforcement: str
    source: str
    status: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Rule":
        return cls(**_cols(row))


@dataclass(frozen=True)
class ContextRow:
    id: int
    job_id: int
    kind: str
    content: str
    source: str | None
    superseded_by: int | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ContextRow":
        return cls(**_cols(row))
