from jobpilot.config import Config
from jobpilot.db import migrate, open_db

EXPECTED_TABLES = {
    "job",
    "artifact",
    "application",
    "field_fill",
    "blocker",
    "answer_bank",
    "mail",
    "event",
    "rule",
    "job_context",
    "credential",
    "setting",
    "blacklist",
    "schema_migration",
}


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row["name"] for row in rows}


def test_fresh_db_has_full_schema(conn):
    assert table_names(conn) == EXPECTED_TABLES


def test_migrate_is_idempotent(conn):
    assert migrate(conn) == []


def test_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_reopen_existing_db(paths):
    first = open_db(paths, Config())
    first.close()
    second = open_db(paths, Config())
    assert table_names(second) == EXPECTED_TABLES
    second.close()


def test_gate_settings_seeded(conn):
    rows = dict(conn.execute("SELECT key, value FROM setting").fetchall())
    assert rows["gates.resume_glance"] == "on"
    assert rows["gates.submit_approval"] == "on"
    assert rows["gates.auto_submit"] == "off"
