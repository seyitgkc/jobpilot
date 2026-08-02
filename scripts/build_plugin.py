"""Assemble the Cowork plugin: the jobpilot skill plus the MCP server
config, packaged as dist/jobpilot.plugin.

The skill is copied from skills/jobpilot, the single source of truth. The
server command and config paths are absolute for this checkout, so the
package is per-machine; rebuild after moving the repo.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PLUGIN_README = """\
# jobpilot

Drives the local jobpilot application assistant from Cowork: capture a
posting by pasting its URL, tailor the resume against it, review the diff,
fill the form, resolve blockers, and track application mail.

Everything runs against the jobpilot checkout on this machine. Capture and
form filling additionally need its browser window running; start it with
jobpilot-browserd from the checkout when a session asks for it.
"""


def build() -> Path:
    staging = ROOT / "dist" / "plugin"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / ".claude-plugin").mkdir(parents=True)

    (staging / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "jobpilot",
                "version": "0.1.0",
                "description": "Job application assistant: capture postings, tailor the resume, fill forms, track mail",
                "author": {"name": "Seyit Gokce"},
            },
            indent=2,
        )
        + "\n"
    )

    shutil.copytree(ROOT / "skills" / "jobpilot", staging / "skills" / "jobpilot")

    (staging / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "jobpilot": {
                        "command": str(ROOT / ".venv" / "bin" / "jobpilot-mcp"),
                        "args": [],
                        "env": {"JOBPILOT_CONFIG": str(ROOT / "config.toml")},
                    }
                }
            },
            indent=2,
        )
        + "\n"
    )

    (staging / "README.md").write_text(PLUGIN_README)

    package = ROOT / "dist" / "jobpilot.plugin"
    if package.exists():
        package.unlink()
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(staging.rglob("*")):
            if path.is_file() and path.name != ".DS_Store":
                bundle.write(path, path.relative_to(staging))
    return package


if __name__ == "__main__":
    print(build())
