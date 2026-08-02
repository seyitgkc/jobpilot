import pytest

from jobpilot.config import Config
from jobpilot.core import answers, blockers, fill, repo, resolve, state
from jobpilot.providers.schemas import MappedField
from jobpilot.util import from_json
from test_fill import FakeFillClient, make_mapping
from test_fill import tailored_job as tailored_job  # fixture reexport


@pytest.fixture
def parked(conn, paths, tailored_job):
    """A job filled up to the batched missing_answer blocker: cover letter
    and salary wait on the owner, everything else is applied."""
    client = FakeFillClient()
    fill.extract_form(conn, paths, Config(), client, tailored_job.id)
    state.approve_resume(conn, tailored_job.id, "owner")
    mapping = make_mapping(
        salary=MappedField(
            field_id="salary", needs_owner=True,
            reason="no banked salary", proposed="120000",
        ),
    )
    result = fill.apply_mapping(conn, paths, Config(), client, tailored_job.id, mapping)
    assert result.blockers and result.blockers[0].kind == "missing_answer"
    return {"job": result.job, "blocker": result.blockers[0], "client": client}


SALARY_ACTION = {"verb": "set", "field_id": "salary", "value": "130000"}
COVER_ACTION = {"verb": "set", "field_id": "cover", "value": "I build payment systems."}


def test_blocker_carries_crops_and_page_context(parked):
    spec = from_json(parked["blocker"].field_spec)
    by_id = {item["field_id"]: item for item in spec["fields"]}
    assert by_id["salary"]["screenshot_artifact_id"] is not None
    assert by_id["salary"]["proposed"] == "120000"
    context = from_json(parked["blocker"].page_context)
    assert "salary" in context["selectors"]
    assert context["url"]


def test_answer_records_actions_and_banks(conn, parked):
    blocker = resolve.answer_blocker(
        conn, parked["blocker"].id, [SALARY_ACTION, COVER_ACTION], "owner",
        reusable_fields=("salary",),
    )
    assert blocker.status == "answered"
    assert blocker.answered_at is not None
    assert blocker.answer_reusable
    agreed = from_json(blocker.agreed_action)
    assert len(agreed["actions"]) == 2

    banked = answers.get_answer(conn, "Desired salary")
    assert banked is not None and banked.answer == "130000"
    assert answers.get_answer(conn, "Cover letter") is None


def test_answer_rejects_foreign_fields_and_verbs(conn, parked):
    with pytest.raises(ValueError):
        resolve.answer_blocker(
            conn, parked["blocker"].id,
            [{"verb": "set", "field_id": "ghost", "value": "x"}], "owner",
        )
    with pytest.raises(ValueError):
        resolve.answer_blocker(
            conn, parked["blocker"].id,
            [{"verb": "click_mapped", "field_id": "salary"}], "owner",
        )


def test_execute_validates_applies_and_finalizes(conn, paths, parked):
    client = parked["client"]
    resolve.answer_blocker(
        conn, parked["blocker"].id, [SALARY_ACTION, COVER_ACTION], "owner"
    )
    result = resolve.execute_blocker(
        conn, paths, Config(), client, parked["blocker"].id, actor="owner"
    )

    assert result.ok
    assert result.blocker.status == "resolved"
    assert result.blocker.resolved_at is not None
    assert result.finalized_job is not None
    assert result.finalized_job.status == "filled"
    assert not blockers.is_blocked(conn, parked["job"].id)

    application = repo.get_application(conn, parked["job"].id)
    fills = {f.field_id: f for f in repo.list_field_fills(conn, application.id)}
    assert fills["salary"].source == f"blocker:{parked['blocker'].id}"
    assert fills["salary"].status == "verified"
    assert application.filled_at is not None


def test_execute_redacts_sensitive_checkpoint(conn, paths, parked):
    answers.set_answer(conn, "Desired salary", "130000", "owner", sensitive=True)
    resolve.answer_blocker(
        conn, parked["blocker"].id, [SALARY_ACTION, COVER_ACTION], "owner"
    )
    resolve.execute_blocker(
        conn, paths, Config(), parked["client"], parked["blocker"].id
    )
    application = repo.get_application(conn, parked["job"].id)
    fills = {f.field_id: f for f in repo.list_field_fills(conn, application.id)}
    assert fills["salary"].value_redacted == "[redacted]"


def test_execute_requires_answered(conn, paths, parked):
    with pytest.raises(ValueError):
        resolve.execute_blocker(
            conn, paths, Config(), parked["client"], parked["blocker"].id
        )


