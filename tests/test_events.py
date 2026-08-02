import pytest

from jobpilot.core import events, repo


def test_append_and_timeline(conn):
    job = repo.create_job(conn, "https://example.com/x", "claude")
    events.append(
        conn, actor="browserd", kind="tab_opened", job_id=job.id, payload={"url": job.url}
    )
    conn.commit()
    timeline = events.timeline(conn, job.id)
    assert [e.kind for e in timeline] == ["job_created", "tab_opened"]
    assert timeline[1].actor == "browserd"


def test_unknown_actor_rejected(conn):
    with pytest.raises(ValueError):
        events.append(conn, actor="intern", kind="oops")


def test_repo_mutations_write_events(conn):
    job = repo.create_job(conn, "https://example.com/x", "claude")
    repo.set_job_gates(conn, job.id, "owner", submit_gate="off")
    repo.add_artifact(conn, job.id, "posting_md", "jobs/1/posting.md", "cafebabe", "claude")
    kinds = [e.kind for e in events.timeline(conn, job.id)]
    assert kinds == ["job_created", "gates_changed", "artifact_added"]
