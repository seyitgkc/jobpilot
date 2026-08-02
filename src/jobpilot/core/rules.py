"""Global rules store.

Hard rules carry a regex and are enforced deterministically by validators via
hard_violations(); soft rules are injected into provider prompts. Owner rules
activate immediately; AI proposals sit at 'proposed' until the owner accepts.
Only the owner activates rules or touches owner-authored rows.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from ..util import utcnow
from . import events
from .models import Rule

KINDS = frozenset(
    {"banned_word", "banned_construct", "always", "never", "style", "fact_correction"}
)
ENFORCEMENTS = frozenset({"hard", "soft"})
STATUSES = frozenset({"active", "proposed", "rejected", "retired"})


class RulePermissionError(Exception):
    pass


class NotFound(Exception):
    pass


@dataclass(frozen=True)
class RuleViolation:
    rule_id: int
    rule_text: str
    match: str


def _validate(kind: str, enforcement: str, pattern: str | None) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown rule kind {kind!r}")
    if enforcement not in ENFORCEMENTS:
        raise ValueError(f"enforcement must be hard|soft, got {enforcement!r}")
    if enforcement == "hard" and not pattern:
        raise ValueError("hard rules require a pattern; use soft for prompt-only rules")
    if pattern:
        re.compile(pattern)


def _insert(
    conn: sqlite3.Connection,
    *,
    kind: str,
    scope: str,
    text: str,
    pattern: str | None,
    enforcement: str,
    source: str,
    status: str,
    actor: str,
) -> Rule:
    _validate(kind, enforcement, pattern)
    now = utcnow()
    with conn:
        cursor = conn.execute(
            "INSERT INTO rule(kind, scope, text, pattern, enforcement, source,"
            " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, scope, text, pattern, enforcement, source, status, now, now),
        )
        rule_id = cursor.lastrowid
        events.append(
            conn,
            actor=actor,
            kind="rule_created",
            payload={"rule_id": rule_id, "scope": scope, "status": status, "text": text},
        )
    return get_rule(conn, rule_id)


def add_owner_rule(
    conn: sqlite3.Connection,
    kind: str,
    scope: str,
    text: str,
    *,
    pattern: str | None = None,
    enforcement: str = "soft",
) -> Rule:
    return _insert(
        conn,
        kind=kind,
        scope=scope,
        text=text,
        pattern=pattern,
        enforcement=enforcement,
        source="owner",
        status="active",
        actor="owner",
    )


def propose_rule(
    conn: sqlite3.Connection,
    kind: str,
    scope: str,
    text: str,
    *,
    pattern: str | None = None,
    enforcement: str = "soft",
) -> Rule:
    return _insert(
        conn,
        kind=kind,
        scope=scope,
        text=text,
        pattern=pattern,
        enforcement=enforcement,
        source="ai_proposed",
        status="proposed",
        actor="claude",
    )


def get_rule(conn: sqlite3.Connection, rule_id: int) -> Rule:
    row = conn.execute("SELECT * FROM rule WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise NotFound(f"rule {rule_id}")
    return Rule.from_row(row)


def list_rules(conn: sqlite3.Connection, status: str | None = None) -> list[Rule]:
    if status is None:
        rows = conn.execute("SELECT * FROM rule ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM rule WHERE status = ? ORDER BY id", (status,)
        ).fetchall()
    return [Rule.from_row(row) for row in rows]


def set_status(conn: sqlite3.Connection, rule_id: int, status: str, actor: str) -> Rule:
    if status not in STATUSES:
        raise ValueError(f"unknown rule status {status!r}")
    rule = get_rule(conn, rule_id)
    if rule.source == "owner" and actor != "owner":
        raise RulePermissionError("owner-authored rules are edited only by the owner")
    if status == "active" and actor != "owner":
        raise RulePermissionError("only the owner activates rules")
    with conn:
        conn.execute(
            "UPDATE rule SET status = ?, updated_at = ? WHERE id = ?",
            (status, utcnow(), rule_id),
        )
        events.append(
            conn,
            actor=actor,
            kind="rule_status_changed",
            payload={"rule_id": rule_id, "from": rule.status, "to": status},
        )
    return get_rule(conn, rule_id)


def update_text(
    conn: sqlite3.Connection,
    rule_id: int,
    actor: str,
    *,
    text: str | None = None,
    pattern: str | None = None,
) -> Rule:
    rule = get_rule(conn, rule_id)
    if rule.source == "owner" and actor != "owner":
        raise RulePermissionError("owner-authored rules are edited only by the owner")
    if rule.source == "ai_proposed" and actor != "owner" and rule.status != "proposed":
        raise RulePermissionError("accepted proposals change only via the owner")
    new_text = text if text is not None else rule.text
    new_pattern = pattern if pattern is not None else rule.pattern
    _validate(rule.kind, rule.enforcement, new_pattern)
    with conn:
        conn.execute(
            "UPDATE rule SET text = ?, pattern = ?, updated_at = ? WHERE id = ?",
            (new_text, new_pattern, utcnow(), rule_id),
        )
        events.append(
            conn,
            actor=actor,
            kind="rule_updated",
            payload={"rule_id": rule_id, "text": new_text},
        )
    return get_rule(conn, rule_id)


def active_rules(conn: sqlite3.Connection, *scopes: str) -> list[Rule]:
    """Active rules for the given scopes, always including 'global'.

    Every provider call path receives the result of this function; there is
    no rule-free path to a model.
    """
    wanted = {"global", *scopes}
    placeholders = ", ".join("?" for _ in wanted)
    rows = conn.execute(
        f"SELECT * FROM rule WHERE status = 'active' AND scope IN ({placeholders})"
        " ORDER BY id",
        tuple(sorted(wanted)),
    ).fetchall()
    return [Rule.from_row(row) for row in rows]


def hard_violations(text: str, rules: list[Rule]) -> list[RuleViolation]:
    """Check text against the hard rules in the given list; deterministic,
    case-insensitive. Soft rules and non-pattern rules are skipped."""
    found = []
    for rule in rules:
        if rule.enforcement != "hard" or not rule.pattern:
            continue
        match = re.search(rule.pattern, text, re.IGNORECASE)
        if match:
            found.append(
                RuleViolation(rule_id=rule.id, rule_text=rule.text, match=match.group(0))
            )
    return found
