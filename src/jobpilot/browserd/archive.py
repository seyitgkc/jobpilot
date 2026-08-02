"""Single-file HTML archiving.

Takes the serialized post-hydration DOM and inlines subresources as data URIs:
stylesheets (including url() references inside them) and images. Scripts are
dropped; the DOM is already hydrated and re-running scripts against a moved
page breaks more than it preserves. Fetch failures leave the original
reference in place; an imperfect archive beats a failed capture.

The fetch callable is injected so the daemon can fetch with the browser
context's cookies while tests use a fake.
"""

from __future__ import annotations

import base64
import mimetypes
import re
from typing import Awaitable, Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

# fetch(absolute_url) -> (body, content_type or None), or None on failure.
Fetcher = Callable[[str], Awaitable[tuple[bytes, str | None] | None]]

MAX_RESOURCE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024

_CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+?)['\"]?\s*\)")
_DROP_TAGS = ("script", "noscript")
_DROP_LINK_RELS = {"preload", "prefetch", "modulepreload", "dns-prefetch", "preconnect"}


def _skippable(reference: str) -> bool:
    reference = reference.strip()
    return (
        not reference
        or reference.startswith("data:")
        or reference.startswith("#")
        or reference.startswith("javascript:")
        or reference.startswith("blob:")
    )


class _Budget:
    def __init__(self, limit: int):
        self.remaining = limit

    def take(self, size: int) -> bool:
        if size > self.remaining:
            return False
        self.remaining -= size
        return True


def _data_uri(body: bytes, content_type: str | None, url: str) -> str:
    mime = content_type or mimetypes.guess_type(url)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(body).decode()}"


async def _fetch_limited(
    fetch: Fetcher, url: str, budget: _Budget
) -> tuple[bytes, str | None] | None:
    result = await fetch(url)
    if result is None:
        return None
    body, content_type = result
    if len(body) > MAX_RESOURCE_BYTES or not budget.take(len(body)):
        return None
    return body, content_type


async def _inline_css_refs(
    css: str, css_url: str, fetch: Fetcher, budget: _Budget
) -> str:
    replacements: dict[str, str] = {}
    for reference in set(_CSS_URL.findall(css)):
        if _skippable(reference):
            continue
        absolute = urljoin(css_url, reference)
        fetched = await _fetch_limited(fetch, absolute, budget)
        if fetched is None:
            continue
        body, content_type = fetched
        replacements[reference] = _data_uri(body, content_type, absolute)
    if not replacements:
        return css

    def substitute(match: re.Match) -> str:
        reference = match.group(1)
        if reference in replacements:
            return f"url({replacements[reference]})"
        return match.group(0)

    return _CSS_URL.sub(substitute, css)


async def inline_html(html: str, base_url: str, fetch: Fetcher) -> str:
    soup = BeautifulSoup(html, "html.parser")
    budget = _Budget(MAX_TOTAL_BYTES)

    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()

    base = soup.find("base")
    if base is not None:
        base_url = urljoin(base_url, base.get("href", ""))
        base.decompose()

    for link in list(soup.find_all("link")):
        rels = {rel.lower() for rel in (link.get("rel") or [])}
        if rels & _DROP_LINK_RELS:
            link.decompose()
            continue
        if "stylesheet" not in rels:
            continue
        href = link.get("href", "")
        if _skippable(href):
            continue
        absolute = urljoin(base_url, href)
        fetched = await _fetch_limited(fetch, absolute, budget)
        if fetched is None:
            continue
        body, _content_type = fetched
        css = body.decode("utf-8", errors="replace")
        css = await _inline_css_refs(css, absolute, fetch, budget)
        style = soup.new_tag("style")
        style.string = css
        link.replace_with(style)

    for style in soup.find_all("style"):
        if style.string:
            style.string = await _inline_css_refs(
                style.string, base_url, fetch, budget
            )

    for img in soup.find_all("img"):
        source = img.get("src", "")
        if img.has_attr("srcset"):
            del img["srcset"]
        if _skippable(source):
            continue
        absolute = urljoin(base_url, source)
        fetched = await _fetch_limited(fetch, absolute, budget)
        if fetched is None:
            continue
        body, content_type = fetched
        img["src"] = _data_uri(body, content_type, absolute)

    return str(soup)
