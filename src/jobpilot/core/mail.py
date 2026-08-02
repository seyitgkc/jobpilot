"""Mail tracking: store every message, match it to a job, classify, advance.

The matching ladder stops at the first hit: req_id verbatim, then thread
inheritance through References, then the heuristic score against the match
threshold. ATS sender domains alone are a weak signal because one domain
serves hundreds of companies; company tokens are the strong disambiguator.
Below the threshold the message waits in the unassigned queue with ranked
candidates and a one-click assign. IMAP usage is read-only in v1; the raw
.eml on disk is the archive and the DB row is the index.
"""

from __future__ import annotations

import email
import email.utils
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy

from bs4 import BeautifulSoup

from ..config import Config, DataPaths
from ..providers.base import ProviderCallFailed, ProviderUnavailable
from ..providers.schemas import JobSummary, MailClassification, MailMessage
from ..util import utcnow
from . import blockers, events, repo, settings, state

ATS_DOMAINS = (
    "greenhouse.io",
    "greenhouse-mail.io",
    "lever.co",
    "hire.lever.co",
    "ashbyhq.com",
    "smartrecruiters.com",
    "myworkday.com",
    "myworkdayjobs.com",
    "workday.com",
    "icims.com",
    "jobvite.com",
)

CLASSIFICATIONS = ("confirmation", "rejection", "interview", "action_required", "other")

_STATE_FOR = {
    "confirmation": "acknowledged",
    "interview": "interview",
    "rejection": "rejected",
}

_TOKEN = re.compile(r"[a-z0-9]{3,}")


@dataclass(frozen=True)
class Mail:
    id: int
    message_id: str | None
    thread_refs: str | None
    sender: str | None
    sender_domain: str | None
    subject: str | None
    received_at: str | None
    body_path: str | None
    classification: str | None
    job_id: int | None
    match_score: float | None
    match_method: str | None
    status: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Mail":
        return cls(**{key: row[key] for key in row.keys()})


def get_mail(conn: sqlite3.Connection, mail_id: int) -> Mail:
    row = conn.execute("SELECT * FROM mail WHERE id = ?", (mail_id,)).fetchone()
    if row is None:
        raise repo.NotFound(f"mail {mail_id}")
    return Mail.from_row(row)


def list_unassigned(conn: sqlite3.Connection) -> list[Mail]:
    rows = conn.execute(
        "SELECT * FROM mail WHERE status = 'needs_assignment' ORDER BY id DESC"
    ).fetchall()
    return [Mail.from_row(row) for row in rows]


def list_for_job(conn: sqlite3.Connection, job_id: int) -> list[Mail]:
    rows = conn.execute(
        "SELECT * FROM mail WHERE job_id = ? ORDER BY received_at", (job_id,)
    ).fetchall()
    return [Mail.from_row(row) for row in rows]


def body_text(paths: DataPaths, mail: Mail, limit: int = 4000) -> str:
    if not mail.body_path:
        return ""
    file = paths.absolute(mail.body_path)
    if not file.exists():
        return ""
    message = email.message_from_bytes(file.read_bytes(), policy=policy.default)
    return _extract_body(message)[:limit]


def _extract_body(message) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_type() == "text/html":
        return BeautifulSoup(content, "html.parser").get_text(" ", strip=True)
    return content


def _parse(raw: bytes) -> dict:
    message = email.message_from_bytes(raw, policy=policy.default)
    message_id = (message.get("Message-ID") or "").strip().strip("<>")
    sender = str(message.get("From") or "")
    address = email.utils.parseaddr(sender)[1]
    domain = address.split("@")[-1].lower() if "@" in address else None
    date = message.get("Date")
    try:
        received = email.utils.parsedate_to_datetime(date).astimezone(
            timezone.utc
        ).isoformat(timespec="seconds") if date else None
    except (TypeError, ValueError):
        received = None
    references = " ".join(
        ref.strip("<>")
        for header in ("References", "In-Reply-To")
        for ref in str(message.get(header) or "").split()
    )
    return {
        "message_id": message_id or None,
        "sender": sender or None,
        "sender_domain": domain,
        "subject": str(message.get("Subject") or "") or None,
        "received_at": received,
        "references": references or None,
        "body": _extract_body(message),
    }


def store_message(
    conn: sqlite3.Connection, paths: DataPaths, raw: bytes, actor: str = "worker"
) -> Mail | None:
    """Archive one raw message; returns None when the message id was already
    stored."""
    parsed = _parse(raw)
    message_id = parsed["message_id"] or f"missing-id-{hash(raw)}"
    existing = conn.execute(
        "SELECT id FROM mail WHERE message_id = ?", (message_id,)
    ).fetchone()
    if existing:
        return None

    month = (parsed["received_at"] or utcnow())[:7]
    directory = paths.mail_dir / month
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9@._-]", "_", message_id)[:120]
    file_path = directory / f"{safe_name}.eml"
    file_path.write_bytes(raw)

    with conn:
        cursor = conn.execute(
            "INSERT INTO mail(message_id, thread_refs, sender, sender_domain,"
            " subject, received_at, body_path, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'needs_assignment')",
            (
                message_id, parsed["references"], parsed["sender"],
                parsed["sender_domain"], parsed["subject"], parsed["received_at"],
                paths.relative_to_root(file_path),
            ),
        )
        mail_id = cursor.lastrowid
        events.append(
            conn, actor=actor, kind="mail_stored",
            payload={"mail_id": mail_id, "subject": parsed["subject"]},
        )
    return get_mail(conn, mail_id)


