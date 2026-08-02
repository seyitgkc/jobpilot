import asyncio
import threading
import time

from conftest import write_facts
from jobpilot.config import Config
from jobpilot.core import agenda, artifacts, repo
from jobpilot.db import open_db
from jobpilot.mcp.server import create_server
from test_tailor import POSTING


def make_api(conn, paths):
    _mcp, api = create_server(Config(), paths, conn=conn, client_factory=lambda: None)
    return api


def capture_like(conn, paths, url="https://example.com/new"):
    job = repo.create_job(conn, url, "owner")
    artifacts.store_text(conn, paths, job.id, "posting_md", "posting.md", POSTING, "owner")
    return job


def test_wait_returns_immediately_when_work_exists(conn, paths):
    write_facts(paths)
    api = make_api(conn, paths)
    capture_like(conn, paths)

    result = asyncio.run(api.agenda_wait(after_event_id=0, timeout_seconds=5))
    assert result["work"] is True
    assert not result["timed_out"]
    assert len(result["agenda"]["to_tailor"]) == 1


def test_wait_times_out_on_quiet_system(conn, paths):
    write_facts(paths)
    api = make_api(conn, paths)

    started = time.monotonic()
    result = asyncio.run(api.agenda_wait(after_event_id=0, timeout_seconds=5))
    elapsed = time.monotonic() - started

    assert result["work"] is False
    assert result["timed_out"] is True
    assert 4 < elapsed < 12


def test_wait_wakes_on_a_job_added_meanwhile(conn, paths):
    write_facts(paths)
    api = make_api(conn, paths)
    baseline = agenda.latest_event_id(conn)

    # A capture from the panel while the session is parked in agenda_wait.
    def add_job_soon():
        time.sleep(1)
        writer = open_db(paths, Config())
        job = writer.execute(
            "INSERT INTO job(url, status, workday_flag, created_at, updated_at)"
            " VALUES ('https://example.com/panel', 'captured', 0,"
            " '2026-08-02T12:00:00+00:00', '2026-08-02T12:00:00+00:00')"
        )
        writer.execute(
            "INSERT INTO event(job_id, ts, actor, kind) VALUES"
            " (?, '2026-08-02T12:00:00+00:00', 'owner', 'job_created')",
            (job.lastrowid,),
        )
        writer.commit()
        artifacts.store_text(
            writer, paths, job.lastrowid, "posting_md", "posting.md", POSTING, "owner"
        )
        writer.close()

    thread = threading.Thread(target=add_job_soon)
    thread.start()
    result = asyncio.run(api.agenda_wait(after_event_id=baseline, timeout_seconds=20))
    thread.join()

    assert result["work"] is True
    assert result["agenda"]["last_event_id"] > baseline
    urls = [entry["url"] for entry in result["agenda"]["to_tailor"]]
    assert "https://example.com/panel" in urls


def test_wait_ignores_stale_work_already_seen(conn, paths):
    write_facts(paths)
    api = make_api(conn, paths)
    capture_like(conn, paths)
    seen = agenda.latest_event_id(conn)

    result = asyncio.run(api.agenda_wait(after_event_id=seen, timeout_seconds=5))
    assert result["work"] is False
    assert result["last_event_id"] == seen


def test_timeout_is_bounded(conn, paths):
    api = make_api(conn, paths)
    started = time.monotonic()
    asyncio.run(api.agenda_wait(after_event_id=0, timeout_seconds=1))
    assert time.monotonic() - started >= 5


def test_tool_is_registered(conn, paths):
    import anyio

    mcp, _api = create_server(Config(), paths, conn=conn, client_factory=lambda: None)
    names = {tool.name for tool in anyio.run(mcp.list_tools)}
    assert "agenda_wait" in names
