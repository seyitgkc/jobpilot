"""Golden-corpus regression: every captured page is a fixture.

Replays form extraction over each job's archived snapshot and compares the
result to the schema that was stored when the job was worked. Board markup
changes surface here as drift reports instead of botched applications.
Schemas that came from a board's JSON endpoint are skipped; replaying those
offline would compare two different sources.
"""

from __future__ import annotations

import sqlite3

from ..config import DataPaths
from ..providers.schemas import FormSchema
from ..util import from_json
from . import formscan, repo


def _latest(artifacts, kind):
    matching = [artifact for artifact in artifacts if artifact.kind == kind]
    return matching[-1] if matching else None


def _diff_schemas(stored: FormSchema, fresh: FormSchema) -> dict | None:
    stored_fields = {field.field_id: field for field in stored.fields}
    fresh_fields = {field.field_id: field for field in fresh.fields}
    missing = sorted(set(stored_fields) - set(fresh_fields))
    new = sorted(set(fresh_fields) - set(stored_fields))
    changed = []
    for field_id in sorted(set(stored_fields) & set(fresh_fields)):
        old, now = stored_fields[field_id], fresh_fields[field_id]
        deltas = []
        if old.type != now.type:
            deltas.append(f"type {old.type} to {now.type}")
        if old.required != now.required:
            deltas.append(f"required {old.required} to {now.required}")
        if old.options != now.options:
            deltas.append("options changed")
        if deltas:
            changed.append({"field_id": field_id, "changes": deltas})
    if not missing and not new and not changed:
        return None
    return {"missing_fields": missing, "new_fields": new, "changed": changed}


def corpus_check(conn: sqlite3.Connection, paths: DataPaths) -> dict:
    report = {"checked": 0, "ok": [], "drift": {}, "skipped": {}}
    for job in repo.list_jobs(conn):
        artifacts = repo.list_artifacts(conn, job.id)
        snapshot = _latest(artifacts, "snapshot_html")
        schema_artifact = _latest(artifacts, "form_schema_json")
        if snapshot is None or schema_artifact is None:
            continue
        application = repo.get_application(conn, job.id)
        method = application.method if application else None
        if method and method != "dom_generic":
            report["skipped"][job.id] = f"schema came from {method}"
            continue
        snapshot_path = paths.absolute(snapshot.path)
        schema_path = paths.absolute(schema_artifact.path)
        if not snapshot_path.exists() or not schema_path.exists():
            report["skipped"][job.id] = "artifact file missing on disk"
            continue
        report["checked"] += 1
        stored = FormSchema.model_validate(from_json(schema_path.read_text()))
        fresh = formscan.scan(snapshot_path.read_text())
        drift = _diff_schemas(stored, fresh)
        if drift is None:
            report["ok"].append(job.id)
        else:
            report["drift"][job.id] = drift
    return report
