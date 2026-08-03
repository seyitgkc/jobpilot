import pytest

from conftest import write_facts
from jobpilot.config import Config
from jobpilot.core import answers, artifacts, blockers, fill, repo, rules, state, tailor
from jobpilot.providers.base import NullProvider
from jobpilot.providers.schemas import FieldMapping, MappedField
from jobpilot.util import from_json
from test_tailor import POSTING, FakeProvider, fake_compile, good_plan

FORM_HTML = """
<form>
  <label for="fn">First name</label><input id="fn" name="first_name" required>
  <label for="em">Email</label><input id="em" name="email" type="email" required>
  <label for="exp">Years of experience</label>
  <select id="exp" name="experience"><option value="">Pick</option><option>1-3</option><option>4-6</option></select>
  <label for="sal">Desired salary</label><input id="sal" name="salary">
  <label for="cover">Cover letter</label><textarea id="cover" name="cover"></textarea>
  <label for="cv">Resume</label><input id="cv" name="resume" type="file" required>
  <label for="agree">I agree to the terms</label><input id="agree" name="agree" type="checkbox">
  <button type="submit">Submit application</button>
</form>
"""


class FakeFillClient:
    def __init__(self, html=FORM_HTML, unresolved=(), fail_fields=()):
        self.html = html
        self.unresolved = list(unresolved)
        self.fail_fields = set(fail_fields)
        self.opened = []
        self.closed = []
        self.apply_calls = []
        self.page_title = "Thanks. Confirmation number: XYZ789"

    def open_tab(self, job_id, url):
        self.opened.append(url)
        return {"url": url, "title": "Apply"}

    def archive(self, job_id):
        return {
            "html": self.html, "raw_html": self.html,
            "url": "https://example.com/apply", "title": "Apply",
        }

    def resolve(self, job_id, form_schema):
        resolved = {
            field["field_id"]: {"kind": "css", "selector": f"[name='{field['field_id']}']"}
            for field in form_schema["fields"]
            if field["field_id"] not in self.unresolved
        }
        if form_schema.get("submit_button"):
            resolved["submit"] = {"kind": "role", "role": "button", "name": "Submit application"}
        if form_schema.get("apply_button"):
            resolved["apply"] = {
                "kind": "role", "role": "link",
                "name": form_schema["apply_button"]["label"],
            }
        return {"resolved": resolved, "unresolved": self.unresolved}

    def apply(self, job_id, actions, selector_map):
        self.apply_calls.append(actions)
        results = []
        for action in actions:
            ok = action["field_id"] not in self.fail_fields
            results.append(
                {
                    "field_id": action["field_id"], "verb": action["verb"],
                    "ok": ok, "verified": ok,
                    **({} if ok else {"error": "timeout"}),
                }
            )
        return results

    def validate(self, job_id, fields, selector_map):
        return {f["field_id"]: {"present": True, "matches": True} for f in fields}

    def screenshot(self, job_id, selector=None):
        return b"\x89PNG fake-shot"

    def crop(self, job_id, descriptor):
        return b"\x89PNG fake-crop"

    def page_state(self, job_id):
        return {"url": "https://example.com/done", "title": self.page_title}

    def challenge(self, job_id):
        return {"present": getattr(self, "captcha", False), "kind": "recaptcha"}

    def focus(self, job_id):
        self.focused = getattr(self, "focused", 0) + 1

    def close_tab(self, job_id):
        self.closed.append(job_id)

    def health(self):
        return {"status": "ok", "open_tabs": [1, 2]}

    def render_pdf(self, html):
        return b"%PDF-1.4 fake\n"


POSTING_GATE_HTML = """
<div class="posting">
  <h1>Platform Engineer</h1>
  <label for="share">Link to This Job</label><input id="share" name="share_link">
  <a href="#apply">Apply for This Job</a>
</div>
"""


