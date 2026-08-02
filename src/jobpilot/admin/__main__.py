"""Run the admin panel: jobpilot-admin or python -m jobpilot.admin."""

from __future__ import annotations

import uvicorn

from ..config import DataPaths, load_config
from .app import create_app


def main() -> None:
    config = load_config()
    paths = DataPaths.from_config(config)
    app = create_app(config, paths)
    uvicorn.run(app, host="127.0.0.1", port=config.admin.port)


if __name__ == "__main__":
    main()
