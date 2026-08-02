"""Run the browser daemon: jobpilot-browserd or python -m jobpilot.browserd."""

from __future__ import annotations

import uvicorn

from ..config import DataPaths, load_config
from .app import create_app


def main() -> None:
    config = load_config()
    paths = DataPaths.from_config(config)
    app = create_app(config, paths)
    uvicorn.run(app, host="127.0.0.1", port=config.browser.daemon_port)


if __name__ == "__main__":
    main()