class GatedFormClient(FakeFillClient):
    """Posting page first; the real form appears after the apply click."""

    def __init__(self):
        super().__init__(html=POSTING_GATE_HTML)

    def apply(self, job_id, actions, selector_map):
        results = super().apply(job_id, actions, selector_map)
        if any(action["verb"] == "click_mapped" and action["field_id"] == "apply"
               for action in actions):
            self.html = FORM_HTML
        return results


def make_mapping(**overrides):
    items = {
        "first_name": MappedField(field_id="first_name", value="Jamie", source="profile", confidence=0.95),
        "email": MappedField(field_id="email", value="jamie@example.com", source="profile", confidence=0.95),
        "experience": MappedField(field_id="experience", value="4-6", source="answer_bank", confidence=0.9),
        "salary": MappedField(field_id="salary", value="120000", source="answer_bank", confidence=0.9),
        "cover": MappedField(field_id="cover", needs_owner=True, reason="no banked cover letter"),
        "resume": MappedField(field_id="resume", source="resume_file", confidence=1.0),
        "agree": MappedField(field_id="agree", value=True, source="derived", confidence=0.8),
    }
    items.update(overrides)
    return FieldMapping(fields=list(items.values()))


@pytest.fixture
def tailored_job(conn, paths):
    write_facts(paths)
    job = repo.create_job(
        conn, "https://example.com/apply", "claude",
        company="Acme", title="Platform Engineer", board="other",
    )
    artifacts.store_text(conn, paths, job.id, "posting_md", "posting.md", POSTING, "claude")
    result = tailor.tailor_job(
        conn, paths, Config(), FakeProvider([good_plan]), job.id, compile_fn=fake_compile
    )
    assert result.ok
    return result.job


def extract(conn, paths, client, job):
    return fill.extract_form(conn, paths, Config(), client, job.id)


def test_extract_form_follows_apply_gate(conn, paths, tailored_job):
    client = GatedFormClient()
    result = fill.extract_form(conn, paths, Config(), client, tailored_job.id)

    assert result.ok, [b.question for b in result.blockers]
    assert result.schema.submit_button is not None
    field_ids = {field.field_id for field in result.schema.fields}
    assert "first_name" in field_ids and "share_link" not in field_ids
    clicked = [
        action for call in client.apply_calls for action in call
        if action["verb"] == "click_mapped" and action["field_id"] == "apply"
    ]
    assert len(clicked) == 1

    from jobpilot.core import events

    kinds = [event.kind for event in events.timeline(conn, tailored_job.id)]
    assert "apply_gate_followed" in kinds


def test_extract_form_gate_without_apply_control_parks(conn, paths, tailored_job):
    client = FakeFillClient(html="<div><p>Read about the role.</p></div>")
    result = fill.extract_form(conn, paths, Config(), client, tailored_job.id)
    assert not result.ok
    assert result.blockers[0].kind == "extraction_failed"


def test_extract_form_persists_schema_and_map(conn, paths, tailored_job):
    client = FakeFillClient()
    result = extract(conn, paths, client, tailored_job)

    assert result.ok
    assert "form_schema_json" in result.artifacts
    application = repo.get_application(conn, tailored_job.id)
    assert application.form_schema_artifact_id == result.artifacts["form_schema_json"].id
    assert application.method == "dom_generic"
    selector_map = from_json(application.selector_map)
    assert "first_name" in selector_map and "submit" in selector_map


def test_extract_parks_on_unresolved_required(conn, paths, tailored_job):
    client = FakeFillClient(unresolved=["first_name", "resume"])
    result = extract(conn, paths, client, tailored_job)

    assert not result.ok
    assert result.blockers[0].kind == "extraction_failed"
    assert "first_name" in result.blockers[0].question
    assert client.apply_calls == []


def test_extract_parks_on_empty_form(conn, paths, tailored_job):
    client = FakeFillClient(html="<html><p>Loading</p></html>")
    result = extract(conn, paths, client, tailored_job)
    assert result.blockers[0].kind == "extraction_failed"


