from email.message import EmailMessage

import pytest

from conftest import write_facts
from jobpilot.config import Config
from jobpilot.core import answers, artifacts, blockers, context, events, mail, repo


def eml(subject="Thanks for applying", message_id="<m1@x>"):
    message = EmailMessage()
    message["From"] = "Acme Recruiting <no-reply@greenhouse.io>"
    message["To"] = "jamie@example.com"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = "Sat, 01 Aug 2026 12:00:00 +0000"
    message.set_content("We received your application.")
    return bytes(message)


@pytest.fixture
def populated(conn, paths):
    write_facts(paths)
    job = repo.create_job(
        conn, "https://example.com/job", "claude", company="Acme", title="Engineer"
    )
    artifact = artifacts.store_text(
        conn, paths, job.id, "posting_md", "posting.md", "# Role", "claude"
    )
    application = repo.get_or_create_application(conn, job.id)
    repo.update_application_artifacts(
        conn, job.id, "claude", tailor_plan_artifact_id=artifact.id
    )
    repo.add_field_fill(
        conn, application.id, "first_name",
        field_label="First name", field_type="text",
        value_redacted="Jamie", source="profile",
    )
    blocker = blockers.raise_blocker(
        conn, job.id, "missing_answer", "claude", question="salary?"
    )
    answers.set_answer(
        conn, "Desired salary", "130000", "owner", created_from_blocker_id=blocker.id
    )
    context.append(conn, paths, job.id, "note", "good fit", "claude")
    stored = mail.store_message(conn, paths, eml(), "worker")
    mail.assign(conn, paths, stored.id, job.id, "owner")
    return {"job": job, "mail": stored, "blocker": blocker}


def test_delete_job_removes_everything_it_owns(conn, paths, populated):
    job = populated["job"]
    job_dir = paths.job_dir(job.id)
    assert job_dir.exists()

    repo.delete_job(conn, paths, job.id, "owner")

    with pytest.raises(repo.NotFound):
        repo.get_job(conn, job.id)
    for table in ("application", "blocker", "job_context", "artifact", "field_fill"):
        column = "application_id" if table == "field_fill" else "job_id"
        query = (
            f"SELECT COUNT(*) FROM {table}"
            if table == "field_fill"
            else f"SELECT COUNT(*) FROM {table} WHERE {column} = {job.id}"
        )
        assert conn.execute(query).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM event WHERE job_id = ?", (job.id,)
    ).fetchone()[0] == 0
    assert not job_dir.exists()


def test_delete_job_preserves_shared_stores(conn, paths, populated):
    job = populated["job"]
    repo.delete_job(conn, paths, job.id, "owner")

    banked = answers.get_answer(conn, "Desired salary")
    assert banked is not None and banked.answer == "130000"
    row = conn.execute(
        "SELECT created_from_blocker_id FROM answer_bank WHERE question_norm = 'desired salary'"
    ).fetchone()
    assert row["created_from_blocker_id"] is None

    freed = mail.get_mail(conn, populated["mail"].id)
    assert freed.job_id is None
    assert freed.status == "needs_assignment"
    assert freed.match_method is None
    assert paths.absolute(freed.body_path).exists()

    global_events = conn.execute(
        "SELECT kind, payload FROM event WHERE job_id IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert global_events["kind"] == "job_deleted"
    assert "Acme" in global_events["payload"]


def test_delete_unknown_job_raises(conn, paths):
    with pytest.raises(repo.NotFound):
        repo.delete_job(conn, paths, 999, "owner")
