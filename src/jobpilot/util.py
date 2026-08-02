"""Small shared helpers with no jobpilot-internal dependencies."""

from __future__ import annotations

import json
from datetime import datetime, timezone


def utcnow() -> str:
    """Current UTC time as ISO-8601 text, the timestamp format for all tables."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_json(payload: dict | list | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def from_json(text: str | None) -> dict | list | None:
    if text is None:
        return None
    return json.loads(text)
