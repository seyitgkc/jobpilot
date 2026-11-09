import pytest

from jobpilot.core import events, repo


def test_set_job_details_fixes_a_failed_capture(conn):
    job = repo.create_job(conn, "https://example.com/jobs/9", "claude")
    updated = repo.set_job_details(conn, job.id, "claude", company="Bluehost", title="Backend Engineer")
    assert (updated.company, updated.title) == ("Bluehost", "Backend Engineer")
    only_title = repo.set_job_details(conn, job.id, "claude", title="Senior Backend Engineer")
    assert (only_title.company, only_title.title) == ("Bluehost", "Senior Backend Engineer")
    assert [e.kind for e in events.timeline(conn, job.id)].count("job_details_set") == 2
    with pytest.raises(ValueError):
        repo.set_job_details(conn, job.id, "claude")
