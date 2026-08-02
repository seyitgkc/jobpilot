"""Fill pipeline: read the form, map answers, apply constrained verbs.

Order of operations per the agreed flow: extract the schema before any
typing, resolve selectors and park behind extraction_failed when required
controls stay unresolved, map fields (provider path or a session-supplied
mapping), raise one batched missing_answer blocker for everything the owner
must decide, then apply the resolvable values with a field_fill checkpoint
per action. Submission is its own step behind the submit gate and never
happens implicitly.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from ..config import Config, DataPaths
from ..providers.base import ProviderCallFailed, ProviderUnavailable
from ..providers.schemas import (
    AnswerBank,
    AnswerEntry,
    FieldMapping,
    FormField,
    FormSchema,
    JobSummary,
    MappedField,
)
from ..util import from_json, to_json, utcnow
from . import adapters, answers, artifacts, blockers, events, repo, resume, rules, state
from .models import Artifact, Blocker, Job

_IDENTITY_FIELDS = (
    ("first name", lambda profile: profile.name.split()[0] if profile.name else None),
    ("last name", lambda profile: profile.name.split()[-1] if profile.name else None),
    ("full name", lambda profile: profile.name),
    ("email", lambda profile: profile.email),
    ("phone", lambda profile: profile.phone),
    ("location", lambda profile: profile.location),
    ("city", lambda profile: profile.location),
    ("linkedin", lambda profile: f"https://{profile.linkedin}" if profile.linkedin else None),
    ("github", lambda profile: f"https://{profile.github}" if profile.github else None),
    ("website", lambda profile: f"https://{profile.github}" if profile.github else None),
)

_CONFIRMATION_REF = re.compile(
    r"(?:confirmation|reference|application)\s*(?:number|id|code)\s*[:#]?\s*([A-Za-z0-9-]{4,})",
    re.IGNORECASE,
)


@dataclass
class FillResult:
    job: Job
    schema: FormSchema | None = None
    mapping: FieldMapping | None = None
    applied: list[dict] = field(default_factory=list)
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    blockers: list[Blocker] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.blockers


def _job_summary(conn: sqlite3.Connection, job: Job) -> JobSummary:
    active = rules.active_rules(conn, "answers")
    return JobSummary(
        id=job.id,
        company=job.company,
        title=job.title,
        board=job.board,
        url=job.canonical_url or job.url,
        rules=[rule.text for rule in active],
    )


def _profile_entries(paths: DataPaths) -> list[AnswerEntry]:
    profile = resume.load_facts(paths).profile
    entries = []
    for question, getter in _IDENTITY_FIELDS:
        value = getter(profile)
        if value:
            entries.append(AnswerEntry(question=question, answer=value))
    return entries


def assemble_answers(conn: sqlite3.Connection, paths: DataPaths, job: Job) -> AnswerBank:
    bank = answers.bank(conn, company=job.company)
    return AnswerBank(entries=_profile_entries(paths) + bank.entries)


def extract_form(
    conn: sqlite3.Connection,
    paths: DataPaths,
    config: Config,
    client,
    job_id: int,
    actor: str = "claude",
    fetch_json=None,
) -> FillResult:
    """Open the application page, build the schema, resolve selectors, and
    persist both. Parks behind extraction_failed if required controls cannot
    be resolved; no typing has happened at that point."""
    job = repo.get_job(conn, job_id)
    result = FillResult(job=job)

    client.open_tab(job.id, job.canonical_url or job.url)
    page = client.archive(job.id)
    raw_html = page.get("raw_html") or page["html"]

    kwargs = {"fetch_json": fetch_json} if fetch_json is not None else {}
    schema, method = adapters.extract_schema(job, raw_html, **kwargs)
    result.schema = schema
    if not schema.fields:
        result.blockers.append(
            blockers.raise_blocker(
                conn, job.id, "extraction_failed", actor,
                question=(
                    "No form fields were found on the application page. Open "
                    "the tab, check where the form lives, and adjust before "
                    "retrying."
                ),
            )
        )
        return result

    resolution = client.resolve(job.id, schema.model_dump())
    selector_map = resolution["resolved"]
    unresolved = set(resolution["unresolved"])
    required_unresolved = sorted(
        field.field_id
        for field in schema.fields
        if field.required and field.field_id in unresolved
    )

    result.artifacts["form_schema_json"] = artifacts.store_text(
        conn, paths, job.id, "form_schema_json", "form_schema.json",
        schema.model_dump_json(indent=2), actor,
    )
    repo.update_application_artifacts(
        conn, job.id, actor,
        form_schema_artifact_id=result.artifacts["form_schema_json"].id,
    )
    repo.update_application_fill(
        conn, job.id, actor, selector_map=to_json(selector_map), method=method
    )

    if required_unresolved:
        result.blockers.append(
            blockers.raise_blocker(
                conn, job.id, "extraction_failed", actor,
                question=(
                    "These required fields could not be located on the page: "
                    f"{', '.join(required_unresolved)}. Nothing was typed. "
                    "Check the form in the tab and retry extraction."
                ),
                field_spec={"unresolved": required_unresolved},
            )
        )
    return result


def fill_context(
    conn: sqlite3.Connection, paths: DataPaths, config: Config, job_id: int
) -> dict:
    """The exact map_fields inputs, for a session that authors the mapping."""
    job = repo.get_job(conn, job_id)
    application = repo.get_application(conn, job_id)
    if application is None or application.form_schema_artifact_id is None:
        raise ValueError(f"job {job_id} has no extracted form schema yet")
    schema_artifact = repo.get_artifact(conn, application.form_schema_artifact_id)
    schema = FormSchema.model_validate(
        from_json(paths.absolute(schema_artifact.path).read_text())
    )
    return {
        "schema": schema,
        "answers": assemble_answers(conn, paths, job),
        "job": _job_summary(conn, job),
    }


def map_with_provider(
    conn: sqlite3.Connection,
    paths: DataPaths,
    config: Config,
    provider,
    job_id: int,
) -> FieldMapping:
    context = fill_context(conn, paths, config, job_id)
    return provider.map_fields(context["schema"], context["answers"], context["job"])


def _guard_hard_rules(
    conn: sqlite3.Connection, mapping: FieldMapping
) -> FieldMapping:
    """Hard rules gate mapped values before any fill; violating fields are
    turned over to the owner instead of being typed."""
    active = rules.active_rules(conn, "answers")
    guarded = []
    for item in mapping.fields:
        if item.value is not None and isinstance(item.value, str):
            hits = rules.hard_violations(item.value, active)
            if hits:
                guarded.append(
                    item.model_copy(
                        update={
                            "value": None,
                            "needs_owner": True,
                            "proposed": item.value,
                            "reason": f"blocked by rule: {hits[0].rule_text}",
                        }
                    )
                )
                continue
        guarded.append(item)
    return FieldMapping(fields=guarded)


def _action_for(field: FormField, item: MappedField, resume_pdf: str | None) -> dict | None:
    if item.source == "resume_file" or field.type == "file":
        if not resume_pdf:
            return None
        return {"verb": "upload", "field_id": field.field_id, "path": resume_pdf}
    if field.type in ("select", "multiselect", "radio"):
        return {"verb": "select", "field_id": field.field_id, "value": item.value}
    if field.type == "checkbox":
        return {"verb": "check", "field_id": field.field_id, "value": bool(item.value)}
    return {"verb": "set", "field_id": field.field_id, "value": str(item.value)}


def _redacted_value(conn: sqlite3.Connection, item: MappedField) -> str | None:
    if item.value is None:
        return None
    text = ", ".join(item.value) if isinstance(item.value, list) else str(item.value)
    if answers.is_sensitive_answer(conn, text):
        return "[redacted]"
    return text


def apply_mapping(
    conn: sqlite3.Connection,
    paths: DataPaths,
    config: Config,
    client,
    job_id: int,
    mapping: FieldMapping,
    actor: str = "claude",
) -> FillResult:
    """Apply a mapping with the constrained verbs, one checkpoint per field.
    Raises the resume gate blocker instead of filling when approval is
    missing; needs_owner fields become one batched blocker while everything
    resolvable is filled."""
    job = repo.get_job(conn, job_id)
    result = FillResult(job=job, mapping=mapping)

    context = fill_context(conn, paths, config, job_id)
    schema: FormSchema = context["schema"]
    application = repo.get_application(conn, job_id)
    selector_map = from_json(application.selector_map) if application.selector_map else {}
    if not selector_map:
        result.error = "no selector map is stored; run extraction first"
        return result

    if job.status == "tailored":
        try:
            job = state.transition(conn, job.id, "filling", actor)
            result.job = job
        except state.GateNotSatisfied:
            result.blockers.append(
                blockers.raise_blocker(
                    conn, job.id, "resume_approval", actor,
                    question=(
                        "The resume review gate is on and this resume has not "
                        "been approved. Review and approve it, then fill."
                    ),
                )
            )
            return result
    elif job.status != "filling":
        result.error = f"job is {job.status}; filling starts from tailored"
        return result

    mapping = _guard_hard_rules(conn, mapping)
    result.mapping = mapping

    needs_owner = [item for item in mapping.fields if item.needs_owner]
    if needs_owner:
        specs = []
        for item in needs_owner:
            form_field = schema.by_id(item.field_id)
            specs.append(
                {
                    "field_id": item.field_id,
                    "label": form_field.label if form_field else None,
                    "type": form_field.type if form_field else None,
                    "options": form_field.options if form_field else [],
                    "required": form_field.required if form_field else False,
                    "reason": item.reason,
                    "proposed": item.proposed,
                }
            )
        questions = "; ".join(
            f"{spec['label'] or spec['field_id']}"
            + (f" ({spec['reason']})" if spec["reason"] else "")
            for spec in specs
        )
        result.blockers.append(
            blockers.raise_blocker(
                conn, job.id, "missing_answer", actor,
                question=(
                    f"{len(specs)} field(s) need your answer before this "
                    f"application is complete: {questions}"
                ),
                field_spec={"fields": specs},
            )
        )

    resume_pdf = None
    if application.resume_artifact_id is not None:
        resume_pdf = str(
            paths.absolute(repo.get_artifact(conn, application.resume_artifact_id).path)
        )

    actions = []
    checkpoint_ids = {}
    for item in mapping.fields:
        if item.needs_owner or item.value is None and item.source != "resume_file":
            continue
        form_field = schema.by_id(item.field_id)
        if form_field is None or item.field_id not in selector_map:
            continue
        action = _action_for(form_field, item, resume_pdf)
        if action is None:
            continue
        checkpoint_ids[item.field_id] = repo.add_field_fill(
            conn, application.id, item.field_id,
            field_label=form_field.label,
            field_type=form_field.type,
            value_redacted=(
                None if action["verb"] == "upload" else _redacted_value(conn, item)
            ),
            source=item.source or "derived",
        )
        actions.append(action)

    if actions:
        results = client.apply(job.id, actions, selector_map)
        result.applied = results
        for outcome in results:
            checkpoint = checkpoint_ids.get(outcome["field_id"])
            if checkpoint is None:
                continue
            if not outcome["ok"]:
                repo.set_field_fill_status(conn, checkpoint, "failed")
            elif outcome.get("verified"):
                repo.set_field_fill_status(conn, checkpoint, "verified")
            else:
                repo.set_field_fill_status(conn, checkpoint, "applied")
        failed = [outcome for outcome in results if not outcome["ok"]]
        if failed:
            result.blockers.append(
                blockers.raise_blocker(
                    conn, job.id, "other", actor,
                    question=(
                        "Some fields could not be filled: "
                        + ", ".join(
                            f"{outcome['field_id']} ({outcome.get('error', 'unknown')})"
                            for outcome in failed
                        )
                    ),
                )
            )

    screenshot = client.screenshot(job.id)
    result.artifacts["filled_screenshot"] = artifacts.store_bytes(
        conn, paths, job.id, "filled_screenshot", "screenshots_filled.png",
        screenshot, actor,
    )

    if not result.blockers:
        repo.update_application_fill(conn, job.id, actor, filled_at=utcnow())
        result.job = state.transition(conn, job.id, "filled", actor)
    return result


def fill_job(
    conn: sqlite3.Connection,
    paths: DataPaths,
    config: Config,
    client,
    provider,
    job_id: int,
    actor: str = "claude",
    fetch_json=None,
) -> FillResult:
    """The provider-driven path: extract, map, apply in one pass."""
    extracted = extract_form(
        conn, paths, config, client, job_id, actor, fetch_json=fetch_json
    )
    if extracted.blockers:
        return extracted
    try:
        mapping = map_with_provider(conn, paths, config, provider, job_id)
    except (ProviderUnavailable, ProviderCallFailed) as error:
        extracted.blockers.append(
            blockers.raise_blocker(
                conn, job_id, "other", actor,
                question=(
                    f"Field mapping needs an AI provider and the call did not "
                    f"succeed ({error}). Configure one or map by hand, then "
                    f"retry."
                ),
            )
        )
        return extracted
    result = apply_mapping(conn, paths, config, client, job_id, mapping, actor)
    result.schema = extracted.schema
    result.artifacts = {**extracted.artifacts, **result.artifacts}
    return result


def submit_job(
    conn: sqlite3.Connection,
    paths: DataPaths,
    config: Config,
    client,
    job_id: int,
    actor: str = "claude",
) -> FillResult:
    """Click the mapped submit control behind the submit gate, capture the
    confirmation, close the tab."""
    job = repo.get_job(conn, job_id)
    result = FillResult(job=job)
    if job.status != "filled":
        result.error = f"job is {job.status}; submit starts from filled"
        return result

    try:
        job = state.transition(conn, job.id, "submitting", actor)
        result.job = job
    except state.GateNotSatisfied:
        result.blockers.append(
            blockers.raise_blocker(
                conn, job.id, "submit_approval", actor,
                question=(
                    "The submit gate is on and this application has not been "
                    "approved for submission. Approve it, then submit."
                ),
            )
        )
        return result

    application = repo.get_application(conn, job_id)
    selector_map = from_json(application.selector_map) if application.selector_map else {}
    if "submit" not in selector_map:
        state.transition(conn, job.id, "filled", actor)
        result.job = repo.get_job(conn, job_id)
        result.blockers.append(
            blockers.raise_blocker(
                conn, job.id, "other", actor,
                question=(
                    "No submit control is in the selector map, so nothing was "
                    "clicked. Re-run extraction or submit by hand in the tab."
                ),
            )
        )
        return result

    outcomes = client.apply(
        job.id, [{"verb": "click_mapped", "field_id": "submit"}], selector_map
    )
    if not outcomes or not outcomes[0]["ok"]:
        error = outcomes[0].get("error", "unknown") if outcomes else "no result"
        state.transition(conn, job.id, "filled", actor)
        result.job = repo.get_job(conn, job_id)
        result.blockers.append(
            blockers.raise_blocker(
                conn, job.id, "other", actor,
                question=f"The submit click failed ({error}). The form is "
                f"still filled; check the tab and retry.",
            )
        )
        return result

    screenshot = client.screenshot(job.id)
    result.artifacts["confirmation_screenshot"] = artifacts.store_bytes(
        conn, paths, job.id, "confirmation_screenshot",
        "screenshots_confirmation.png", screenshot, actor,
    )
    page = client.page_state(job.id)
    confirmation_ref = None
    match = _CONFIRMATION_REF.search(page.get("title") or "")
    if match:
        confirmation_ref = match.group(1)

    repo.mark_submitted(
        conn, job.id, actor, submitted_by="system", confirmation_ref=confirmation_ref
    )
    result.job = state.transition(conn, job.id, "submitted", actor)
    try:
        client.close_tab(job.id)
    except Exception:
        pass
    events.append(
        conn, actor=actor, kind="tab_lifecycle", job_id=job.id,
        payload={"closed_on": "submitted"},
    )
    conn.commit()
    return result
