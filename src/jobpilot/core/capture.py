"""Capture pipeline: URL in, job row plus three artifacts out.

Always the first action on a posting, because postings vanish. The page is
rendered in the daemon's tab (ATS pages are JS-hydrated; a plain GET is
useless), archived as single-file HTML, printed to PDF from that same archive,
and extracted to posting.md for model consumption. Workday postings are marked
fill-assist-only and parked behind a blocker immediately.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..config import Config, DataPaths
from . import artifacts, blockers, boards, extract, repo, state
from .models import Artifact, Blocker, Job

WORKDAY_QUESTION = (
    "This posting is on Workday, which jobpilot does not automate. The posting "
    "is archived and the tab can be reopened at any time; apply by hand and "
    "mark this resolved once submitted."
)


@dataclass(frozen=True)
class CaptureResult:
    job: Job
    artifacts: dict[str, Artifact]
    blockers: list[Blocker]


def capture(
    conn: sqlite3.Connection,
    paths: DataPaths,
    config: Config,
    client,
    url: str,
    actor: str = "claude",
) -> CaptureResult:
    info = boards.detect_from_url(url)
    job = repo.create_job(
        conn,
        url,
        actor,
        board=info.board,
        req_id=info.req_id,
        workday_flag=info.board == "workday",
    )

    opened = client.open_tab(job.id, url)
    try:
        page = client.archive(job.id)
        canonical_url = page["url"]
        raw_html = page.get("raw_html") or page["html"]

        # Second detection pass on the canonical URL and rendered DOM. The raw
        # DOM is used because the inlined archive drops the script tags that
        # carry embed fingerprints.
        refined = boards.detect_from_url(canonical_url)
        if refined.board == "other" and info.board != "other":
            refined = info
        refined = boards.refine_from_html(refined, raw_html)
        title, company = boards.extract_title_company(raw_html, refined)

        stored = {
            "snapshot_html": artifacts.store_text(
                conn, paths, job.id, "snapshot_html", "snapshot.html", page["html"], actor
            ),
            "snapshot_pdf": artifacts.store_bytes(
                conn, paths, job.id, "snapshot_pdf", "snapshot.pdf",
                client.render_pdf(page["html"]), actor,
            ),
        }

        posting_parts = [
            extract.posting_markdown(page["html"], canonical_url, refined.board)
        ]
        for extra_url in refined.extra_capture_urls:
            try:
                client.open_tab(job.id, extra_url)
                extra_page = client.archive(job.id)
                posting_parts.append(
                    extract.posting_markdown(
                        extra_page["html"], extra_page["url"], refined.board
                    )
                )
            except Exception:
                # The main page is already archived; a missing secondary page
                # only thins posting.md.
                continue
        stored["posting_md"] = artifacts.store_text(
            conn, paths, job.id, "posting_md", "posting.md",
            "\n\n".join(posting_parts), actor,
        )

        job = repo.update_job_capture(
            conn,
            job.id,
            actor,
            canonical_url=canonical_url,
            company=company,
            title=title,
            board=refined.board,
            req_id=refined.req_id,
            workday_flag=refined.board == "workday",
        )
    finally:
        try:
            client.close_tab(job.id)
        except Exception:
            pass

    raised: list[Blocker] = []
    if job.workday_flag:
        raised.append(
            blockers.raise_blocker(
                conn, job.id, "workday_manual", actor, question=WORKDAY_QUESTION
            )
        )
        job = state.transition(conn, job.id, "manual_required", actor)

    return CaptureResult(job=job, artifacts=stored, blockers=raised)
