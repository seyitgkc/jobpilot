"""Command line entry: python -m jobpilot or the jobpilot script."""

from __future__ import annotations

import argparse
import sys

from .config import DataPaths, load_config
from .core import capture as capture_pipeline
from .core.browser_client import BrowserClient, BrowserDaemonDown
from .db import open_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobpilot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser(
        "capture", help="archive a job posting and create its job row"
    )
    capture_parser.add_argument("url")
    args = parser.parse_args(argv)

    config = load_config()
    paths = DataPaths.from_config(config)
    conn = open_db(paths, config)
    client = BrowserClient(f"http://127.0.0.1:{config.browser.daemon_port}")

    if args.command == "capture":
        try:
            client.health()
        except BrowserDaemonDown:
            print(
                "The browser daemon is not running. Start it with: jobpilot-browserd",
                file=sys.stderr,
            )
            return 1
        result = capture_pipeline.capture(
            conn, paths, config, client, args.url, actor="owner"
        )
        job = result.job
        print(f"Captured job {job.id}: {job.title or '?'} at {job.company or '?'}")
        print(f"  board: {job.board}  req_id: {job.req_id or '-'}  status: {job.status}")
        for artifact in result.artifacts.values():
            print(f"  {artifact.kind}: {artifact.path}")
        for blocker in result.blockers:
            print(f"  blocker [{blocker.kind}]: {blocker.question}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
