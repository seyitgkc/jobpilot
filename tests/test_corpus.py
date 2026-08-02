from jobpilot.core import artifacts, corpus, formscan, repo
from test_formscan import FORM

FORM_DRIFTED = FORM.replace(
    '<label for="em">Email address</label><input id="em" name="email" type="email">',
    "",
).replace(
    "<option>7+</option>",
    "<option>7+</option><option>10+</option>",
)


def seed(conn, paths, html, method="dom_generic"):
    job = repo.create_job(conn, "https://example.com/apply", "claude", company="Acme")
    artifacts.store_text(conn, paths, job.id, "snapshot_html", "snapshot.html", html, "claude")
    schema = formscan.scan(FORM)
    artifacts.store_text(
        conn, paths, job.id, "form_schema_json", "form_schema.json",
        schema.model_dump_json(), "claude",
    )
    repo.update_application_fill(conn, job.id, "claude", method=method)
    return job


def test_stable_snapshot_passes(conn, paths):
    job = seed(conn, paths, FORM)
    report = corpus.corpus_check(conn, paths)
    assert report["checked"] == 1
    assert report["ok"] == [job.id]
    assert report["drift"] == {}


def test_drift_reported(conn, paths):
    job = seed(conn, paths, FORM_DRIFTED)
    report = corpus.corpus_check(conn, paths)
    drift = report["drift"][job.id]
    assert "email" in drift["missing_fields"]
    assert any(
        change["field_id"] == "experience" and "options changed" in change["changes"]
        for change in drift["changed"]
    )


def test_json_adapter_schemas_skipped(conn, paths):
    job = seed(conn, paths, FORM, method="adapter:greenhouse")
    report = corpus.corpus_check(conn, paths)
    assert report["checked"] == 0
    assert job.id in report["skipped"]


def test_jobs_without_schema_ignored(conn, paths):
    repo.create_job(conn, "https://example.com/x", "claude")
    report = corpus.corpus_check(conn, paths)
    assert report["checked"] == 0
    assert report["drift"] == {} and report["skipped"] == {}
