import logging

from conftest import force_status, write_facts
from jobpilot.config import Config
from jobpilot.core import agenda, artifacts, blockers, repo, state
from jobpilot.worker.__main__ import drain_tailoring
from test_tailor import POSTING, FakeProvider, fake_compile, good_plan


def make(conn, paths, status="captured", url="https://example.com/a"):
    job = repo.create_job(conn, url, "claude")
    artifacts.store_text(conn, paths, job.id, "posting_md", "posting.md", POSTING, "claude")
    if status != "captured":
        force_status(conn, job.id, status)
    return job


def test_agenda_buckets(conn, paths):
    write_facts(paths)
    captured = make(conn, paths)
    review = make(conn, paths, "tailored", "https://example.com/b")
    fillable = make(conn, paths, "tailored", "https://example.com/c")
    state.approve_resume(conn, fillable.id, "owner")
    submit_wait = make(conn, paths, "filled", "https://example.com/d")
    submit_ready = make(conn, paths, "filled", "https://example.com/e")
    state.approve_submit(conn, submit_ready.id, "owner")
    parked = make(conn, paths, status="captured", url="https://example.com/f")
    blocker = blockers.raise_blocker(conn, parked.id, "workday_manual", "claude")

    view = agenda.agenda(conn)
    assert [j["id"] for j in view["to_tailor"]] == [captured.id]
    assert [j["id"] for j in view["awaiting_resume_review"]] == [review.id]
    assert [j["id"] for j in view["ready_to_fill"]] == [fillable.id]
    assert [j["id"] for j in view["awaiting_submit_approval"]] == [submit_wait.id]
    assert [j["id"] for j in view["ready_to_submit"]] == [submit_ready.id]
    assert [b["id"] for b in view["open_blockers"]] == [blocker.id]
    assert view["answered_blockers"] == []
    assert view["unassigned_mail"] == 0


def test_agenda_respects_gate_overrides(conn, paths):
    job = make(conn, paths, "tailored")
    repo.set_job_gates(conn, job.id, "owner", resume_glance_gate="off")
    view = agenda.agenda(conn)
    assert [j["id"] for j in view["ready_to_fill"]] == [job.id]
    assert view["awaiting_resume_review"] == []


def test_drain_tailoring(conn, paths):
    write_facts(paths)
    first = make(conn, paths)
    second = make(conn, paths, url="https://example.com/b")
    blocked = make(conn, paths, url="https://example.com/c")
    blockers.raise_blocker(conn, blocked.id, "other", "claude", question="wait")

    provider = FakeProvider([good_plan, good_plan])
    done = drain_tailoring(
        conn, paths, Config(), provider,
        logging.getLogger("test"), compile_fn=fake_compile,
    )
    assert done == 2
    assert repo.get_job(conn, first.id).status == "tailored"
    assert repo.get_job(conn, second.id).status == "tailored"
    assert repo.get_job(conn, blocked.id).status == "captured"
    assert agenda.agenda(conn)["to_tailor"] == []
    assert [j["id"] for j in agenda.agenda(conn)["awaiting_resume_review"]] == [
        first.id, second.id,
    ]
