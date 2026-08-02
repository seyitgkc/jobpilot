"""Posting text extraction: rendered HTML to posting.md.

Readability-style, not pixel-faithful: pick the content root, strip chrome and
interactive elements, convert the rest to markdown. posting.md is what the
tailoring model reads, so the goal is complete text with minimal boilerplate.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from markdownify import markdownify

from . import boards, injection

_STRIP_TAGS = (
    "nav",
    "header",
    "footer",
    "aside",
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "button",
    "form",
    "img",
    "picture",
    "video",
)

_GENERIC_ROOTS = ("main", "article", "[role='main']", "#content", ".content")


def _content_root(soup: BeautifulSoup, board: str):
    for selector in boards.CONTENT_SELECTORS.get(board, ()) + _GENERIC_ROOTS:
        node = soup.select_one(selector)
        if node is not None and node.get_text(strip=True):
            return node
    return soup.body or soup


def posting_markdown(html: str, source_url: str, board: str = "other") -> str:
    """Extract one page into markdown, headed by a source marker so
    multi-page postings concatenate traceably."""
    soup = BeautifulSoup(html, "html.parser")
    root = _content_root(soup, board)
    for tag in root.find_all(_STRIP_TAGS):
        tag.decompose()
    markdown = markdownify(str(root), heading_style="ATX")
    markdown = injection.strip_invisible(markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return f"<!-- source: {source_url} -->\n\n{markdown}\n"