def test_fill_context_includes_profile_and_bank(conn, paths, tailored_job):
    answers.set_answer(conn, "Years of experience", "4-6", "owner")
    extract(conn, paths, FakeFillClient(), tailored_job)

    context = fill.fill_context(conn, paths, Config(), tailored_job.id)
    questions = [entry.question for entry in context["answers"].entries]
    assert "email" in questions and "Years of experience" in questions
    email = next(e for e in context["answers"].entries if e.question == "email")
    assert email.answer == "jamie@example.com"
    assert context["job"].company == "Acme"


def test_fill_parks_behind_resume_gate(conn, paths, tailored_job):
    client = FakeFillClient()
    extract(conn, paths, client, tailored_job)
    result = fill.apply_mapping(
        conn, paths, Config(), client, tailored_job.id, make_mapping()
    )
    assert result.blockers[0].kind == "resume_approval"
    assert repo.get_job(conn, tailored_job.id).status == "tailored"
    assert client.apply_calls == []


def approve_and_extract(conn, paths, client, job):
    extract(conn, paths, client, job)
    state.approve_resume(conn, job.id, "owner")


def test_fill_happy_path(conn, paths, tailored_job):
    answers.set_answer(conn, "Desired salary", "120000", "owner", sensitive=True)
    client = FakeFillClient()
    approve_and_extract(conn, paths, client, tailored_job)

    result = fill.apply_mapping(
        conn, paths, Config(), client, tailored_job.id,
        make_mapping(cover=MappedField(field_id="cover", value="From the bank", source="answer_bank", confidence=0.9)),
    )

    assert result.ok
    assert result.job.status == "filled"
    application = repo.get_application(conn, tailored_job.id)
    assert application.filled_at is not None
    assert "filled_screenshot" in result.artifacts

    fills = {f.field_id: f for f in repo.list_field_fills(conn, application.id)}
    assert fills["first_name"].status == "verified"
    assert fills["salary"].value_redacted == "[redacted]"
    assert fills["first_name"].value_redacted == "Jamie"
    assert fills["resume"].source == "resume_file"

    upload = next(
        action for call in client.apply_calls for action in call
        if action["verb"] == "upload"
    )
    assert upload["path"].endswith("resume.pdf")


def test_needs_owner_fields_batch_into_one_blocker(conn, paths, tailored_job):
    client = FakeFillClient()
    approve_and_extract(conn, paths, client, tailored_job)

    mapping = make_mapping(
        salary=MappedField(field_id="salary", needs_owner=True, reason="no banked salary", proposed="ask owner"),
    )
    result = fill.apply_mapping(conn, paths, Config(), client, tailored_job.id, mapping)

    assert len(result.blockers) == 1
    blocker = result.blockers[0]
    assert blocker.kind == "missing_answer"
    spec = from_json(blocker.field_spec)
    assert {item["field_id"] for item in spec["fields"]} == {"cover", "salary"}
    assert "Cover letter" in blocker.question

    applied_ids = {action["field_id"] for call in client.apply_calls for action in call}
    assert "first_name" in applied_ids
    assert "cover" not in applied_ids and "salary" not in applied_ids
    assert repo.get_job(conn, tailored_job.id).status == "filling"
    assert blockers.is_blocked(conn, tailored_job.id)


def test_hard_rules_gate_mapping(conn, paths, tailored_job):
    rules.add_owner_rule(
        conn, "banned_word", "answers", "never say negotiable",
        pattern=r"\bnegotiable\b", enforcement="hard",
    )
    client = FakeFillClient()
    approve_and_extract(conn, paths, client, tailored_job)

    mapping = make_mapping(
        salary=MappedField(field_id="salary", value="Fully negotiable", source="derived", confidence=0.5),
    )
    result = fill.apply_mapping(conn, paths, Config(), client, tailored_job.id, mapping)

    guarded = result.mapping.by_id("salary")
    assert guarded.needs_owner
    assert guarded.proposed == "Fully negotiable"
    applied_ids = {action["field_id"] for call in client.apply_calls for action in call}
    assert "salary" not in applied_ids