def _tokens(text: str | None) -> set[str]:
    return set(_TOKEN.findall((text or "").lower()))


def score_job(
    conn: sqlite3.Connection, mail: Mail, body: str, job
) -> float:
    haystack = " ".join(
        part for part in (mail.subject, mail.sender, body[:2000]) if part
    ).lower()
    score = 0.0
    company_tokens = {t for t in _tokens(job.company) if len(t) >= 4} or _tokens(job.company)
    if company_tokens and any(token in haystack for token in company_tokens):
        score += 0.5
    title_tokens = {t for t in _tokens(job.title) if len(t) >= 4}
    if title_tokens:
        overlap = sum(1 for token in title_tokens if token in haystack) / len(title_tokens)
        score += 0.25 * overlap
    if mail.sender_domain and any(
        mail.sender_domain.endswith(domain) for domain in ATS_DOMAINS
    ):
        score += 0.15
    application = repo.get_application(conn, job.id)
    if application and application.submitted_at and mail.received_at:
        try:
            submitted = datetime.fromisoformat(application.submitted_at)
            received = datetime.fromisoformat(mail.received_at)
            days = (received - submitted).days
            if 0 <= days <= 45:
                score += 0.1 * (1 - days / 45)
        except ValueError:
            pass
    return round(min(score, 1.0), 3)


def candidates(
    conn: sqlite3.Connection, paths: DataPaths, mail: Mail, limit: int = 3
) -> list[dict]:
    body = body_text(paths, mail)
    ranked = []
    for job in repo.list_jobs(conn):
        if job.status == "abandoned":
            continue
        score = score_job(conn, mail, body, job)
        if score > 0:
            ranked.append({"job": job, "score": score})
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:limit]


def _match(conn: sqlite3.Connection, paths: DataPaths, mail: Mail, config: Config) -> Mail:
    body = body_text(paths, mail)
    haystack = f"{mail.subject or ''} {body}"

    for job in repo.list_jobs(conn):
        if job.req_id and len(job.req_id) >= 4 and job.req_id in haystack:
            return _assign_row(conn, mail.id, job.id, 1.0, "req_id", "auto_matched")

    if mail.thread_refs:
        refs = set(mail.thread_refs.split())
        if refs:
            placeholders = ", ".join("?" for _ in refs)
            row = conn.execute(
                f"SELECT job_id FROM mail WHERE job_id IS NOT NULL"
                f" AND message_id IN ({placeholders}) LIMIT 1",
                tuple(refs),
            ).fetchone()
            if row:
                return _assign_row(
                    conn, mail.id, row["job_id"], 1.0, "thread", "auto_matched"
                )

    best_job, best_score = None, 0.0
    for job in repo.list_jobs(conn):
        if job.status == "abandoned":
            continue
        score = score_job(conn, mail, body, job)
        if score > best_score:
            best_job, best_score = job, score
    if best_job is not None and best_score >= config.mail.match_threshold:
        return _assign_row(
            conn, mail.id, best_job.id, best_score, "heuristic", "auto_matched"
        )
    return mail


def _assign_row(
    conn: sqlite3.Connection,
    mail_id: int,
    job_id: int,
    score: float,
    method: str,
    status: str,
) -> Mail:
    with conn:
        conn.execute(
            "UPDATE mail SET job_id = ?, match_score = ?, match_method = ?,"
            " status = ? WHERE id = ?",
            (job_id, score, method, status, mail_id),
        )
        events.append(
            conn, actor="worker", kind="mail_matched", job_id=job_id,
            payload={"mail_id": mail_id, "method": method, "score": score},
        )
    return get_mail(conn, mail_id)


def assign(
    conn: sqlite3.Connection, paths: DataPaths, mail_id: int, job_id: int, actor: str
) -> Mail:
    repo.get_job(conn, job_id)
    mail = _assign_row(conn, mail_id, job_id, 1.0, "manual", "assigned")
    if mail.classification:
        _advance(conn, mail, actor)
    return mail


def to_message(paths: DataPaths, mail: Mail) -> MailMessage:
    return MailMessage(
        message_id=mail.message_id or "",
        sender=mail.sender,
        sender_domain=mail.sender_domain,
        subject=mail.subject,
        body_text=body_text(paths, mail),
        received_at=mail.received_at,
    )


def open_job_summaries(conn: sqlite3.Connection) -> list[JobSummary]:
    return [
        JobSummary(
            id=job.id, company=job.company, title=job.title,
            board=job.board, url=job.canonical_url or job.url,
        )
        for job in repo.list_jobs(conn)
        if job.status not in ("rejected", "offer", "abandoned")
    ]


