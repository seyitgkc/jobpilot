import pytest
from fastapi.testclient import TestClient

from conftest import write_facts
from jobpilot.admin.app import create_app
from jobpilot.config import Config
from jobpilot.core import artifacts, blockers, context, repo, rules, settings, tailor
from jobpilot.db import open_db
from test_tailor import POSTING, FakeProvider, fake_compile, good_plan


@pytest.fixture
def web(paths):
    app = create_app(Config(), paths)
    with TestClient(app, follow_redirects=False) as client:
        conn = open_db(paths, Config())
        yield client, conn
        conn.close()


def seed_job(conn, paths, tailored=False):
    write_facts(paths)
    job = repo.create_job(
        conn, "https://boards.greenhouse.io/acme/jobs/1", "claude",
        company="Acme", title="Platform Engineer", board="greenhouse",
    )
    artifacts.store_text(conn, paths, job.id, "posting_md", "posting.md", POSTING, "claude")
    if tailored:
        result = tailor.tailor_job(
            conn, paths, Config(), FakeProvider([good_plan]), job.id,
            compile_fn=fake_compile,
        )
        assert result.ok
        return result.job
    return job


def test_dashboard_lists_jobs_and_blockers(web, paths):
    client, conn = web
    job = seed_job(conn, paths)
    blockers.raise_blocker(conn, job.id, "captcha", "claude", question="solve it")

    page = client.get("/")
    assert page.status_code == 200
    assert "Acme" in page.text
    assert "Platform Engineer" in page.text
    assert "blocked (1)" in page.text
    assert "captured" in page.text


def test_dashboard_empty_state(web):
    client, _conn = web
    page = client.get("/")
    assert page.status_code == 200
    assert "jobpilot capture" in page.text


def test_job_detail_shows_timeline_context_artifacts(web, paths):
    client, conn = web
    job = seed_job(conn, paths, tailored=True)
    context.append(conn, paths, job.id, "owner_pref", "emphasize infra work", "owner")
    context.set_summary(conn, paths, job.id, "tailored, waiting on review", "claude")

    page = client.get(f"/jobs/{job.id}")
    assert page.status_code == 200
    assert "emphasize infra work" in page.text
    assert "tailored, waiting on review" in page.text
    assert "resume.pdf" in page.text
    assert "status changed" in page.text
    assert "Resume review" in page.text


def test_job_detail_404(web):
    client, _conn = web
    assert client.get("/jobs/999").status_code == 404


def test_artifact_download_and_isolation(web, paths):
    client, conn = web
    job_a = seed_job(conn, paths, tailored=True)
    job_b = repo.create_job(conn, "https://example.com/b", "claude")

    artifact = repo.list_artifacts(conn, job_a.id)[0]
    good = client.get(f"/jobs/{job_a.id}/artifacts/{artifact.id}")
    assert good.status_code == 200
    assert good.content == paths.absolute(artifact.path).read_bytes()

    cross = client.get(f"/jobs/{job_b.id}/artifacts/{artifact.id}")
    assert cross.status_code == 404


def test_glance_shows_diff_and_approves(web, paths):
    client, conn = web
    job = seed_job(conn, paths, tailored=True)

    page = client.get(f"/jobs/{job.id}/glance")
    assert page.status_code == 200
    assert "validator passed" in page.text
    assert "Built the payments gateway in Go" in page.text
    assert "Approve this resume" in page.text

    response = client.post(f"/jobs/{job.id}/approve-resume")
    assert response.status_code == 303
    application = repo.get_application(conn, job.id)
    assert application.resume_approved_at is not None

    page = client.get(f"/jobs/{job.id}/glance")
    assert "Approve this resume" not in page.text
    assert "Approved" in page.text


def test_glance_without_plan(web, paths):
    client, conn = web
    job = seed_job(conn, paths)
    page = client.get(f"/jobs/{job.id}/glance")
    assert page.status_code == 200
    assert "no tailored resume yet" in page.text


def test_gate_override_and_submit_approval(web, paths):
    client, conn = web
    job = seed_job(conn, paths)

    response = client.post(
        f"/jobs/{job.id}/gates",
        data={"resume_glance_gate": "off", "submit_gate": "on"},
    )
    assert response.status_code == 303
    updated = repo.get_job(conn, job.id)
    assert updated.resume_glance_gate == "off"
    assert updated.submit_gate == "on"

    assert client.post(f"/jobs/{job.id}/approve-submit").status_code == 303
    assert repo.get_application(conn, job.id).submit_approved_at is not None

    bad = client.post(
        f"/jobs/{job.id}/gates",
        data={"resume_glance_gate": "sometimes", "submit_gate": "on"},
    )
    assert bad.status_code == 400


def test_abandon_action(web, paths):
    client, conn = web
    job = seed_job(conn, paths)
    assert client.post(f"/jobs/{job.id}/abandon").status_code == 303
    assert repo.get_job(conn, job.id).status == "abandoned"
    assert client.post(f"/jobs/{job.id}/abandon").status_code == 400


def test_rules_add_accept_reject_retire(web):
    client, conn = web
    response = client.post(
        "/rules",
        data={
            "kind": "banned_word", "scope": "tailoring",
            "text": "never use the word synergy",
            "pattern": r"\bsynergy\b", "enforcement": "hard",
        },
    )
    assert response.status_code == 303
    active = rules.active_rules(conn, "tailoring")
    assert len(active) == 1

    proposal = rules.propose_rule(conn, "style", "answers", "keep answers short")
    page = client.get("/rules")
    assert "keep answers short" in page.text
    assert "Accept" in page.text

    assert (
        client.post(f"/rules/{proposal.id}/status", data={"status": "active"}).status_code
        == 303
    )
    assert rules.get_rule(conn, proposal.id).status == "active"

    second = rules.propose_rule(conn, "style", "answers", "always answer in French")
    client.post(f"/rules/{second.id}/status", data={"status": "rejected"})
    assert rules.get_rule(conn, second.id).status == "rejected"

    client.post(f"/rules/{active[0].id}/status", data={"status": "retired"})
    assert rules.get_rule(conn, active[0].id).status == "retired"


def test_rules_bad_input(web):
    client, _conn = web
    response = client.post(
        "/rules",
        data={"kind": "banned_word", "scope": "global", "text": "x", "enforcement": "hard"},
    )
    assert response.status_code == 400


def test_settings_page_and_gate_toggle(web):
    client, conn = web
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Approval gates" in page.text
    assert "config.toml" in page.text

    response = client.post(
        "/settings/gates",
        data={"resume_glance": "off", "submit_approval": "on", "auto_submit": "off"},
    )
    assert response.status_code == 303
    assert settings.get(conn, "gates.resume_glance") == "off"
    assert settings.get(conn, "gates.submit_approval") == "on"

    bad = client.post(
        "/settings/gates",
        data={"resume_glance": "maybe", "submit_approval": "on", "auto_submit": "off"},
    )
    assert bad.status_code == 400
