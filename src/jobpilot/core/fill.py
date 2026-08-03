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

# Boards chain an apply step across a couple of pages before the form shows.
# Past a few hops the control is looping rather than leading anywhere.
_APPLY_GATE_HOPS = 3

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

    # Posting pages that keep the form behind an apply step: the apply
    # control is part of the extracted schema, so clicking it stays within
    # the mapped-verb rules. Boards chain these steps and some send the
    # applicant to another site entirely, so keep following while the page
    # offers a way forward and no fields have appeared. A page carrying no
    # fields counts even when something submit-shaped was matched, because
    # skip links and search buttons read as submit controls.
    seen_urls = {page.get("url")}
    for _ in range(_APPLY_GATE_HOPS):
        if schema.apply_button is None:
            break
        if schema.fields and schema.submit_button is not None:
            break
        resolution = client.resolve(job.id, schema.model_dump())
        apply_descriptor = resolution["resolved"].get("apply")
        if apply_descriptor is None:
            break
        outcomes = client.apply(
            job.id,
            [{"verb": "click_mapped", "field_id": "apply"}],
            {"apply": apply_descriptor},
        )
        if not outcomes or not outcomes[0]["ok"]:
            break
        events.append(
            conn, actor=actor, kind="apply_gate_followed", job_id=job.id,
            payload={"label": schema.apply_button.label},
        )
        conn.commit()
        page = client.archive(job.id)
        raw_html = page.get("raw_html") or page["html"]
        schema, method = adapters.extract_schema(job, raw_html, **kwargs)
        url = page.get("url")
        # A control that reopens the page it sits on would otherwise loop.
        if url in seen_urls and not schema.fields:
            break
        seen_urls.add(url)

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


def _pending_decisions(conn: sqlite3.Connection, job_id: int) -> dict[str, dict]:
    """Fields an open blocker is still waiting on the owner for, by field id,
    carrying the proposal and the reason it was not answered outright."""
    pending: dict[str, dict] = {}
    for blocker in blockers.list_open(conn, job_id):
        if not blocker.field_spec:
            continue
        try:
            spec = from_json(blocker.field_spec)
        except ValueError:
            continue
        for entry in spec.get("fields") or []:
            if isinstance(entry, dict) and entry.get("field_id"):
                pending[entry["field_id"]] = entry
    return pending