def set_classification(
    conn: sqlite3.Connection, mail_id: int, classification: str, actor: str
) -> Mail:
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"unknown classification {classification!r}")
    mail = get_mail(conn, mail_id)
    with conn:
        conn.execute(
            "UPDATE mail SET classification = ? WHERE id = ?",
            (classification, mail_id),
        )
        events.append(
            conn, actor=actor, kind="mail_classified", job_id=mail.job_id,
            payload={"mail_id": mail_id, "classification": classification},
        )
    mail = get_mail(conn, mail_id)
    if mail.job_id is not None:
        _advance(conn, mail, actor)
    return get_mail(conn, mail_id)


def _advance(conn: sqlite3.Connection, mail: Mail, actor: str) -> None:
    """Mail-driven status changes, applied only when legal from the current
    status; an out-of-order message becomes a timeline note instead."""
    if mail.classification == "action_required":
        already = any(
            blocker.question and (mail.subject or "") in blocker.question
            for blocker in blockers.list_open(conn, mail.job_id)
        )
        if not already:
            blockers.raise_blocker(
                conn, mail.job_id, "other", actor,
                question=f"Mail needs action: {mail.subject or 'no subject'}",
            )
        return
    target = _STATE_FOR.get(mail.classification or "")
    if target is None:
        return
    job = repo.get_job(conn, mail.job_id)
    if state.is_legal(job.status, target):
        state.transition(
            conn, mail.job_id, target, actor, payload={"mail_id": mail.id}
        )
    else:
        events.append(
            conn, actor=actor, kind="mail_noted", job_id=mail.job_id,
            payload={
                "mail_id": mail.id,
                "classification": mail.classification,
                "status": job.status,
            },
        )
        conn.commit()


def sync(
    conn: sqlite3.Connection,
    paths: DataPaths,
    config: Config,
    provider=None,
    fetch=None,
    actor: str = "worker",
) -> dict:
    """Pull new messages, store, match, classify, advance. The fetcher is
    injectable; the default reads IMAP with the configured account."""
    if fetch is None:
        fetch = lambda: _imap_fetch(conn, config)  # noqa: E731
    fetched = stored = matched = classified = 0
    for raw in fetch():
        fetched += 1
        mail = store_message(conn, paths, raw, actor)
        if mail is None:
            continue
        stored += 1
        mail = _match(conn, paths, mail, config)
        if mail.job_id is not None:
            matched += 1
        if provider is not None:
            try:
                result = provider.classify_mail(
                    to_message(paths, mail), open_job_summaries(conn)
                )
                set_classification(conn, mail.id, result.classification, actor)
                classified += 1
            except (ProviderUnavailable, ProviderCallFailed):
                pass
    return {
        "fetched": fetched,
        "stored": stored,
        "matched": matched,
        "classified": classified,
        "unassigned": len(list_unassigned(conn)),
    }


KEYCHAIN_SERVICE = "jobpilot-imap"


def imap_password(config: Config) -> str:
    """The IMAP secret: the configured environment variable when set, else
    the macOS keychain item for the account. The secret never lives in
    config or the database."""
    import os
    import subprocess
    import sys

    password = os.environ.get(config.mail.imap_pass_env or "", "")
    if password:
        return password
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                [
                    "security", "find-generic-password",
                    "-s", KEYCHAIN_SERVICE,
                    "-a", config.mail.imap_user,
                    "-w",
                ],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise RuntimeError(
        f"IMAP password not found. Set ${config.mail.imap_pass_env}, or store "
        f"it in the keychain with: security add-generic-password "
        f"-s {KEYCHAIN_SERVICE} -a {config.mail.imap_user} -w"
    )


def _imap_fetch(conn: sqlite3.Connection, config: Config):
    """Read-only IMAP pull of UIDs above the stored cursor."""
    if not config.mail.imap_host or not config.mail.imap_user:
        return

    from imapclient import IMAPClient

    password = imap_password(config)
    last_uid = int(settings.get(conn, "mail.last_uid", "0"))
    with IMAPClient(config.mail.imap_host, ssl=True) as client:
        client.login(config.mail.imap_user, password)
        client.select_folder("INBOX", readonly=not config.mail.mark_seen)
        if last_uid:
            uids = [uid for uid in client.search("ALL") if uid > last_uid]
        else:
            # First run: bound the backfill to recent mail instead of
            # archiving the whole mailbox history.
            since = (datetime.now(timezone.utc) - timedelta(days=30)).date()
            uids = client.search(["SINCE", since])
        for uid in sorted(uids):
            data = client.fetch([uid], ["BODY.PEEK[]"])
            entry = data.get(uid) or {}
            raw = entry.get(b"BODY[]") or entry.get(b"RFC822")
            if raw is None:
                settings.set(conn, "mail.last_uid", str(uid), "worker")
                continue
            yield raw
            settings.set(conn, "mail.last_uid", str(uid), "worker")
            if config.mail.mark_seen:
                client.add_flags([uid], ["\\Seen"])
