"""End-to-end daemon tests: real Chromium (headless), real HTTP, local fixture
site. Skipped when the Playwright Chromium build is not installed."""

import base64
import http.server
import socket
import threading
import time

import pytest

from jobpilot.browserd.app import create_app
from jobpilot.config import BrowserConfig, Config, DataPaths
from jobpilot.core import capture as capture_pipeline
from jobpilot.core import repo
from jobpilot.core.browser_client import BrowserClient, BrowserDaemonError
from jobpilot.db import open_db

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

JOB_HTML = """<!doctype html>
<html><head><title>Job Application for Platform Engineer at Acme</title>
<link rel="stylesheet" href="style.css">
<script src="app.js"></script>
</head>
<body>
<div id="grnhse_app"></div>
<nav><a href="/">Careers home</a></nav>
<div id="content">
  <h1>Platform Engineer</h1>
  <p>Own the deployment platform end to end.</p>
  <img src="logo.png" alt="team">
  <a href="https://careers.acme.test/apply?gh_jid=4567890">Apply now</a>
</div>
<footer>Acme Inc</footer>
</body></html>
"""

STYLE_CSS = "body { background: url('logo.png'); font-family: sans-serif; }"
APP_JS = "document.title = document.title;"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def chromium():
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            playwright.chromium.launch(headless=True).close()
    except Exception as error:
        pytest.skip(f"playwright chromium unavailable: {error}")


@pytest.fixture(scope="module")
def site_url(tmp_path_factory):
    directory = tmp_path_factory.mktemp("site")
    (directory / "job.html").write_text(JOB_HTML)
    (directory / "style.css").write_text(STYLE_CSS)
    (directory / "app.js").write_text(APP_JS)
    (directory / "logo.png").write_bytes(PNG_1PX)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def env(chromium, tmp_path_factory):
    import uvicorn

    data_root = tmp_path_factory.mktemp("data")
    paths = DataPaths(data_root=data_root)
    config = Config(browser=BrowserConfig(tab_cap=2, settle_ms=50))
    port = _free_port()
    app = create_app(config, paths, headless=True)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started:
        if time.time() > deadline:
            pytest.fail("daemon did not start within 30s")
        time.sleep(0.05)

    client = BrowserClient(f"http://127.0.0.1:{port}")
    conn = open_db(paths, config)
    yield {"client": client, "conn": conn, "paths": paths, "config": config}
    conn.close()
    client.close()
    server.should_exit = True
    thread.join(timeout=15)


def _make_job(env, url):
    return repo.create_job(env["conn"], url, "claude")


def test_open_tab_unknown_job_is_404(env, site_url):
    with pytest.raises(BrowserDaemonError) as excinfo:
        env["client"].open_tab(9999, f"{site_url}/job.html")
    assert excinfo.value.status_code == 404


def test_tab_roundtrip(env, site_url):
    client = env["client"]
    job = _make_job(env, f"{site_url}/job.html")
    opened = client.open_tab(job.id, f"{site_url}/job.html")
    assert opened["url"].endswith("/job.html")
    assert "Platform Engineer" in opened["title"]
    assert repo.get_job(env["conn"], job.id).tab_state == "open"
    assert client.health()["open_tabs"] == [job.id]

    snapshot = client.snapshot(job.id)
    assert "Platform Engineer" in snapshot["a11y_tree"]

    archive = client.archive(job.id)
    assert "<script" not in archive["html"]
    assert "<script" in archive["raw_html"]
    encoded = base64.b64encode(PNG_1PX).decode()
    assert f"data:image/png;base64,{encoded}" in archive["html"]
    assert "font-family: sans-serif" in archive["html"]

    image = client.screenshot(job.id)
    assert image[:4] == b"\x89PNG"
    cropped = client.screenshot(job.id, selector="#content")
    assert cropped[:4] == b"\x89PNG"
    assert len(cropped) != len(image)

    pdf = client.render_pdf(archive["html"])
    assert pdf[:5] == b"%PDF-"

    client.close_tab(job.id)
    assert repo.get_job(env["conn"], job.id).tab_state == "none"
    assert client.health()["open_tabs"] == []


def test_tab_cap_enforced(env, site_url):
    client = env["client"]
    jobs = [_make_job(env, f"{site_url}/job.html") for _ in range(3)]
    try:
        client.open_tab(jobs[0].id, f"{site_url}/job.html")
        client.open_tab(jobs[1].id, f"{site_url}/job.html")
        with pytest.raises(BrowserDaemonError) as excinfo:
            client.open_tab(jobs[2].id, f"{site_url}/job.html")
        assert excinfo.value.status_code == 409
        # Renavigating an existing tab stays allowed at the cap.
        client.open_tab(jobs[0].id, f"{site_url}/job.html")
    finally:
        for job in jobs[:2]:
            client.close_tab(job.id)


def test_capture_end_to_end(env, site_url):
    result = capture_pipeline.capture(
        env["conn"], env["paths"], env["config"], env["client"], f"{site_url}/job.html"
    )
    job = result.job
    assert job.board == "greenhouse"
    assert job.req_id == "4567890"
    assert job.title == "Platform Engineer"
    assert job.company == "Acme"
    assert job.status == "captured"
    assert repo.get_job(env["conn"], job.id).tab_state == "none"

    html = env["paths"].absolute(result.artifacts["snapshot_html"].path).read_text()
    assert "data:image/png;base64," in html
    pdf = env["paths"].absolute(result.artifacts["snapshot_pdf"].path).read_bytes()
    assert pdf[:5] == b"%PDF-"
    posting = env["paths"].absolute(result.artifacts["posting_md"].path).read_text()
    assert "Own the deployment platform" in posting
    assert "Careers home" not in posting
