import pytest

from conftest import write_facts
from jobpilot.config import Config
from jobpilot.core import blockers, fill, repo, state
from jobpilot.mcp.server import create_server
from jobpilot.providers.schemas import MappedField
from test_fill import FakeFillClient, make_mapping
from test_tailor import POSTING


@pytest.fixture
def api(conn, paths):
    fill_client = FakeFillClient()
    _mcp, api = create_server(
        Config(), paths, conn=conn, client_factory=lambda: fill_client
    )
    api.fill_client = fill_client
    return api


def seed(conn, paths, api):
    write_facts(paths)
    job = repo.create_job(
        conn, "https://example.com/apply", "claude", company="Acme", title="Platform Engineer"
    )
    from jobpilot.core import artifacts

    artifacts.store_text(conn, paths, job.id, "posting_md", "posting.md", POSTING, "claude")
    return job


def test_server_registers_full_surface(conn, paths):
    mcp, _api = create_server(Config(), paths, conn=conn, client_factory=lambda: None)
    import anyio

    tools = anyio.run(mcp.list_tools)
    names = {tool.name for tool in tools}
    assert {
        "job_capture", "job_tailor_context", "job_tailor_submit", "form_extract",
        "form_map_context", "form_fill", "blocker_answer", "blocker_execute",
        "job_submit", "answer_bank_set", "rules_propose", "settings_get",
        "credential_set", "tab_replay",
    } <= names


def test_session_driven_tailor_roundtrip(api, conn, paths, monkeypatch):
    from jobpilot.core import latex
    from test_tailor import fake_compile

    monkeypatch.setattr(latex, "compile_tex", fake_compile)
    job = seed(conn, paths, api)

    context = api.job_tailor_context(job.id, notes="payments focus")
    assert context["constraints"]["notes"] == "payments focus"
    assert context["facts"]["profile"]["name"] == "Jamie Doe"

    from jobpilot.core import resume as resume_module

    plan = resume_module.full_plan(
        resume_module.load_facts(paths)
    ).model_dump()
    result = api.job_tailor_submit(job.id, plan)
    assert result["ok"], result["error"]
    assert result["job"]["status"] == "tailored"
    assert result["report"]["ok"]

    glance = api.job_glance(job.id)
    assert glance["report"]["ok"]


def test_session_driven_fill_and_blocker_flow(api, conn, paths, monkeypatch):
    from jobpilot.core import latex
    from test_tailor import fake_compile

    monkeypatch.setattr(latex, "compile_tex", fake_compile)
    job = seed(conn, paths, api)
    from jobpilot.core import resume as resume_module

    api.job_tailor_submit(
        job.id, resume_module.full_plan(resume_module.load_facts(paths)).model_dump()
    )
    api.job_approve_resume(job.id)

    extracted = api.form_extract(job.id)
    assert extracted["ok"]
    context = api.form_map_context(job.id)
    assert any(e["question"] == "email" for e in context["answers"]["entries"])

    mapping = make_mapping(
        salary=MappedField(field_id="salary", needs_owner=True, reason="not banked"),
    ).model_dump()
    filled = api.form_fill(job.id, mapping)
    assert not filled["ok"]
    assert filled["blockers"][0]["kind"] == "missing_answer"

    blocker_id = filled["blockers"][0]["id"]
    detail = api.blocker_get(blocker_id)
    assert {f["field_id"] for f in detail["field_spec"]["fields"]} == {"cover", "salary"}

    api.blocker_answer(
        blocker_id,
        [
            {"verb": "set", "field_id": "salary", "value": "130000"},
            {"verb": "set", "field_id": "cover", "value": "Short and honest."},
        ],
        reusable_fields=["salary"],
    )
    executed = api.blocker_execute(blocker_id)
    assert executed["ok"]
    assert executed["job"]["status"] == "filled"

    banked = api.answer_bank_list()
    assert any(entry["question"] == "Desired salary" for entry in banked)

    api.job_approve_submit(job.id)
    submitted = api.job_submit(job.id)
    assert submitted["ok"]
    assert submitted["job"]["status"] == "submitted"


def test_sensitive_answers_masked_in_listing(api, conn):
    api.answer_bank_set("Desired salary", "120000", sensitive=True)
    api.answer_bank_set("Notice period", "two weeks")
    listing = {entry["question"]: entry["answer"] for entry in api.answer_bank_list()}
    assert listing["Desired salary"] == "[redacted]"
    assert listing["Notice period"] == "two weeks"


def test_credentials_masked(api):
    api.credential_set("company:acme", "jamie@example.com", "hunter2")
    listing = api.credential_list()
    assert listing[0]["password"] == "********"
    assert "hunter2" not in str(listing)


def test_manual_submitted_and_rules(api, conn, paths):
    job = seed(conn, paths, api)
    marked = api.job_mark_manual_submitted(job.id, note="applied on their portal")
    assert marked["status"] == "submitted_manually"

    proposal = api.rules_propose("style", "answers", "keep answers under two sentences")
    assert proposal["status"] == "proposed"
    accepted = api.rules_set_status(proposal["id"], "active")
    assert accepted["status"] == "active"
