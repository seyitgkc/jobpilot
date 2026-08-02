"""The blocker resolution loop.

What the owner approves is an action, never prose: answer_blocker() records
the agreed actions on the blocker row (banking reusable answers as it goes),
and execute_blocker() re-validates the live page against the stored selector
slice before applying a single verb. Drift parks as its own blocker with the
before and after; nothing is ever blind-filled. replay_tab() rebuilds a lost
tab from checkpoints without re-asking anything the owner already answered.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..config import Config, DataPaths
from ..util import from_json, to_json
from . import answers, artifacts, blockers, events, fill, repo, state
from .models import Blocker, Job

ANSWER_VERBS = frozenset({"set", "select", "check"})


@dataclass
class ExecuteResult:
    blocker: Blocker
    applied: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    drift: Blocker | None = None
    finalized_job: Job | None = None

    @property
    def ok(self) -> bool:
        return self.drift is None and not self.failed


def _spec_fields(blocker: Blocker) -> dict[str, dict]:
    spec = from_json(blocker.field_spec) if blocker.field_spec else {}
    return {item["field_id"]: item for item in (spec or {}).get("fields", [])}


def answer_blocker(
    conn: sqlite3.Connection,
    blocker_id: int,
    actions: list[dict],
    actor: str,
    reusable_fields: tuple[str, ...] = (),
) -> Blocker:
    """Record the agreed actions. Reusable answers are banked keyed by the
    field label; job-specific answers stay on the blocker row only."""
    blocker = blockers.get_blocker(conn, blocker_id)
    if blocker.status not in ("open", "answered"):
        raise ValueError(f"blocker {blocker_id} is {blocker.status}, not answerable")
    known = _spec_fields(blocker)
    for action in actions:
        if action.get("verb") not in ANSWER_VERBS:
            raise ValueError(f"answers use {sorted(ANSWER_VERBS)}, got {action.get('verb')!r}")
        if known and action.get("field_id") not in known:
            raise ValueError(f"field {action.get('field_id')!r} is not part of this blocker")

    for field_id in reusable_fields:
        action = next((a for a in actions if a["field_id"] == field_id), None)
        spec = known.get(field_id, {})
        label = spec.get("label")
        if action is None or not label:
            continue
        value = action.get("value")
        if isinstance(value, bool):
            value = "yes" if value else "no"
        elif isinstance(value, list):
            value = ", ".join(value)
        answers.set_answer(
            conn, label, str(value), actor,
            sensitive=bool(spec.get("sensitive")),
            created_from_blocker_id=blocker_id,
        )

    return blockers.set_status(
        conn, blocker_id, "answered", actor,
        agreed_action={"actions": actions},
        answer_reusable=bool(reusable_fields),
    )


def _selector_slice(conn: sqlite3.Connection, blocker: Blocker) -> dict:
    context = from_json(blocker.page_context) if blocker.page_context else {}
    selectors = (context or {}).get("selectors") or {}
    if selectors:
        return selectors
    application = repo.get_application(conn, blocker.job_id)
    if application and application.selector_map:
        return from_json(application.selector_map) or {}
    return {}


def execute_blocker(
    conn: sqlite3.Connection,
    paths: DataPaths,
    config: Config,
    client,
    blocker_id: int,
    actor: str = "claude",
) -> ExecuteResult:
    """Validate then apply the agreed actions. The page is re-checked field
    by field first; any mismatch becomes a drift blocker and nothing is
    typed."""
    blocker = blockers.get_blocker(conn, blocker_id)
    if blocker.status != "answered":
        raise ValueError(f"blocker {blocker_id} is {blocker.status}; answer it first")
    agreed = from_json(blocker.agreed_action) or {}
    actions = agreed.get("actions") or []
    if not actions:
        raise ValueError(f"blocker {blocker_id} has no agreed actions")

    selector_map = _selector_slice(conn, blocker)
    known = _spec_fields(blocker)
    result = ExecuteResult(blocker=blocker)

    checks = client.validate(
        blocker.job_id,
        [
            {
                "field_id": action["field_id"],
                "options": known.get(action["field_id"], {}).get("options") or [],
            }
            for action in actions
        ],
        selector_map,
    )
    drifted = {
        field_id: check
        for field_id, check in checks.items()
        if not (check.get("present") and check.get("matches"))
    }
    if drifted:
        details = "; ".join(
            f"{field_id}: {check.get('reason') or 'missing from the page'}"
            for field_id, check in drifted.items()
        )
        result.drift = blockers.raise_blocker(
            conn, blocker.job_id, "drift", actor,
            question=(
                "The page changed since these fields were mapped, so nothing "
                f"was typed. Changed: {details}. Check the tab, then either "
                "retry execution or re-run extraction."
            ),
            field_spec={
                "fields": [
                    {
                        "field_id": field_id,
                        "expected": known.get(field_id),
                        "reason": check.get("reason"),
                    }
                    for field_id, check in drifted.items()
                ]
            },
        )
        events.append(
            conn, actor=actor, kind="drift_detected", job_id=blocker.job_id,
            payload={"blocker_id": blocker.id, "fields": sorted(drifted)},
        )
        conn.commit()
        return result

    blockers.set_status(conn, blocker_id, "executing", actor)
    application = repo.get_or_create_application(conn, blocker.job_id)
    checkpoints = {}
    for action in actions:
        spec = known.get(action["field_id"], {})
        value = action.get("value")
        if isinstance(value, bool):
            stored = "yes" if value else "no"
        elif isinstance(value, list):
            stored = ", ".join(value)
        else:
            stored = None if value is None else str(value)
        if stored is not None and answers.is_sensitive_answer(conn, stored):
            stored = "[redacted]"
        checkpoints[action["field_id"]] = repo.add_field_fill(
            conn, application.id, action["field_id"],
            field_label=spec.get("label"),
            field_type=spec.get("type"),
            value_redacted=stored,
            source=f"blocker:{blocker_id}",
        )

    outcomes = client.apply(blocker.job_id, actions, selector_map)
    for outcome in outcomes:
        checkpoint = checkpoints.get(outcome["field_id"])
        if checkpoint is None:
            continue
        if not outcome["ok"]:
            repo.set_field_fill_status(conn, checkpoint, "failed")
        elif outcome.get("verified"):
            repo.set_field_fill_status(conn, checkpoint, "verified")
        else:
            repo.set_field_fill_status(conn, checkpoint, "applied")

    result.applied = [outcome for outcome in outcomes if outcome["ok"]]
    result.failed = [outcome for outcome in outcomes if not outcome["ok"]]
    if result.failed:
        result.blocker = blockers.set_status(conn, blocker_id, "answered", actor)
        events.append(
            conn, actor=actor, kind="blocker_execution_failed",
            job_id=blocker.job_id,
            payload={
                "blocker_id": blocker_id,
                "fields": [outcome["field_id"] for outcome in result.failed],
            },
        )
        conn.commit()
        return result

    result.blocker = blockers.set_status(conn, blocker_id, "resolved", actor)
    result.finalized_job = fill.finalize_if_complete(conn, blocker.job_id, actor)
    if result.finalized_job is not None:
        try:
            image = client.screenshot(blocker.job_id)
            artifacts.store_bytes(
                conn, paths, blocker.job_id, "filled_screenshot",
                "screenshots_filled.png", image, actor,
            )
        except Exception:
            pass
    return result


def mark_manual_resolved(
    conn: sqlite3.Connection, blocker_id: int, actor: str, note: str | None = None
) -> Blocker:
    """The owner handled it in the tab (login, captcha, manual form work).
    The standard follow-up for pending fields is validate before applying
    anything deferred, which execute_blocker() already does."""
    blocker = blockers.get_blocker(conn, blocker_id)
    if blocker.status not in ("open", "answered"):
        raise ValueError(f"blocker {blocker_id} is {blocker.status}")
    resolved = blockers.set_status(conn, blocker_id, "resolved", actor)
    if note:
        events.append(
            conn, actor=actor, kind="blocker_manual_note",
            job_id=blocker.job_id,
            payload={"blocker_id": blocker_id, "note": note},
        )
        conn.commit()
    return resolved


@dataclass
class ReplayResult:
    job: Job
    replayed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.missing and not self.blockers


def _latest_fills(conn: sqlite3.Connection, application_id: int) -> dict[str, object]:
    latest: dict[str, object] = {}
    for row in repo.list_field_fills(conn, application_id):
        if row.status in ("applied", "verified"):
            latest[row.field_id] = row
    return latest


def replay_tab(
    conn: sqlite3.Connection,
    paths: DataPaths,
    config: Config,
    client,
    job_id: int,
    actor: str = "claude",
) -> ReplayResult:
    """Reopen a lost tab and replay checkpointed fills. Values come from the
    checkpoints, sensitive ones from the answer bank; anything that cannot be
    reconstructed is re-raised as one blocker instead of re-asking piecemeal."""
    job = repo.get_job(conn, job_id)
    result = ReplayResult(job=job)
    application = repo.get_application(conn, job_id)
    if application is None or application.form_schema_artifact_id is None:
        raise ValueError(f"job {job_id} has no extracted form to replay")

    schema_artifact = repo.get_artifact(conn, application.form_schema_artifact_id)
    schema = from_json(paths.absolute(schema_artifact.path).read_text())

    client.open_tab(job_id, job.canonical_url or job.url)
    resolution = client.resolve(job_id, schema)
    selector_map = resolution["resolved"]
    repo.update_application_fill(conn, job_id, actor, selector_map=to_json(selector_map))

    fields_by_id = {item["field_id"]: item for item in schema.get("fields", [])}
    resume_pdf = None
    if application.resume_artifact_id is not None:
        resume_pdf = str(
            paths.absolute(repo.get_artifact(conn, application.resume_artifact_id).path)
        )

    actions = []
    for field_id, row in _latest_fills(conn, application.id).items():
        if field_id not in selector_map:
            result.missing.append(field_id)
            continue
        form_field = fields_by_id.get(field_id, {})
        field_type = row.field_type or form_field.get("type") or "text"
        if row.source == "resume_file" or field_type == "file":
            if resume_pdf:
                actions.append({"verb": "upload", "field_id": field_id, "path": resume_pdf})
            else:
                result.missing.append(field_id)
            continue
        value = row.value_redacted
        if value == "[redacted]" or value is None:
            banked = answers.get_answer(conn, row.field_label or field_id)
            if banked is None:
                result.missing.append(field_id)
                continue
            value = banked.answer
        if field_type == "checkbox":
            actions.append(
                {"verb": "check", "field_id": field_id, "value": value.lower() != "no"}
            )
        elif field_type in ("select", "multiselect", "radio"):
            actions.append({"verb": "select", "field_id": field_id, "value": value})
        else:
            actions.append({"verb": "set", "field_id": field_id, "value": value})

    if actions:
        outcomes = client.apply(job_id, actions, selector_map)
        for outcome in outcomes:
            if outcome["ok"]:
                result.replayed.append(outcome["field_id"])
            else:
                result.failed.append(outcome["field_id"])

    if result.missing:
        result.blockers.append(
            blockers.raise_blocker(
                conn, job_id, "missing_answer", actor,
                question=(
                    "The tab was reopened but these fields could not be "
                    f"replayed from checkpoints: {', '.join(sorted(result.missing))}. "
                    "Answer them to finish the form."
                ),
                field_spec={
                    "fields": [
                        {
                            "field_id": field_id,
                            "label": fields_by_id.get(field_id, {}).get("label"),
                            "type": fields_by_id.get(field_id, {}).get("type"),
                            "options": fields_by_id.get(field_id, {}).get("options") or [],
                            "required": fields_by_id.get(field_id, {}).get("required", False),
                            "reason": "not replayable from checkpoints",
                            "proposed": None,
                        }
                        for field_id in sorted(result.missing)
                    ]
                },
            )
        )

    events.append(
        conn, actor=actor, kind="tab_replayed", job_id=job_id,
        payload={
            "replayed": result.replayed,
            "failed": result.failed,
            "missing": sorted(result.missing),
        },
    )
    conn.commit()
    result.job = repo.get_job(conn, job_id)
    return result


def abandon_job(conn: sqlite3.Connection, job_id: int, actor: str) -> Job:
    """Owner abandons the job: open blockers go with it, one core call for
    panel and chat alike."""
    job = state.transition(conn, job_id, "abandoned", actor)
    blockers.abandon_open(conn, job_id, actor)
    return job
