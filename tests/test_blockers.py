import pytest

from jobpilot.core import blockers, events, repo


def test_raise_and_list_open(conn):
    job = repo.create_job(conn, "https://example.com/a", "claude")
    blocker = blockers.raise_blocker(
        conn, job.id, "workday_manual", "claude", question="apply by hand"
    )
    assert blocker.status == "open"
    assert blockers.is_blocked(conn, job.id)
    open_blockers = blockers.list_open(conn, job.id)
    assert [b.id for b in open_blockers] == [blocker.id]
    assert any(e.kind == "blocker_raised" for e in events.timeline(conn, job.id))


def test_unknown_kind_rejected(conn):
    job = repo.create_job(conn, "https://example.com/a", "claude")
    with pytest.raises(ValueError):
        blockers.raise_blocker(conn, job.id, "mystery", "claude")


def test_unknown_job_rejected(conn):
    with pytest.raises(repo.NotFound):
        blockers.raise_blocker(conn, 999, "other", "claude")


def test_list_open_scopes_to_job(conn):
    job_a = repo.create_job(conn, "https://example.com/a", "claude")
    job_b = repo.create_job(conn, "https://example.com/b", "claude")
    blockers.raise_blocker(conn, job_a.id, "captcha", "claude")
    assert blockers.list_open(conn, job_b.id) == []
    assert not blockers.is_blocked(conn, job_b.id)
    assert len(blockers.list_open(conn)) == 1
