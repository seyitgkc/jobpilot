import pytest
from fastapi.testclient import TestClient

from conftest import write_facts
from jobpilot.admin.app import create_app
from jobpilot.config import Config
from jobpilot.core import answers, artifacts, blockers, fill, repo, rules, settings, state, tailor
from jobpilot.core import context
from jobpilot.db import open_db
from jobpilot.providers.schemas import MappedField
from test_fill import FakeFillClient, make_mapping
from test_tailor import POSTING, FakeProvider, fake_compile, good_plan


@pytest.fixture
def fill_client():
    return FakeFillClient()


@pytest.fixture
def web(paths, fill_client):
    app = create_app(Config(), paths, browser_client_factory=lambda: fill_client)
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
    assert "Browser tabs" in page.text and "2/8" in page.text


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


def parked_blocker(conn, paths, fill_client):
    job = seed_job(conn, paths, tailored=True)
    fill.extract_form(conn, paths, Config(), fill_client, job.id)
    state.approve_resume(conn, job.id, "owner")
    mapping = make_mapping(
        salary=MappedField(field_id="salary", needs_owner=True, reason="no banked salary"),
    )
    result = fill.apply_mapping(conn, paths, Config(), fill_client, job.id, mapping)
    return job, result.blockers[0]


def test_blockers_queue_answer_and_execute(web, paths, fill_client):
    client, conn = web
    job, blocker = parked_blocker(conn, paths, fill_client)

    page = client.get("/blockers")
    assert page.status_code == 200
    assert "Cover letter" in page.text
    assert "Save answers and fill them" in page.text
    assert "Save for future applications" in page.text

    deep = client.get(f"/blockers/{blocker.id}")
    assert deep.status_code == 200
    assert "missing answer" in deep.text

    response = client.post(
        f"/blockers/{blocker.id}/answer",
        data={
            "value_cover": "I build payment systems.",
            "value_salary": "130000",
            "bank_salary": "1",
        },
    )
    assert response.status_code == 303
    assert blockers.get_blocker(conn, blocker.id).status == "resolved"
    assert repo.get_job(conn, job.id).status == "filled"
    assert answers.get_answer(conn, "Desired salary").answer == "130000"

    empty = client.get("/blockers")
    assert "Nothing is waiting on you" in empty.text


def test_blockers_answer_requires_all_fields(web, paths, fill_client):
    client, conn = web
    _job, blocker = parked_blocker(conn, paths, fill_client)
    response = client.post(
        f"/blockers/{blocker.id}/answer", data={"value_cover": "text only"}
    )
    assert response.status_code == 400


def test_blocker_manual_resolve_routes(web, paths):
    client, conn = web
    job = seed_job(conn, paths)
    blocker = blockers.raise_blocker(
        conn, job.id, "workday_manual", "claude", question="apply by hand"
    )
    page = client.get("/blockers")
    assert "Mark resolved by hand" in page.text
    assert client.post(f"/blockers/{blocker.id}/resolve").status_code == 303
    assert blockers.get_blocker(conn, blocker.id).status == "resolved"


def test_resume_approval_blocker_resolve_approves(web, paths):
    client, conn = web
    job = seed_job(conn, paths, tailored=True)
    blocker = blockers.raise_blocker(
        conn, job.id, "resume_approval", "claude", question="approve the resume"
    )
    assert client.post(f"/blockers/{blocker.id}/resolve").status_code == 303
    assert repo.get_application(conn, job.id).resume_approved_at is not None
    assert blockers.get_blocker(conn, blocker.id).status == "resolved"


def test_lost_tab_replay_from_dashboard(web, paths, fill_client):
    client, conn = web
    job, blocker = parked_blocker(conn, paths, fill_client)
    client.post(
        f"/blockers/{blocker.id}/answer",
        data={"value_cover": "text", "value_salary": "130000"},
    )
    repo.set_tab_state(conn, job.id, "lost", "browserd")

    page = client.get("/")
    assert "Lost tabs" in page.text
    assert "Reopen and replay" in page.text

    response = client.post(f"/jobs/{job.id}/replay")
    assert response.status_code == 303
    assert fill_client.opened
    from jobpilot.core import events

    kinds = [event.kind for event in events.timeline(conn, job.id)]
    assert "tab_replayed" in kinds


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
