"""Run the MCP server on stdio: jobpilot-mcp or python -m jobpilot.mcp."""

from __future__ import annotations

from .server import create_server


def main() -> None:
    mcp, _api = create_server()
    mcp.run()


if __name__ == "__main__":
    main()
