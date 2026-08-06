"""Employers the owner has ruled out.

Checked during capture, so a posting from a blacklisted employer never
reaches tailoring. A match carries the rejection mail already on file from
that employer, which is the evidence the entry was earned rather than a bare
refusal to proceed.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse

from . import events
from .mail import Mail
from ..util import utcnow

KINDS = frozenset({"company", "domain"})

_REJECTION_HINT = re.compile(
    r"not (?:be )?(?:moving|move) forward|not selected|decided not to|"
    r"pursue other candidates|will not be progressing|unsuccessful|"
    r"not a (?:good )?(?:fit|match) (?:at this time|right now)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BlacklistEntry:
    id: int
    kind: str
    pattern: str
    normalised: str
    note: str | None
    created_at: str
    created_by: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "BlacklistEntry":
        return cls(**{key: row[key] for key in row.keys()})


def normalise(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _host(url: str | None) -> str:
    if not url:
        return ""
    return urlparse(url).netloc.lower().split(":")[0]


def add(
    conn: sqlite3.Connection,
    pattern: str,
    actor: str,
    kind: str = "company",
    note: str | None = None,
) -> BlacklistEntry:
    if kind not in KINDS:
        raise ValueError(f"unknown blacklist kind {kind!r}")
    pattern = pattern.strip()
    if not pattern:
        raise ValueError("blacklist pattern is empty")
    key = normalise(pattern) if kind == "company" else pattern.lower().strip()
    if not key:
        raise ValueError(f"blacklist pattern {pattern!r} normalises to nothing")
    with conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO blacklist"
            "(kind, pattern, normalised, note, created_at, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (kind, pattern, key, note, utcnow(), actor),
        )
        if cursor.rowcount:
            events.append(
                conn, actor=actor, kind="blacklist_added", job_id=None,
                payload={"kind": kind, "pattern": pattern},
            )
    row = conn.execute(
        "SELECT * FROM blacklist WHERE kind = ? AND normalised = ?", (kind, key)
    ).fetchone()
    return BlacklistEntry.from_row(row)


def remove(conn: sqlite3.Connection, entry_id: int, actor: str) -> bool:
    row = conn.execute("SELECT * FROM blacklist WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return False
    with conn:
        conn.execute("DELETE FROM blacklist WHERE id = ?", (entry_id,))
        events.append(
            conn, actor=actor, kind="blacklist_removed", job_id=None,
            payload={"kind": row["kind"], "pattern": row["pattern"]},
        )
    return True


def list_all(conn: sqlite3.Connection) -> list[BlacklistEntry]:
    return [
        BlacklistEntry.from_row(row)
        for row in conn.execute("SELECT * FROM blacklist ORDER BY kind, pattern")
    ]


def match(
    conn: sqlite3.Connection, company: str | None, url: str | None = None
) -> BlacklistEntry | None:
    """The entry ruling this posting out, if any. Company names are compared
    with punctuation and case removed, and the entry has to be contained in
    the company: "Achievers" rules out "Achievers Solutions Inc" without also
    ruling out a different employer called "Achiever"."""
    entries = list_all(conn)
    target = normalise(company)
    host = _host(url)
    for entry in entries:
        if entry.kind == "company" and target and entry.normalised:
            if entry.normalised in target:
                return entry
        if entry.kind == "domain" and host:
            if host == entry.normalised or host.endswith("." + entry.normalised):
                return entry
    return None


def rejections(
    conn: sqlite3.Connection, entry: BlacklistEntry, paths=None
) -> list[Mail]:
    """Rejection mail already on file from this employer. Matches the mail
    assigned to one of its jobs, and unassigned mail whose subject names it,
    since a rejection often arrives before its job was ever tracked."""
    key = entry.normalised
    if not key:
        return []
    found: dict[int, Mail] = {}

    for row in conn.execute(
        "SELECT m.* FROM mail m JOIN job j ON j.id = m.job_id"
        " WHERE m.classification = 'rejection'"
    ):
        if key in normalise(row["subject"]) or key in normalise(
            _company_of(conn, row["job_id"])
        ):
            found[row["id"]] = Mail.from_row(row)

    for row in conn.execute(
        "SELECT * FROM mail WHERE classification = 'rejection' AND job_id IS NULL"
    ):
        if key in normalise(row["subject"]):
            found[row["id"]] = Mail.from_row(row)

    return [found[key_id] for key_id in sorted(found)]


def _company_of(conn: sqlite3.Connection, job_id: int | None) -> str:
    if job_id is None:
        return ""
    row = conn.execute("SELECT company FROM job WHERE id = ?", (job_id,)).fetchone()
    return (row["company"] if row else "") or ""
