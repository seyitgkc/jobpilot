"""Login credentials, scoped to a job, company, or domain.

Plaintext in the local database by design (single owner, single machine).
Values are never echoed: listings mask, events carry no secret, and the fill
path redacts. The scope forms are job:{id}, company:{name}, domain:{host}.
"""

from __future__ import annotations

import sqlite3

from ..util import utcnow
from . import events

_SCOPE_PREFIXES = ("job:", "company:", "domain:")


def set_credential(
    conn: sqlite3.Connection,
    scope: str,
    username: str,
    password: str,
    actor: str,
    note: str | None = None,
) -> int:
    if not scope.startswith(_SCOPE_PREFIXES):
        raise ValueError("scope must start with job:, company:, or domain:")
    with conn:
        existing = conn.execute(
            "SELECT id FROM credential WHERE scope = ?", (scope,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE credential SET username = ?, password = ?, note = ? WHERE id = ?",
                (username, password, note, existing["id"]),
            )
            credential_id = existing["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO credential(scope, username, password, note, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (scope, username, password, note, utcnow()),
            )
            credential_id = cursor.lastrowid
        events.append(
            conn, actor=actor, kind="credential_set",
            payload={"scope": scope, "username": username},
        )
    return credential_id


def list_credentials(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, scope, username, note, created_at, last_used_at"
        " FROM credential ORDER BY scope"
    ).fetchall()
    return [
        {
            "id": row["id"],
            "scope": row["scope"],
            "username": row["username"],
            "password": "********",
            "note": row["note"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
        }
        for row in rows
    ]


def delete_credential(conn: sqlite3.Connection, credential_id: int, actor: str) -> None:
    row = conn.execute(
        "SELECT scope FROM credential WHERE id = ?", (credential_id,)
    ).fetchone()
    if row is None:
        from .repo import NotFound

        raise NotFound(f"credential {credential_id}")
    with conn:
        conn.execute("DELETE FROM credential WHERE id = ?", (credential_id,))
        events.append(
            conn, actor=actor, kind="credential_deleted",
            payload={"scope": row["scope"]},
        )


def get_credential(conn: sqlite3.Connection, scope: str) -> dict | None:
    """The unmasked credential for internal fill use; callers redact."""
    row = conn.execute(
        "SELECT * FROM credential WHERE scope = ?", (scope,)
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE credential SET last_used_at = ? WHERE id = ?", (utcnow(), row["id"])
    )
    conn.commit()
    return {
        "scope": row["scope"],
        "username": row["username"],
        "password": row["password"],
    }
