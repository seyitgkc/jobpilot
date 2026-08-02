import hashlib

import pytest

from jobpilot.config import Config
from jobpilot.core import blockers, capture, events, repo

GREENHOUSE_HTML = """<!doctype html>
<html><head><title>Job Application for Platform Engineer at Acme</title></head>
<body><div id="content"><h1>Platform Engineer</h1>
<p>Own the deployment platform end to end.</p></div></body></html>
"""

LEVER_MAIN_HTML = """<!doctype html>
<html><head><title>Acme - Data Engineer</title></head>
<body><div class="content"><h1>Data Engineer</h1><p>Build pipelines.</p></div></body></html>
"""

LEVER_APPLY_HTML = """<!doctype html>
<html><head><title>Acme - Data Engineer</title></head>
<body><div class="content"><p>Submit your application below.</p></div></body></html>
"""

WORKDAY_HTML = """<!doctype html>
<html><head><title>Platform Engineer</title></head>
<body><div data-automation-id="jobPostingDescription"><p>Workday role.</p></div></body></html>
"""


class FakeBrowserClient:
    def __init__(self, pages, pdf=b"%PDF-1.4 fake"):
        self.pages = pages
        self.pdf = pdf
        self.opened = []
        self.closed = []
        self._current = {}

    def open_tab(self, job_id, url):
        page = self.pages[url]
        self.opened.append((job_id, url))
        self._current[job_id] = page
        return {"url": page["final_url"], "title": page.get("title", "")}

    def archive(self, job_id):
        page = self._current[job_id]
        return {
            "html": page["html"],
            "raw_html": page.get("raw_html", page["html"]),
            "url": page["final_url"],
            "title": page.get("title", ""),
        }

    def render_pdf(self, html):
        return self.pdf

    def close_tab(self, job_id):
        self.closed.append(job_id)


def test_capture_greenhouse_with_redirect(conn, paths):
    submitted = "https://short.example/x1"
    canonical = "https://boards.greenhouse.io/acme/jobs/4567890"
    client = FakeBrowserClient(
        {submitted: {"final_url": canonical, "html": GREENHOUSE_HTML, "title": "Acme"}}
    )
    result = capture.capture(conn, paths, Config(), client, submitted)

    job = result.job
    assert job.url == submitted
    assert job.canonical_url == canonical
    assert job.board == "greenhouse"
    assert job.req_id == "4567890"
    assert job.title == "Platform Engineer"
    assert job.company == "Acme"
    assert job.status == "captured"
    assert not job.workday_flag
    assert client.closed == [job.id]

    assert set(result.artifacts) == {"snapshot_html", "snapshot_pdf", "posting_md"}
    for artifact in result.artifacts.values():
        file_path = paths.absolute(artifact.path)
        assert file_path.exists()
        assert hashlib.sha256(file_path.read_bytes()).hexdigest() == artifact.sha256

    posting = paths.absolute(result.artifacts["posting_md"].path).read_text()
    assert f"<!-- source: {canonical} -->" in posting
    assert "Own the deployment platform" in posting

    kinds = [e.kind for e in events.timeline(conn, job.id)]
    assert "job_created" in kinds and "job_captured" in kinds


def test_capture_lever_concatenates_apply_page(conn, paths):
    main = "https://jobs.lever.co/acme/f6a2c517-3b1e-4f52-9d2a-8c47e1b02f11"
    apply_url = main + "/apply"
    client = FakeBrowserClient(
        {
            main: {"final_url": main, "html": LEVER_MAIN_HTML},
            apply_url: {"final_url": apply_url, "html": LEVER_APPLY_HTML},
        }
    )
    result = capture.capture(conn, paths, Config(), client, main)

    assert result.job.board == "lever"
    assert result.job.req_id == "f6a2c517-3b1e-4f52-9d2a-8c47e1b02f11"
    posting = paths.absolute(result.artifacts["posting_md"].path).read_text()
    assert f"<!-- source: {main} -->" in posting
    assert f"<!-- source: {apply_url} -->" in posting
    assert "Build pipelines." in posting
    assert "Submit your application below." in posting
    assert [url for _job, url in client.opened] == [main, apply_url]


def test_capture_workday_parks_behind_blocker(conn, paths):
    url = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/NYC/Platform-Engineer_R-2025-1234"
    client = FakeBrowserClient({url: {"final_url": url, "html": WORKDAY_HTML}})
    result = capture.capture(conn, paths, Config(), client, url)

    job = result.job
    assert job.board == "workday"
    assert job.workday_flag
    assert job.req_id == "R-2025-1234"
    assert job.status == "manual_required"
    assert len(result.blockers) == 1
    assert result.blockers[0].kind == "workday_manual"
    assert blockers.is_blocked(conn, job.id)


def test_capture_closes_tab_on_failure(conn, paths):
    url = "https://example.com/job"

    class FailingClient(FakeBrowserClient):
        def archive(self, job_id):
            raise RuntimeError("page crashed")

    client = FailingClient({url: {"final_url": url, "html": ""}})
    with pytest.raises(RuntimeError):
        capture.capture(conn, paths, Config(), client, url)
    jobs = repo.list_jobs(conn)
    assert len(jobs) == 1
    assert client.closed == [jobs[0].id]
