"""Board-specific form schemas.

Greenhouse publishes the application questions as JSON, which beats parsing
its DOM; every other board goes through the generic DOM scan of the rendered
application page. Adapter fragility stays isolated here and the golden corpus
pins the behavior as captures accumulate.
"""

from __future__ import annotations

from typing import Callable

import httpx

from ..providers.schemas import FormField, FormSchema
from . import boards, formscan
from .models import Job

FetchJson = Callable[[str], dict | None]

_GREENHOUSE_TYPES = {
    "input_text": "text",
    "input_file": "file",
    "textarea": "textarea",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "multiselect",
}


def _default_fetch_json(url: str) -> dict | None:
    try:
        response = httpx.get(url, timeout=20)
        if response.status_code != 200:
            return None
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def greenhouse_schema(
    company_slug: str, req_id: str, fetch_json: FetchJson = _default_fetch_json
) -> FormSchema | None:
    data = fetch_json(
        f"https://boards-api.greenhouse.io/v1/boards/{company_slug}"
        f"/jobs/{req_id}?questions=true"
    )
    if not data or "questions" not in data:
        return None
    fields: list[FormField] = []
    for section, questions in (
        (None, data.get("questions") or []),
        ("Location", data.get("location_questions") or []),
        ("Demographic questions", data.get("demographic_questions") or []),
    ):
        for question in questions:
            for raw_field in question.get("fields") or []:
                field_type = _GREENHOUSE_TYPES.get(raw_field.get("type"))
                if field_type is None:
                    continue
                options = [
                    value.get("label")
                    for value in raw_field.get("values") or []
                    if value.get("label")
                ]
                fields.append(
                    FormField(
                        field_id=raw_field.get("name") or question["label"],
                        label=question.get("label"),
                        type=field_type,
                        options=options,
                        required=bool(question.get("required")),
                        section=section,
                    )
                )
    return FormSchema(
        fields=fields,
        submit_button=FormField(field_id="submit", label="Submit application"),
    )


def extract_schema(
    job: Job, raw_html: str, fetch_json: FetchJson = _default_fetch_json
) -> tuple[FormSchema, str]:
    """Schema plus the method string recorded on the application."""
    if job.board == "greenhouse":
        info = boards.detect_from_url(job.canonical_url or job.url)
        req_id = job.req_id or info.req_id
        if info.company_slug and req_id:
            schema = greenhouse_schema(info.company_slug, req_id, fetch_json)
            if schema is not None and schema.fields:
                return schema, "adapter:greenhouse"
    return formscan.scan(raw_html), "dom_generic"
