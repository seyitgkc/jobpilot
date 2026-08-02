"""Run the admin panel: jobpilot-admin or python -m jobpilot.admin."""

from __future__ import annotations

import sys

import httpx
import uvicorn

from ..config import DataPaths, load_config
from .app import create_app


def port_status(port: int, marker: str) -> str:
    """One of free, ours, or taken."""
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/", timeout=2)
    except httpx.TransportError:
        return "free"
    return "ours" if marker in response.text else "taken"


def main() -> None:
    config = load_config()
    paths = DataPaths.from_config(config)
    status = port_status(config.admin.port, "jobpilot")
    if status == "ours":
        print(f"The panel is already running at http://127.0.0.1:{config.admin.port}")
        return
    if status == "taken":
        print(
            f"Port {config.admin.port} is in use by another program; set a "
            f"different admin.port in config.toml.",
            file=sys.stderr,
        )
        sys.exit(1)
    app = create_app(config, paths)
    uvicorn.run(app, host="127.0.0.1", port=config.admin.port)


if __name__ == "__main__":
    main()
