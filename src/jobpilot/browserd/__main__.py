"""Run the browser daemon: jobpilot-browserd or python -m jobpilot.browserd."""

from __future__ import annotations

import sys

import httpx
import uvicorn

from ..config import DataPaths, load_config
from .app import create_app


def main() -> None:
    config = load_config()
    paths = DataPaths.from_config(config)
    port = config.browser.daemon_port
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
    except httpx.TransportError:
        response = None
    if response is not None:
        if response.status_code == 200 and "open_tabs" in response.text:
            print(f"The browser daemon is already running on port {port}")
            return
        print(
            f"Port {port} is in use by another program; set a different "
            f"browser.daemon_port in config.toml.",
            file=sys.stderr,
        )
        sys.exit(1)
    app = create_app(config, paths)
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
