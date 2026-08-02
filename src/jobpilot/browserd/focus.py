"""Give the owner their focus back.

macOS activates Chromium when it launches and when it opens windows, and no
Playwright option prevents it. Under the notify_only focus policy the daemon
snapshots the frontmost app before a focus-stealing operation and reactivates
it afterward if the browser took over. Both steps use stock tools that need
no permissions: lsappinfo to read the front app, open -b to reactivate.
Deliberate focus (the tab focus endpoint) bypasses this on purpose.
"""

from __future__ import annotations

import asyncio
import re
import sys

BROWSER_BUNDLES = (
    "org.chromium.Chromium",
    "com.google.Chrome",
    "com.google.Chrome.canary",
    "com.google.Chrome.beta",
)

_BUNDLE = re.compile(r'"?CFBundleIdentifier"?\s*=\s*"([^"]+)"')


def parse_front(text: str) -> str | None:
    match = _BUNDLE.search(text)
    return match.group(1) if match else None


async def front_bundle() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        process = await asyncio.create_subprocess_shell(
            "lsappinfo info -only bundleid $(lsappinfo front)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _err = await asyncio.wait_for(process.communicate(), timeout=5)
    except Exception:
        return None
    return parse_front(out.decode(errors="replace"))


async def restore(previous: str | None, delay: float = 0.4) -> bool:
    """Reactivate the previously frontmost app if the browser now holds the
    front. Returns whether a reactivation ran."""
    if not previous or previous in BROWSER_BUNDLES or sys.platform != "darwin":
        return False
    await asyncio.sleep(delay)
    current = await front_bundle()
    if current == previous or current not in BROWSER_BUNDLES:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            "open", "-b", previous,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), timeout=5)
    except Exception:
        return False
    return True