def assist_sheet(conn: sqlite3.Connection, paths: DataPaths, job_id: int) -> dict:
    """Everything needed to fill this application by hand, in the order the
    form asks for it. For boards that refuse automated sessions, the owner
    types and jobpilot supplies the content, so a row carries the exact value
    to enter, the control it goes into, and the options to pick between."""
    job = repo.get_job(conn, job_id)
    application = repo.get_application(conn, job_id)
    pending = _pending_decisions(conn, job_id)

    schema = FormSchema()
    if application is not None and application.form_schema_artifact_id is not None:
        artifact = repo.get_artifact(conn, application.form_schema_artifact_id)
        schema = FormSchema.model_validate(
            from_json(paths.absolute(artifact.path).read_text())
        )

    filled: dict[str, object] = {}
    if application is not None:
        for record in repo.list_field_fills(conn, application.id):
            filled[record.field_id] = record

    def row_for(field: FormField) -> dict:
        record = filled.get(field.field_id)
        decision = pending.get(field.field_id)
        value = getattr(record, "value_redacted", None) or ""
        source = getattr(record, "source", None) or ""
        if not value and decision:
            value = decision.get("proposed") or ""
            source = "proposed"
        return {
            "field_id": field.field_id,
            "label": field.label or field.field_id,
            "value": value,
            "source": source or "unmapped",
            "type": field.type,
            "options": field.options,
            "required": field.required,
            "maxlen": field.maxlen,
            "needs_owner": decision is not None,
            "reason": (decision or {}).get("reason"),
        }

    groups: list[dict] = []
    placed: dict[str, dict] = {}
    for field in schema.fields:
        if field.type == "file":
            continue
        # A checkbox group renders one control per choice under a single
        # name, so the choices are options of one question rather than
        # separate questions that happen to share an answer.
        existing = placed.get(field.field_id)
        if existing is not None:
            if not existing["options"]:
                # The first control's label was read as the question; under a
                # shared name it is really the first choice.
                existing["options"] = [existing["label"]]
                existing["label"] = field.section or "Select all that apply"
            if field.label and field.label not in existing["options"]:
                existing["options"].append(field.label)
            continue
        row = row_for(field)
        placed[field.field_id] = row
        name = field.section
        if not groups or groups[-1]["name"] != name:
            groups.append({"name": name, "rows": []})
        groups[-1]["rows"].append(row)

    # Anything banked that the form never asked for, so a question the board
    # renders late or behind a step still has its answer to hand.
    covered = {row["label"].lower() for group in groups for row in group["rows"]}
    entered = {
        row["value"].strip().lower()
        for group in groups
        for row in group["rows"]
        if row["value"]
    }
    extra: list[dict] = []
    for entry in assemble_answers(conn, paths, job).entries:
        if entry.question.lower() in covered:
            continue
        if not entry.sensitive and entry.answer.strip().lower() in entered:
            continue
        covered.add(entry.question.lower())
        extra.append(
            {
                "field_id": entry.question,
                "label": entry.question,
                "value": "[saved privately]" if entry.sensitive else entry.answer,
                "source": "answer bank",
                "type": entry.answer_type,
                "options": [],
                "required": False,
                "maxlen": None,
                "needs_owner": False,
                "reason": None,
            }
        )

    resume = None
    if application is not None and application.resume_artifact_id is not None:
        artifact = repo.get_artifact(conn, application.resume_artifact_id)
        resume = {
            "artifact_id": artifact.id,
            "path": artifact.path,
            "absolute": str(paths.absolute(artifact.path).resolve()),
        }
    ready = sum(
        1
        for group in groups
        for row in group["rows"]
        if row["value"] and not row["needs_owner"]
    )
    return {
        "job": job,
        "resume": resume,
        "groups": groups,
        "extra": extra,
        "ready": ready,
        "waiting": sum(
            1 for group in groups for row in group["rows"] if row["needs_owner"]
        ),
    }


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

    # A filled job fills again for rework: the page drifted, a value was
    # rejected, or the tab was replaced and its values went with it.
    if job.status in ("tailored", "filled"):
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
        result.error = (
            f"job is {job.status}; filling starts from tailored or filled"
        )
        return result

    mapping = _guard_hard_rules(conn, mapping)
    result.mapping = mapping

    needs_owner = [item for item in mapping.fields if item.needs_owner]
    if needs_owner:
        specs = []
        for item in needs_owner:
            form_field = schema.by_id(item.field_id)
            spec = {
                "field_id": item.field_id,
                "label": form_field.label if form_field else None,
                "type": form_field.type if form_field else None,
                "options": form_field.options if form_field else [],
                "maxlen": form_field.maxlen if form_field else None,
                "required": form_field.required if form_field else False,
                "reason": item.reason,
                "proposed": item.proposed,
            }
            descriptor = selector_map.get(item.field_id)
            if descriptor is not None:
                try:
                    image = client.crop(job.id, descriptor)
                    shot = artifacts.store_bytes(
                        conn, paths, job.id, "field_screenshot",
                        f"screenshots_field_{item.field_id}.png", image, actor,
                    )
                    spec["screenshot_artifact_id"] = shot.id
                except Exception:
                    pass
            specs.append(spec)
        questions = "; ".join(
            f"{spec['label'] or spec['field_id']}"
            + (f" ({spec['reason']})" if spec["reason"] else "")
            for spec in specs
        )
        page_context = {
            "url": job.canonical_url or job.url,
            "selectors": {
                spec["field_id"]: selector_map[spec["field_id"]]
                for spec in specs
                if spec["field_id"] in selector_map
            },
        }
        result.blockers.append(
            blockers.raise_blocker(
                conn, job.id, "missing_answer", actor,
                question=(
                    f"{len(specs)} field(s) need your answer before this "
                    f"application is complete: {questions}"
                ),
                field_spec={"fields": specs},
                page_context=page_context,
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

    finalized = finalize_if_complete(conn, job.id, actor)
    if finalized is not None:
        result.job = finalized
    return result


def finalize_if_complete(conn: sqlite3.Connection, job_id: int, actor: str) -> Job | None:
    """Advance filling to filled once nothing is parked. Called after the
    initial fill and again whenever a blocker resolution lands answers."""
    job = repo.get_job(conn, job_id)
    if job.status != "filling" or blockers.list_open(conn, job_id):
        return None
    repo.update_application_fill(conn, job_id, actor, filled_at=utcnow())
    return state.transition(conn, job_id, "filled", actor)


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

    # Human verification is asked for here and nowhere else: the owner solves
    # it and submission follows in the same visit, so the token stays fresh.
    try:
        challenge = client.challenge(job.id)
    except Exception:
        challenge = {"present": False}
    # A drawn widget has to be solved before anything is sent. A passive
    # hCaptcha draws nothing to solve, so the send is attempted and judged by
    # what comes back.
    if challenge.get("present") and challenge.get("visible", True):
        state.transition(conn, job.id, "filled", actor)
        result.job = repo.get_job(conn, job_id)
        try:
            client.focus(job.id)
        except Exception:
            pass
        result.blockers.append(
            blockers.raise_blocker(
                conn, job.id, "captcha", actor,
                question=(
                    "This application asks for human verification before it "
                    "can be sent. The tab is in front: solve it there, then "
                    "use Submit now, and the application goes out "
                    "immediately while the verification is still valid."
                ),
                page_context={"kind": challenge.get("kind"), "visible": True},
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

    # A verification widget standing on the page after the click means the
    # send was refused, so the application is still sitting there filled.
    try:
        refused = client.challenge(job.id)
    except Exception:
        refused = {"present": False}
    if refused.get("present") and refused.get("visible", True):
        result.artifacts["refused_screenshot"] = artifacts.store_bytes(
            conn, paths, job.id, "refused_screenshot",
            "screenshots_refused.png", screenshot, actor,
        )
        state.transition(conn, job.id, "filled", actor)
        result.job = repo.get_job(conn, job_id)
        try:
            client.focus(job.id)
        except Exception:
            pass
        result.blockers.append(
            blockers.raise_blocker(
                conn, job.id, "captcha", actor,
                question=(
                    "The send was refused and human verification is now on "
                    "the page, so the application did not go out. The answers "
                    "are still filled in. The tab is in front: solve the "
                    "verification there, then use Submit now. If it refuses "
                    "again, send it by hand from the answer sheet and use "
                    "Mark submitted by hand."
                ),
                page_context={
                    "kind": refused.get("kind"),
                    "after_submit": True,
                },
            )
        )
        return result

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