def test_drift_parks_without_typing(conn, paths, parked):
    client = parked["client"]

    def drifting_validate(job_id, fields, selector_map):
        return {
            f["field_id"]: {"present": f["field_id"] != "salary", "matches": f["field_id"] != "salary", "reason": "gone"}
            for f in fields
        }

    client.validate = drifting_validate
    resolve.answer_blocker(
        conn, parked["blocker"].id, [SALARY_ACTION, COVER_ACTION], "owner"
    )
    calls_before = len(client.apply_calls)
    result = resolve.execute_blocker(
        conn, paths, Config(), client, parked["blocker"].id
    )

    assert result.drift is not None
    assert result.drift.kind == "drift"
    assert "salary" in result.drift.question
    assert len(client.apply_calls) == calls_before
    assert blockers.get_blocker(conn, parked["blocker"].id).status == "answered"
    assert repo.get_job(conn, parked["job"].id).status == "filling"


def test_execute_failure_returns_to_answered(conn, paths, parked):
    client = parked["client"]
    client.fail_fields = {"cover"}
    resolve.answer_blocker(
        conn, parked["blocker"].id, [SALARY_ACTION, COVER_ACTION], "owner"
    )
    result = resolve.execute_blocker(
        conn, paths, Config(), client, parked["blocker"].id
    )
    assert not result.ok
    assert [outcome["field_id"] for outcome in result.failed] == ["cover"]
    assert result.blocker.status == "answered"
    assert repo.get_job(conn, parked["job"].id).status == "filling"


def test_mark_manual_resolved(conn, paths, tailored_job):
    blocker = blockers.raise_blocker(
        conn, tailored_job.id, "captcha", "claude", question="solve the captcha"
    )
    resolved = resolve.mark_manual_resolved(conn, blocker.id, "owner", note="done in tab")
    assert resolved.status == "resolved"
    assert not blockers.is_blocked(conn, tailored_job.id)
    with pytest.raises(ValueError):
        resolve.mark_manual_resolved(conn, blocker.id, "owner")


def test_abandon_job_abandons_blockers(conn, paths, parked):
    job = resolve.abandon_job(conn, parked["job"].id, "owner")
    assert job.status == "abandoned"
    assert blockers.list_open(conn, job.id) == []
    assert blockers.get_blocker(conn, parked["blocker"].id).status == "abandoned"


def test_replay_rebuilds_lost_tab(conn, paths, parked):
    client = parked["client"]
    resolve.answer_blocker(
        conn, parked["blocker"].id, [SALARY_ACTION, COVER_ACTION], "owner",
        reusable_fields=("salary",),
    )
    resolve.execute_blocker(conn, paths, Config(), client, parked["blocker"].id)
    answers.set_answer(conn, "Desired salary", "130000", "owner", sensitive=True)
    repo.set_tab_state(conn, parked["job"].id, "lost", "browserd")

    replay_client = FakeFillClient()
    result = resolve.replay_tab(
        conn, paths, Config(), replay_client, parked["job"].id, actor="owner"
    )

    assert result.ok, (result.failed, result.missing)
    replayed_actions = {
        action["field_id"]: action
        for call in replay_client.apply_calls
        for action in call
    }
    assert replayed_actions["first_name"]["value"] == "Jamie"
    assert replayed_actions["salary"]["value"] == "130000"
    assert replayed_actions["resume"]["verb"] == "upload"
    assert replayed_actions["agree"]["verb"] == "check"
    assert set(result.replayed) >= {"first_name", "salary", "cover", "resume"}


def test_replay_reraises_unrecoverable_fields(conn, paths, parked):
    client = parked["client"]
    resolve.answer_blocker(
        conn, parked["blocker"].id, [SALARY_ACTION, COVER_ACTION], "owner"
    )
    resolve.execute_blocker(conn, paths, Config(), client, parked["blocker"].id)
    # Sensitive checkpoint with no banked answer cannot be reconstructed.
    answers.set_answer(conn, "Desired salary", "130000", "owner", sensitive=True)
    conn.execute(
        "UPDATE field_fill SET value_redacted = '[redacted]' WHERE field_id = 'salary'"
    )
    conn.execute("DELETE FROM answer_bank")
    conn.commit()

    result = resolve.replay_tab(
        conn, paths, Config(), FakeFillClient(), parked["job"].id
    )
    assert "salary" in result.missing
    assert result.blockers and result.blockers[0].kind == "missing_answer"
    assert "salary" in result.blockers[0].question
