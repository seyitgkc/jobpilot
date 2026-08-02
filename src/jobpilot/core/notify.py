"""Notification strategies.

CoworkNotifier is a no-op because the chat platform delivers the push when
the session asks in-thread; FallbackNotifier reaches the owner through ntfy
and best-effort local mail with a deep link into the panel. The degraded
path stays fully functional: every notification points at a panel page that
can resolve the matter alone. Delivery is always best effort and never
breaks the pipeline that triggered it.
"""

from __future__ import annotations

import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx

from ..config import Config, load_config
from . import blockers, events, settings
from .models import Blocker, Job


def _deep_link(config: Config, blocker_id: int) -> str:
    return f"http://127.0.0.1:{config.admin.port}/blockers/{blocker_id}"


class CoworkNotifier:
    def notify_blocker(self, blocker: Blocker) -> None:
        return None

    def notify_status(self, job: Job, msg: str) -> None:
        return None


class FallbackNotifier:
    def __init__(self, config: Config, post=httpx.post, smtp_factory=smtplib.SMTP):
        self.config = config
        self.post = post
        self.smtp_factory = smtp_factory

    def _send(self, title: str, body: str, link: str | None = None) -> bool:
        sent = False
        topic = self.config.notify.ntfy_topic
        if topic and "ntfy" in self.config.notify.channels:
            url = topic if "://" in topic else f"https://ntfy.sh/{topic}"
            headers = {"Title": title}
            if link:
                headers["Click"] = link
            try:
                self.post(url, content=body.encode(), headers=headers, timeout=10)
                sent = True
            except Exception:
                pass
        recipient = self.config.notify.email_to
        if recipient and "email" in self.config.notify.channels:
            message = EmailMessage()
            message["Subject"] = title
            message["To"] = recipient
            message["From"] = f"jobpilot@localhost"
            message.set_content(body + (f"\n\n{link}" if link else ""))
            try:
                with self.smtp_factory("localhost") as smtp:
                    smtp.send_message(message)
                sent = True
            except Exception:
                pass
        return sent

    def notify_blocker(self, blocker: Blocker) -> None:
        self._send(
            "jobpilot needs you",
            blocker.question or f"A {blocker.kind} blocker is waiting.",
            _deep_link(self.config, blocker.id),
        )

    def notify_status(self, job: Job, msg: str) -> None:
        self._send(f"jobpilot: {job.company or 'job'} update", msg)


def blocker_raised_best_effort(blocker: Blocker) -> None:
    """Fire the fallback channels for a fresh blocker. Loads config lazily
    so low-level callers stay config-free; with no channels configured this
    does nothing."""
    try:
        config = load_config()
        FallbackNotifier(config).notify_blocker(blocker)
    except Exception:
        pass


def remind_stale_blockers(
    conn: sqlite3.Connection, config: Config, notifier: FallbackNotifier | None = None
) -> int:
    """One summary reminder covering every blocker that has been waiting
    longer than the configured window; at most one reminder per window."""
    hours = config.notify.reminder_hours
    if hours <= 0:
        return 0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    last = settings.get(conn, "notify.last_reminder")
    if last:
        try:
            if datetime.fromisoformat(last) > cutoff:
                return 0
        except ValueError:
            pass

    stale = [
        blocker
        for blocker in blockers.list_open(conn)
        if datetime.fromisoformat(blocker.raised_at) < cutoff
    ]
    if not stale:
        return 0

    notifier = notifier or FallbackNotifier(config)
    lines = [
        f"#{blocker.id} [{blocker.kind}] {blocker.question or ''}".strip()
        for blocker in stale[:10]
    ]
    notifier._send(
        f"jobpilot: {len(stale)} blocker(s) still waiting",
        "\n".join(lines),
        f"http://127.0.0.1:{config.admin.port}/blockers",
    )
    settings.set(conn, "notify.last_reminder", now.isoformat(timespec="seconds"), "worker")
    events.append(
        conn, actor="worker", kind="blockers_reminded",
        payload={"count": len(stale)},
    )
    conn.commit()
    return len(stale)