def test_failed_apply_becomes_blocker(conn, paths, tailored_job):
    client = FakeFillClient(fail_fields={"email"})
    approve_and_extract(conn, paths, client, tailored_job)

    result = fill.apply_mapping(
        conn, paths, Config(), client, tailored_job.id,
        make_mapping(cover=MappedField(field_id="cover", value="ok", source="answer_bank", confidence=0.9)),
    )
    assert any("email" in b.question for b in result.blockers)
    application = repo.get_application(conn, tailored_job.id)
    fills = {f.field_id: f for f in repo.list_field_fills(conn, application.id)}
    assert fills["email"].status == "failed"
    assert repo.get_job(conn, tailored_job.id).status == "filling"


def test_fill_job_provider_path_and_unavailable(conn, paths, tailored_job):
    client = FakeFillClient()
    state.approve_resume(conn, tailored_job.id, "owner")

    class MappingProvider:
        def map_fields(self, schema, answer_bank, job):
            return make_mapping(
                cover=MappedField(field_id="cover", value="banked text", source="answer_bank", confidence=0.9)
            )

    result = fill.fill_job(
        conn, paths, Config(), client, MappingProvider(), tailored_job.id
    )
    assert result.ok
    assert result.job.status == "filled"

    job2 = repo.create_job(conn, "https://example.com/two", "claude", company="B", title="X")
    artifacts.store_text(conn, paths, job2.id, "posting_md", "posting.md", POSTING, "claude")
    tailor.tailor_job(conn, paths, Config(), FakeProvider([good_plan]), job2.id, compile_fn=fake_compile)
    result2 = fill.fill_job(
        conn, paths, Config(), FakeFillClient(), NullProvider(), job2.id
    )
    assert result2.blockers and "AI provider" in result2.blockers[0].question


def filled_job(conn, paths, client, tailored_job):
    approve_and_extract(conn, paths, client, tailored_job)
    result = fill.apply_mapping(
        conn, paths, Config(), client, tailored_job.id,
        make_mapping(cover=MappedField(field_id="cover", value="ok", source="answer_bank", confidence=0.9)),
    )
    assert result.ok
    return result.job


def test_submit_parks_behind_gate(conn, paths, tailored_job):
    client = FakeFillClient()
    job = filled_job(conn, paths, client, tailored_job)

    result = fill.submit_job(conn, paths, Config(), client, job.id)
    assert result.blockers[0].kind == "submit_approval"
    assert repo.get_job(conn, job.id).status == "filled"


def test_submit_happy_path(conn, paths, tailored_job):
    client = FakeFillClient()
    job = filled_job(conn, paths, client, tailored_job)
    state.approve_submit(conn, job.id, "owner")

    result = fill.submit_job(conn, paths, Config(), client, job.id)

    assert result.ok
    assert result.job.status == "submitted"
    application = repo.get_application(conn, job.id)
    assert application.submitted_by == "system"
    assert application.submitted_at is not None
    assert application.confirmation_ref == "XYZ789"
    assert "confirmation_screenshot" in result.artifacts
    assert client.closed == [job.id]


def test_submit_parks_on_human_verification(conn, paths, tailored_job):
    client = FakeFillClient()
    job = filled_job(conn, paths, client, tailored_job)
    state.approve_submit(conn, job.id, "owner")
    client.captcha = True

    result = fill.submit_job(conn, paths, Config(), client, job.id)

    assert not result.ok
    assert result.blockers[0].kind == "captcha"
    assert repo.get_job(conn, job.id).status == "filled"
    assert client.focused == 1
    assert client.closed == []
    application = repo.get_application(conn, job.id)
    assert application.submitted_at is None

    # Solving it and submitting immediately is the only path forward, and it
    # succeeds without asking again.
    client.captcha = False
    second = fill.submit_job(conn, paths, Config(), client, job.id)
    assert second.ok
    assert second.job.status == "submitted"


