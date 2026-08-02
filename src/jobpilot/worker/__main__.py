"""Run the worker: jobpilot-worker or python -m jobpilot.worker."""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.blocking import BlockingScheduler

from .. import providers
from ..config import Config, DataPaths, load_config
from ..core import agenda, mail, notify, resume, tailor
from ..db import open_db


def drain_tailoring(conn, paths: DataPaths, config: Config, provider, log, compile_fn=None) -> int:
    """Tailor every job waiting for it. Failures park behind blockers per
    the provider path; a broken fact base skips the whole pass."""
    waiting = agenda.agenda(conn)["to_tailor"]
    done = 0
    for entry in waiting:
        try:
            result = tailor.tailor_job(
                conn, paths, config, provider, entry["id"],
                actor="worker", compile_fn=compile_fn,
            )
        except resume.ResumeSetupError as error:
            log.warning("tailoring skipped: %s", error)
            return done
        except ValueError as error:
            log.warning("job %s not tailorable: %s", entry["id"], error)
            continue
        if result.ok:
            done += 1
            log.info("tailored job %s (%s)", entry["id"], entry.get("company"))
        else:
            log.warning(
                "job %s parked: %s", entry["id"],
                result.blocker.question if result.blocker else result.error,
            )
    return done


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("jobpilot.worker")
    config = load_config()
    paths = DataPaths.from_config(config)
    conn = open_db(paths, config)
    provider = providers.from_config(config)
    can_tailor = (
        config.ai.provider == "anthropic" and bool(os.environ.get("ANTHROPIC_API_KEY"))
    )

    def mail_sync() -> None:
        try:
            result = mail.sync(conn, paths, config, provider=provider)
            log.info("mail sync: %s", result)
        except Exception as error:
            log.warning("mail sync failed: %s", error)

    def blocker_reminders() -> None:
        try:
            sent = notify.remind_stale_blockers(conn, config)
            if sent:
                log.info("blocker reminders sent: %d", sent)
        except Exception as error:
            log.warning("blocker reminders failed: %s", error)

    def tailor_pending() -> None:
        try:
            drain_tailoring(conn, paths, config, provider, log)
        except Exception as error:
            log.warning("tailoring pass failed: %s", error)

    scheduler = BlockingScheduler()
    scheduler.add_job(mail_sync, "interval", seconds=config.mail.poll_seconds)
    scheduler.add_job(blocker_reminders, "interval", minutes=30)
    if can_tailor:
        scheduler.add_job(tailor_pending, "interval", minutes=5)
        log.info("tailoring drain on: captured jobs tailor automatically")
    else:
        log.info("no provider key; tailoring stays with chat sessions")
    log.info(
        "worker started: mail every %ss, blocker reminders every 30m",
        config.mail.poll_seconds,
    )
    mail_sync()
    if can_tailor:
        tailor_pending()
    scheduler.start()


if __name__ == "__main__":
    main()
