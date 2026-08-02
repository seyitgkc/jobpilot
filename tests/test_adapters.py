from jobpilot.core import adapters, repo

GREENHOUSE_JSON = {
    "questions": [
        {
            "label": "First Name",
            "required": True,
            "fields": [{"name": "first_name", "type": "input_text", "values": []}],
        },
        {
            "label": "Resume",
            "required": True,
            "fields": [{"name": "resume", "type": "input_file", "values": []}],
        },
        {
            "label": "How did you hear about this job?",
            "required": False,
            "fields": [
                {
                    "name": "question_123",
                    "type": "multi_value_single_select",
                    "values": [
                        {"label": "Job board", "value": 1},
                        {"label": "Referral", "value": 2},
                    ],
                }
            ],
        },
        {
            "label": "Anything else?",
            "required": False,
            "fields": [{"name": "question_124", "type": "textarea", "values": []}],
        },
    ],
    "location_questions": [
        {
            "label": "City",
            "required": True,
            "fields": [{"name": "city", "type": "input_text", "values": []}],
        }
    ],
}


def gh_job(conn):
    return repo.create_job(
        conn, "https://boards.greenhouse.io/acme/jobs/4567890", "claude",
        company="Acme", title="Platform Engineer", board="greenhouse",
        req_id="4567890",
    )


def test_greenhouse_json_schema(conn):
    job = gh_job(conn)
    schema, method = adapters.extract_schema(
        job, "<html></html>", fetch_json=lambda url: GREENHOUSE_JSON
    )
    assert method == "adapter:greenhouse"
    ids = [field.field_id for field in schema.fields]
    assert ids == ["first_name", "resume", "question_123", "question_124", "city"]
    select = schema.by_id("question_123")
    assert select.type == "select"
    assert select.options == ["Job board", "Referral"]
    assert schema.by_id("resume").type == "file"
    assert schema.by_id("city").section == "Location"
    assert schema.submit_button.label == "Submit application"


def test_greenhouse_falls_back_to_dom(conn):
    job = gh_job(conn)
    html = "<form><label for='a'>Name</label><input id='a' name='name'></form>"
    schema, method = adapters.extract_schema(job, html, fetch_json=lambda url: None)
    assert method == "dom_generic"
    assert [field.field_id for field in schema.fields] == ["name"]


def test_other_boards_use_dom(conn):
    lever = repo.create_job(
        conn, "https://jobs.lever.co/acme/f6a2c517-3b1e-4f52-9d2a-8c47e1b02f11",
        "claude", board="lever",
    )
    html = "<form><label for='a'>Full name</label><input id='a' name='name'></form>"
    schema, method = adapters.extract_schema(lever, html)
    assert method == "dom_generic"
    assert schema.by_id("name").label == "Full name"
