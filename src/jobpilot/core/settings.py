"""Global settings stored in the setting table.

The DB row is the runtime source of truth (the panel edits it); config.toml
supplies the initial values on first run via seed_defaults().
"""

from __future__ import annotations

import sqlite3

from ..config import Config
from . import events

GATE_KEYS = ("gates.resume_glance", "gates.submit_approval", "gates.auto_submit")


def get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return default if row is None else row["value"]


def set(conn: sqlite3.Connection, key: str, value: str, actor: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO setting(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        events.append(
            conn,
            actor=actor,
            kind="setting_changed",
            payload={"key": key, "value": value},
        )


def all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM setting ORDER BY key")
    }


def seed_defaults(conn: sqlite3.Connection, config: Config) -> None:
    """Insert missing settings from config; never overwrites existing rows."""
    defaults = {
        "gates.resume_glance": "on" if config.gates.resume_glance else "off",
        "gates.submit_approval": "on" if config.gates.submit_approval else "off",
        "gates.auto_submit": "on" if config.gates.auto_submit else "off",
    }
    with conn:
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO setting(key, value) VALUES (?, ?)", (key, value)
            )
