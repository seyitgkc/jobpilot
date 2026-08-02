"""Run the worker: jobpilot-worker or python -m jobpilot.worker."""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from .. import providers
from ..config import DataPaths, load_config
from ..core import mail, notify
from ..db import open_db


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("jobpilot.worker")
    config = load_config()
    paths = DataPaths.from_config(config)
    conn = open_db(paths, config)
    provider = providers.from_config(config)

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

    scheduler = BlockingScheduler()
    scheduler.add_job(mail_sync, "interval", seconds=config.mail.poll_seconds)
    scheduler.add_job(blocker_reminders, "interval", minutes=30)
    log.info(
        "worker started: mail every %ss, blocker reminders every 30m",
        config.mail.poll_seconds,
    )
    mail_sync()
    scheduler.start()


if __name__ == "__main__":
    main()
