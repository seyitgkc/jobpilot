import pytest

from jobpilot.core import boards

URL_CASES = [
    (
        "https://boards.greenhouse.io/acme/jobs/4567890",
        ("greenhouse", "4567890", "acme"),
    ),
    (
        "https://job-boards.greenhouse.io/acme/jobs/4567890?gh_src=abc",
        ("greenhouse", "4567890", "acme"),
    ),
    (
        "https://boards.eu.greenhouse.io/acme/jobs/999",
        ("greenhouse", "999", "acme"),
    ),
    (
        "https://careers.example.com/roles?gh_jid=1234567",
        ("greenhouse", "1234567", None),
    ),
    (
        "https://jobs.lever.co/acme/f6a2c517-3b1e-4f52-9d2a-8c47e1b02f11",
        ("lever", "f6a2c517-3b1e-4f52-9d2a-8c47e1b02f11", "acme"),
    ),
    (
        "https://jobs.lever.co/acme/f6a2c517-3b1e-4f52-9d2a-8c47e1b02f11/apply",
        ("lever", "f6a2c517-3b1e-4f52-9d2a-8c47e1b02f11", "acme"),
    ),
    (
        "https://jobs.ashbyhq.com/acme/aa11bb22-cc33-dd44-ee55-ff6677889900",
        ("ashby", "aa11bb22-cc33-dd44-ee55-ff6677889900", "acme"),
    ),
    (
        "https://jobs.smartrecruiters.com/AcmeCorp/743999912345678-platform-engineer",
        ("smartrecruiters", "743999912345678", "AcmeCorp"),
    ),
    (
        "https://acme.wd5.myworkdayjobs.com/en-US/External/job/NYC/Platform-Engineer_R-2025-1234",
        ("workday", "R-2025-1234", "acme"),
    ),
    (
        "https://acme.wd1.myworkdayjobs.com/careers/job/Berlin/Data-Engineer_JR-0042",
        ("workday", "JR-0042", "acme"),
    ),
    ("https://example.com/careers/platform-engineer", ("other", None, None)),
]


@pytest.mark.parametrize("url,expected", URL_CASES)
def test_detect_from_url(url, expected):
    info = boards.detect_from_url(url)
    assert (info.board, info.req_id, info.company_slug) == expected


def test_lever_description_page_gets_apply_extra():
    info = boards.detect_from_url(
        "https://jobs.lever.co/acme/f6a2c517-3b1e-4f52-9d2a-8c47e1b02f11"
    )
    assert info.extra_capture_urls == (
        "https://jobs.lever.co/acme/f6a2c517-3b1e-4f52-9d2a-8c47e1b02f11/apply",
    )
    apply_info = boards.detect_from_url(info.extra_capture_urls[0])
    assert apply_info.extra_capture_urls == ()


def test_workday_flag_via_detection():
    info = boards.detect_from_url(
        "https://acme.wd5.myworkdayjobs.com/en-US/External/job/NYC/Engineer_R-1"
    )
    assert info.board == "workday"


def test_refine_fingerprint_only_fills_gaps():
    embedded = boards.BoardInfo("other")
    html = '<html><script src="https://boards.greenhouse.io/embed/job_board/js"></script></html>'
    refined = boards.refine_from_html(embedded, html)
    assert refined.board == "greenhouse"

    lever = boards.BoardInfo("lever", req_id="abc")
    still_lever = boards.refine_from_html(
        lever, '<html><script src="https://boards.greenhouse.io/x"></script></html>'
    )
    assert still_lever.board == "lever"


def test_refine_pulls_gh_jid_from_dom():
    info = boards.BoardInfo("greenhouse")
    refined = boards.refine_from_html(
        info, '<a href="https://careers.example.com/apply?gh_jid=7654321">Apply</a>'
    )
    assert refined.req_id == "7654321"


def test_title_company_greenhouse_pattern():
    html = "<html><head><title>Job Application for Platform Engineer at Acme</title></head></html>"
    title, company = boards.extract_title_company(html, boards.BoardInfo("greenhouse"))
    assert title == "Platform Engineer"
    assert company == "Acme"


def test_title_company_from_meta_and_h1():
    html = (
        '<html><head><meta property="og:site_name" content="Globex">'
        "<title>Data Engineer - Globex</title></head>"
        "<body><h1>Data Engineer</h1></body></html>"
    )
    title, company = boards.extract_title_company(html, boards.BoardInfo("lever", company_slug="globex"))
    assert title == "Data Engineer"
    assert company == "Globex"


def test_company_falls_back_to_slug():
    title, company = boards.extract_title_company(
        "<html><head><title>Engineer</title></head></html>",
        boards.BoardInfo("ashby", company_slug="initech-labs"),
    )
    assert title == "Engineer"
    assert company == "Initech Labs"
