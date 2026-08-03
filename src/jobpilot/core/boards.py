"""Board detection and posting metadata extraction.

Detection runs twice per capture: once on the submitted URL, then again on the
canonical URL and rendered DOM, which catches embedded boards (a careers page
hosting a Greenhouse form) and postings behind redirect wrappers. req_id is the
mail anchor, so the patterns here are the ones the mail matcher depends on;
they are pinned by the golden-corpus tests as captures accumulate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, urlparse

BOARDS = frozenset(
    {"greenhouse", "lever", "ashby", "smartrecruiters", "workday", "bamboohr", "other"}
)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Workday requisition ids trail the last path segment: Role_R-12345,
# Role_JR-0012, Role_REQ-2025-441.
_WORKDAY_REQ = re.compile(r"_((?:R|JR|REQ)-?[\d][\d-]*)$", re.IGNORECASE)

_GH_JID = re.compile(r"gh_jid=(\d+)")


@dataclass(frozen=True)
class BoardInfo:
    board: str
    req_id: str | None = None
    company_slug: str | None = None
    extra_capture_urls: tuple[str, ...] = ()


def detect_from_url(url: str) -> BoardInfo:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    segments = [part for part in parsed.path.split("/") if part]

    if host.endswith("greenhouse.io"):
        slug = segments[0] if segments else None
        req_id = None
        if "jobs" in segments:
            index = segments.index("jobs")
            if index + 1 < len(segments) and segments[index + 1].isdigit():
                req_id = segments[index + 1]
        return BoardInfo("greenhouse", req_id=req_id, company_slug=slug)

    if host.endswith("lever.co"):
        slug = segments[0] if segments else None
        req_id = None
        for part in segments:
            if re.fullmatch(_UUID, part):
                req_id = part.lower()
                break
        extras = ()
        if req_id and (not segments or segments[-1] != "apply"):
            extras = (url.rstrip("/") + "/apply",)
        return BoardInfo("lever", req_id=req_id, company_slug=slug, extra_capture_urls=extras)

    if host.endswith("ashbyhq.com"):
        slug = segments[0] if segments else None
        req_id = None
        for part in segments:
            if re.fullmatch(_UUID, part):
                req_id = part.lower()
                break
        return BoardInfo("ashby", req_id=req_id, company_slug=slug)

    if host.endswith("smartrecruiters.com"):
        slug = None
        req_id = None
        for index, part in enumerate(segments):
            match = re.match(r"^(\d{9,})", part)
            if match:
                req_id = match.group(1)
                if index > 0:
                    slug = segments[index - 1]
                break
        if slug is None and segments:
            slug = segments[0]
        return BoardInfo("smartrecruiters", req_id=req_id, company_slug=slug)

    if host.endswith(".bamboohr.com"):
        slug = host.split(".")[0] or None
        req_id = None
        if "careers" in segments:
            index = segments.index("careers")
            if index + 1 < len(segments) and segments[index + 1].isdigit():
                req_id = segments[index + 1]
        return BoardInfo("bamboohr", req_id=req_id, company_slug=slug)

    if host.endswith("myworkdayjobs.com"):
        slug = host.split(".")[0] or None
        req_id = None
        if segments:
            match = _WORKDAY_REQ.search(segments[-1])
            if match:
                req_id = match.group(1).upper()
        return BoardInfo("workday", req_id=req_id, company_slug=slug)

    jid = _GH_JID.search(url)
    if jid:
        return BoardInfo("greenhouse", req_id=jid.group(1))

    return BoardInfo("other")


_FINGERPRINTS = (
    ("greenhouse", ("boards.greenhouse.io", "job-boards.greenhouse.io", "grnhse")),
    ("lever", ("jobs.lever.co", "lever-analytics")),
    ("ashby", ("jobs.ashbyhq.com", "ashby_embed", "ashbyhq.com/api")),
    ("workday", ("myworkdayjobs.com", "workday-client")),
    ("smartrecruiters", ("smartrecruiters.com/job-api", "jobs.smartrecruiters.com")),
)


def refine_from_html(info: BoardInfo, html: str) -> BoardInfo:
    """Second detection pass over the rendered DOM. Only fills gaps: an
    URL-detected board is never overridden by a fingerprint."""
    if info.board == "other":
        for board, markers in _FINGERPRINTS:
            if any(marker in html for marker in markers):
                info = replace(info, board=board)
                break
    if info.board == "greenhouse" and info.req_id is None:
        jid = _GH_JID.search(html)
        if jid:
            info = replace(info, req_id=jid.group(1))
    return info


# Content roots tried in order during posting.md extraction, before the
# generic main/article fallback.
CONTENT_SELECTORS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("#content", "#app_body", ".job__description"),
    "lever": (".content", ".posting-page"),
    "ashby": ("#overview", "[class*='jobPosting']"),
    "smartrecruiters": ("#st-jobDescription", ".job-sections"),
    "workday": ("[data-automation-id='jobPostingDescription']",),
    "other": (),
}

_TITLE_SPLITS = (" at ", " @ ", " - ", " | ")


def _meta(html: str, prop: str) -> str | None:
    match = re.search(
        rf"<meta[^>]+(?:property|name)=[\"']{re.escape(prop)}[\"'][^>]+content=[\"']([^\"']+)[\"']",
        html,
        re.IGNORECASE,
    )
    if match is None:
        match = re.search(
            rf"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']{re.escape(prop)}[\"']",
            html,
            re.IGNORECASE,
        )
    return match.group(1).strip() if match else None


def _title_tag(html: str) -> str | None:
    match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _h1(html: str) -> str | None:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
    return text or None


_APPLY_PREFIX = re.compile(
    r"^\s*apply\s+(?:to|for)\s+(?:this\s+)?(?:job\b[\s:-]*)?", re.IGNORECASE
)

# Page titles on a board's index rather than the posting itself.
_GENERIC_TITLES = frozenset(
    {"careers", "career", "jobs", "job", "home", "apply", "openings",
     "current openings", "job board", "we are hiring", "join us"}
)

# Boards that hand each employer its own subdomain. The tenant label is the
# only name on the page when the posting body renders through JavaScript.
_TENANT_HOSTS = (
    "hibob.com", "applytojob.com", "bamboohr.com", "icims.com",
    "myworkdayjobs.com", "recruitee.com", "personio.com", "teamtailor.com",
    "workable.com", "ashbyhq.com", "breezy.hr", "jazz.co",
)
_TENANT_SUFFIX = re.compile(r"-[0-9a-f]{5,}$")


def _normalise(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _strip_apply(text: str | None) -> str | None:
    if not text:
        return None
    return _APPLY_PREFIX.sub("", text).strip() or None


def _company_from_host(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url).netloc.lower().split(":")[0]
    if not any(host.endswith(suffix) for suffix in _TENANT_HOSTS):
        return None
    labels = host.split(".")
    if len(labels) < 3:
        return None
    label = _TENANT_SUFFIX.sub("", labels[0])
    label = label.replace("-", " ").replace("_", " ").strip()
    if not label or label in ("www", "careers", "jobs", "apply"):
        return None
    return label.title()


# Trailing segments naming the board rather than the employer.
_BOARD_SUFFIXES = frozenset(
    {"careerpage", "careerspage", "careers", "career", "jobs", "job",
     "jobboard", "hiring", "wearehiring", "joinus", "openings"}
)


def _trim_board_suffix(page_title: str) -> str:
    """Drop trailing segments that name the job board. Without this the
    employer is buried mid-string and the whole tail reads as the company."""
    for _ in range(3):
        for separator in _TITLE_SPLITS:
            head, found, tail = page_title.rpartition(separator)
            if found and _normalise(tail) in _BOARD_SUFFIXES:
                page_title = head.strip()
                break
        else:
            break
    return page_title


def _first_line(posting_md: str | None) -> str | None:
    """The posting's own heading, for pages whose metadata never names the
    role."""
    for line in (posting_md or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if not line or line.startswith("<!--"):
            continue
        return line[:120]
    return None


def extract_title_company(
    html: str,
    info: BoardInfo,
    url: str | None = None,
    posting_md: str | None = None,
) -> tuple[str | None, str | None]:
    """Best-effort title and company from the rendered page. Returns
    (title, company); either may be None."""
    page_title = _meta(html, "og:title") or _title_tag(html) or ""
    site_name = _meta(html, "og:site_name")

    # Sites that set og:site_name to the page title are naming the posting,
    # not the employer, and it would otherwise win over every other signal.
    if site_name and _normalise(site_name) == _normalise(page_title):
        site_name = None

    if info.board == "greenhouse":
        match = re.search(r"Job Application for (.+?) at (.+)", page_title)
        if match:
            return match.group(1).strip(), match.group(2).strip()

    slug = _normalise(info.company_slug)
    title = None
    company = site_name

    page_title = _trim_board_suffix(page_title)
    if page_title:
        for separator in _TITLE_SPLITS:
            if separator in page_title:
                # Split from the right: the employer is the trailing segment,
                # and a role can carry the separator inside it.
                left, _, right = page_title.rpartition(separator)
                left, right = left.strip(), right.strip()
                # Boards write this both ways round. The employer slug in the
                # URL says which side is the company; without one, the trailing
                # side is the usual convention.
                if slug and _normalise(left) == slug:
                    title, company = right, company or left
                else:
                    title, company = left, company or right
                break
        else:
            title = page_title.strip()

    heading = _h1(html)
    if heading and _normalise(heading) != _normalise(company):
        title = heading

    title = _strip_apply(title)
    if _normalise(title) in {_normalise(name) for name in _GENERIC_TITLES}:
        title = None
    title = title or _strip_apply(_first_line(posting_md))

    if company is None and info.company_slug:
        company = info.company_slug.replace("-", " ").replace("_", " ").title()
    company = company or _company_from_host(url)
    return title or None, company or None
