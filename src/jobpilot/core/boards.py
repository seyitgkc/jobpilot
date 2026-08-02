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


def extract_title_company(html: str, info: BoardInfo) -> tuple[str | None, str | None]:
    """Best-effort title and company from the rendered page. Returns
    (title, company); either may be None."""
    page_title = _meta(html, "og:title") or _title_tag(html) or ""
    site_name = _meta(html, "og:site_name")

    if info.board == "greenhouse":
        match = re.search(r"Job Application for (.+?) at (.+)", page_title)
        if match:
            return match.group(1).strip(), match.group(2).strip()

    title = _h1(html)
    company = site_name

    if page_title:
        for separator in _TITLE_SPLITS:
            if separator in page_title:
                left, right = page_title.split(separator, 1)
                title = title or left.strip()
                company = company or right.strip()
                break
        else:
            title = title or page_title.strip()

    if company is None and info.company_slug:
        company = info.company_slug.replace("-", " ").replace("_", " ").title()
    return title or None, company or None