def test_submit_without_mapped_control_parks(conn, paths, tailored_job):
    client = FakeFillClient()
    job = filled_job(conn, paths, client, tailored_job)
    state.approve_submit(conn, job.id, "owner")
    repo.update_application_fill(conn, job.id, "owner", selector_map="{}")

    result = fill.submit_job(conn, paths, Config(), client, job.id)
    assert result.blockers and "submit control" in result.blockers[0].question
    assert repo.get_job(conn, job.id).status == "filled"


def test_submit_click_failure_parks(conn, paths, tailored_job):
    client = FakeFillClient(fail_fields={"submit"})
    job = filled_job(conn, paths, client, tailored_job)
    state.approve_submit(conn, job.id, "owner")

    result = fill.submit_job(conn, paths, Config(), client, job.id)
    assert result.blockers and "submit click failed" in result.blockers[0].question
    assert repo.get_job(conn, job.id).status == "filled"


def test_fill_from_wrong_status(conn, paths):
    write_facts(paths)
    job = repo.create_job(conn, "https://example.com/x", "claude")
    client = FakeFillClient()
    extract(conn, paths, client, job)
    result = fill.apply_mapping(conn, paths, Config(), client, job.id, make_mapping())
    assert result.error is not None
    assert "captured" in result.error


ASSIST_FORM_HTML = """
<form>
  <label for="fn">First name</label><input id="fn" name="first_name" required>
  <input type="hidden" name="cards[abc-123][baseTemplate]"
    value='{"text":"Screening","fields":[{"type":"multiple-choice","text":"Are you willing to travel?"}]}'>
  <label><input type="radio" name="cards[abc-123][field0]" value="Yes">Yes</label>
  <label><input type="radio" name="cards[abc-123][field0]" value="No">No</label>
  <label><input type="checkbox" name="survey[race]">Asian</label>
  <label><input type="checkbox" name="survey[race]">Black or African American</label>
  <label><input type="checkbox" name="survey[race]">Prefer not to disclose</label>
  <label for="cv">Resume</label><input id="cv" name="resume" type="file">
  <button type="submit">Submit application</button>
</form>
"""


def test_assist_sheet_reads_in_form_order_with_real_questions(
    conn, paths, tailored_job
):
    client = FakeFillClient(html=ASSIST_FORM_HTML)
    assert extract(conn, paths, client, tailored_job).ok
    state.approve_resume(conn, tailored_job.id, "owner")

    sheet = fill.assist_sheet(conn, paths, tailored_job.id)
    rows = [row for group in sheet["groups"] for row in group["rows"]]
    labels = [row["label"] for row in rows]

    # The card question reaches the owner as the question, not as its name.
    assert "Are you willing to travel?" in labels
    assert not any("baseTemplate" in label for label in labels)
    assert not any(label.startswith("cards[") for label in labels)

    # The resume is offered as a file to attach rather than a value to copy.
    assert not any(row["type"] == "file" for row in rows)
    assert sheet["resume"]["absolute"].endswith(".pdf")

    # One question, three choices, instead of three questions sharing an answer.
    survey = [row for row in rows if row["field_id"] == "survey[race]"]
    assert len(survey) == 1
    assert survey[0]["options"] == [
        "Asian", "Black or African American", "Prefer not to disclose",
    ]


