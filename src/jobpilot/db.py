"""SQLite connection setup and schema migrations.

Migrations are numbered SQL files in jobpilot/migrations, applied in order
and tracked in schema_migration. WAL mode, foreign keys on.
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path

from .config import Config, DataPaths
from .util import utcnow

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def connect(db_path: Path | str) -> sqlite3.Connection:
    # The admin app serves sync handlers from a thread pool over one shared
    # connection; the sqlite3 module serializes access, so cross-thread use
    # is safe here.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Daemon, panel, worker, and MCP server share this file across processes.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _migration_files() -> list[tuple[int, "resources.abc.Traversable"]]:
    found = []
    for entry in resources.files("jobpilot.migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry))
    found.sort(key=lambda pair: pair[0])
    return found


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations; returns the versions applied this call."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migration")}
    ran = []
    for version, entry in _migration_files():
        if version in applied:
            continue
        conn.executescript(entry.read_text())
        conn.execute(
            "INSERT INTO schema_migration(version, applied_at) VALUES (?, ?)",
            (version, utcnow()),
        )
        conn.commit()
        ran.append(version)
    return ran


def open_db(paths: DataPaths, config: Config | None = None) -> sqlite3.Connection:
    """Create the data root if needed, connect, migrate, seed settings."""
    paths.data_root.mkdir(parents=True, exist_ok=True)
    conn = connect(paths.db_path)
    migrate(conn)
    if config is not None:
        from .core import settings

        settings.seed_defaults(conn, config)
    return conn
