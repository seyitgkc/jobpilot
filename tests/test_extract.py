from jobpilot.core import extract

PAGE = """
<html><head><title>Platform Engineer at Acme</title></head>
<body>
<nav><a href="/">Home</a><a href="/jobs">All jobs</a></nav>
<header>Acme careers</header>
<main>
  <h1>Platform Engineer</h1>
  <p>Build and run the deployment platform.</p>
  <ul><li>5+ years with Python</li><li>Kubernetes in production</li></ul>
  <img src="team.jpg">
  <form><input name="email"><button>Apply</button></form>
</main>
<footer>All rights reserved</footer>
</body></html>
"""


def test_markdown_keeps_content_drops_chrome():
    markdown = extract.posting_markdown(PAGE, "https://example.com/job", "other")
    assert "# Platform Engineer" in markdown
    assert "Build and run the deployment platform." in markdown
    assert "5+ years with Python" in markdown
    assert "All jobs" not in markdown
    assert "All rights reserved" not in markdown
    assert "team.jpg" not in markdown
    assert "Apply" not in markdown


def test_source_marker_present():
    markdown = extract.posting_markdown(PAGE, "https://example.com/job", "other")
    assert markdown.startswith("<!-- source: https://example.com/job -->")


def test_board_selector_preferred():
    html = (
        "<body><div id='content'><p>Greenhouse body text</p></div>"
        "<div><p>Sidebar noise</p></div></body>"
    )
    markdown = extract.posting_markdown(html, "https://x.test", "greenhouse")
    assert "Greenhouse body text" in markdown
    assert "Sidebar noise" not in markdown


def test_body_fallback_when_no_root_matches():
    html = "<body><p>Bare text</p></body>"
    markdown = extract.posting_markdown(html, "https://x.test", "other")
    assert "Bare text" in markdown