def test_assist_sheet_carries_the_proposal_for_open_decisions(
    conn, paths, tailored_job
):
    client = FakeFillClient(html=ASSIST_FORM_HTML)
    assert extract(conn, paths, client, tailored_job).ok
    state.approve_resume(conn, tailored_job.id, "owner")
    fill.apply_mapping(
        conn, paths, Config(), client, tailored_job.id,
        FieldMapping(fields=[
            MappedField(
                field_id="cards[abc-123][field0]", needs_owner=True,
                proposed="Yes", reason="Travel is the owner's call.",
            ),
        ]),
    )

    sheet = fill.assist_sheet(conn, paths, tailored_job.id)
    rows = {
        row["field_id"]: row
        for group in sheet["groups"] for row in group["rows"]
    }
    parked = rows["cards[abc-123][field0]"]
    assert parked["needs_owner"]
    assert parked["value"] == "Yes"
    assert parked["reason"] == "Travel is the owner's call."
    assert sheet["waiting"] == 1


def test_filled_job_fills_again_for_rework(conn, paths, tailored_job):
    client = FakeFillClient()
    assert extract(conn, paths, client, tailored_job).ok
    state.approve_resume(conn, tailored_job.id, "owner")
    first = fill.apply_mapping(
        conn, paths, Config(), client, tailored_job.id,
        FieldMapping(fields=[
            MappedField(
                field_id="first_name", value="Jamie",
                source="answer_bank", confidence=1.0,
            ),
        ]),
    )
    assert first.ok
    assert repo.get_job(conn, tailored_job.id).status == "filled"

    # A replaced tab loses its values, so the same job fills again.
    second = fill.apply_mapping(
        conn, paths, Config(), client, tailored_job.id,
        FieldMapping(fields=[
            MappedField(
                field_id="first_name", value="Jamie",
                source="answer_bank", confidence=1.0,
            ),
            MappedField(field_id="resume", source="resume_file", confidence=1.0),
        ]),
    )
    assert second.ok, second.error
    assert [action["field_id"] for action in client.apply_calls[-1]] == [
        "first_name", "resume",
    ]


def test_submitted_job_does_not_fill_again(conn, paths, tailored_job):
    client = FakeFillClient()
    assert extract(conn, paths, client, tailored_job).ok
    state.approve_resume(conn, tailored_job.id, "owner")
    from conftest import force_status

    force_status(conn, tailored_job.id, "submitted")
    result = fill.apply_mapping(
        conn, paths, Config(), client, tailored_job.id,
        FieldMapping(fields=[
            MappedField(
                field_id="first_name", value="Jamie",
                source="answer_bank", confidence=1.0,
            ),
        ]),
    )
    assert not result.ok
    assert "submitted" in result.error


def _fill_and_ready(conn, paths, client, job):
    assert extract(conn, paths, client, job).ok
    state.approve_resume(conn, job.id, "owner")
    assert fill.apply_mapping(
        conn, paths, Config(), client, job.id,
        FieldMapping(fields=[
            MappedField(
                field_id="first_name", value="Jamie",
                source="answer_bank", confidence=1.0,
            ),
        ]),
    ).ok
    state.approve_submit(conn, job.id, "owner")


def test_passive_hcaptcha_parks_without_sending(conn, paths, tailored_job):
    client = FakeFillClient()
    _fill_and_ready(conn, paths, client, tailored_job)
    client.challenge = lambda job_id: {
        "present": True, "kind": "hcaptcha", "visible": False,
    }

    result = fill.submit_job(conn, paths, Config(), client, tailored_job.id)

    assert not result.ok
    parked = blockers.list_open(conn, tailored_job.id)
    assert [b.kind for b in parked] == ["captcha"]
    # Nothing is drawn, so the owner is sent to the sheet rather than the tab.
    assert "by hand" in parked[0].question
    assert "solve it there" not in parked[0].question
    assert repo.get_job(conn, tailored_job.id).status == "filled"


def test_visible_challenge_still_points_at_the_tab(conn, paths, tailored_job):
    client = FakeFillClient()
    _fill_and_ready(conn, paths, client, tailored_job)
    client.challenge = lambda job_id: {
        "present": True, "kind": "recaptcha", "visible": True,
    }

    assert not fill.submit_job(conn, paths, Config(), client, tailored_job.id).ok
    parked = blockers.list_open(conn, tailored_job.id)
    assert "solve it there" in parked[0].question
