"""Anthropic implementation of the provider interface.

Uses structured outputs (messages.parse) so the response is schema-valid
TailorPlan JSON by construction; semantic validity is the honesty validator's
job in core. Model, temperature, and max_tokens come from config, never from
call sites.
"""

from __future__ import annotations

import os

import anthropic

from ..config import AIConfig
from .base import ProviderCallFailed, ProviderUnavailable
from .schemas import (
    AnswerBank,
    FactBase,
    FieldMapping,
    FormSchema,
    JobSummary,
    MailClassification,
    MailMessage,
    TailorConstraints,
    TailorPlan,
)

_TAILOR_SYSTEM = """\
You tailor a resume against a job posting. You select and rephrase content \
from a fixed fact base; you never invent content.

You produce a tailor plan that references facts by their ids. Hard \
constraints, enforced mechanically after you answer:

- Every rendered_text must be a faithful paraphrase or trim of that fact's \
master text. Never introduce employers, products, technologies, \
certifications, numbers, or dates that are absent from the master text of \
that same fact.
- Keep every number, percentage, and month-year exactly as written in the \
master text.
- A bullet may only appear under the experience or project it belongs to.
- A skill group's rendered_text may drop or reorder the master items and may \
never add new ones.
- Prefer dropping weak or irrelevant content over rewriting it heavily.
- Order experiences in the order they appear in the fact base.

Select and emphasize what the posting asks for. Keep bullets tight and \
concrete."""


def _user_message(
    facts: FactBase, job_md: str, constraints: TailorConstraints
) -> str:
    parts = [
        "Fact base (JSON):",
        facts.model_dump_json(),
        "",
        "Job posting (markdown):",
        job_md,
    ]
    if constraints.rules:
        parts += ["", "Owner rules, follow all of them:"]
        parts += [f"- {rule}" for rule in constraints.rules]
    if constraints.notes:
        parts += ["", f"Owner notes for this job: {constraints.notes}"]
    if constraints.validator_feedback:
        parts += [
            "",
            "Your previous plan was rejected by the honesty validator. "
            "Fix these violations without introducing new content:",
        ]
        parts += [f"- {item}" for item in constraints.validator_feedback]
    if constraints.compile_log:
        parts += [
            "",
            "The rendered LaTeX failed to compile. Adjust the plan text that "
            "triggers this error (special characters are escaped by the "
            "renderer; the usual cause is pathological length or structure):",
            constraints.compile_log,
        ]
    return "\n".join(parts)


_MAP_SYSTEM = """\
You map application form fields to answers from the owner's answer bank. You \
never invent an answer.

For each form field, either supply a value or mark it needs_owner:

- Use an answer bank entry when the field asks the same question, even if \
worded differently. Fit the value to the field: match a dropdown option \
exactly as listed in options; booleans for checkboxes.
- Basic identity fields (name, email, phone, links, location) come from the \
entries provided with source profile.
- The resume upload field gets source resume_file and no value.
- Cover letter or free-writing fields: only fill from a matching banked \
entry; otherwise needs_owner.
- Personal, subjective, demographic, or legal-declaration questions with no \
banked answer are always needs_owner, with a short reason. When you can \
suggest an answer for the owner to confirm, put it in proposed; never in \
value.
- Set confidence between 0 and 1 for every filled field. When unsure, prefer \
needs_owner over guessing.
- Include every field from the schema exactly once.
- Follow every owner rule provided."""


def _map_message(schema: FormSchema, answers: AnswerBank, job: JobSummary) -> str:
    parts = [
        "Job:",
        job.model_dump_json(exclude={"rules"}),
        "",
        "Form schema (JSON):",
        schema.model_dump_json(),
        "",
        "Answer bank (JSON):",
        answers.model_dump_json(),
    ]
    if job.rules:
        parts += ["", "Owner rules, follow all of them:"]
        parts += [f"- {rule}" for rule in job.rules]
    return "\n".join(parts)


_CLASSIFY_SYSTEM = """\
You classify one email about the owner's job applications.

Categories:
- confirmation: the application was received or acknowledged.
- rejection: the application will not move forward.
- interview: an interview, screen, or call is being offered or scheduled.
- action_required: the owner must do something (assessment, document, form).
- other: anything else, including newsletters and unrelated recruiting spam.

Judge only from the message content; the open applications list is context.
Set confidence between 0 and 1."""


class AnthropicProvider:
    def __init__(self, config: AIConfig):
        self.config = config

    def _client(self) -> anthropic.Anthropic:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ProviderUnavailable("ANTHROPIC_API_KEY is not set")
        return anthropic.Anthropic()

    def _parse_call(self, call_name: str, system: str, user: str, output_format):
        client = self._client()
        kwargs: dict = {}
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        try:
            response = client.messages.parse(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=output_format,
                **kwargs,
            )
        except anthropic.APIStatusError as error:
            raise ProviderCallFailed(
                f"{call_name} call failed: {error.status_code} {error.message}"
            ) from error
        except anthropic.APIConnectionError as error:
            raise ProviderCallFailed(f"{call_name} call failed: {error}") from error
        if response.stop_reason == "refusal":
            raise ProviderCallFailed(f"{call_name} call was refused by the model")
        if response.stop_reason == "max_tokens":
            raise ProviderCallFailed(
                f"{call_name} call hit max_tokens before completing; raise ai.max_tokens"
            )
        if response.parsed_output is None:
            raise ProviderCallFailed(f"{call_name} call returned no parsable output")
        return response.parsed_output

    def tailor(
        self, facts: FactBase, job_md: str, constraints: TailorConstraints
    ) -> TailorPlan:
        return self._parse_call(
            "tailor", _TAILOR_SYSTEM, _user_message(facts, job_md, constraints), TailorPlan
        )

    def map_fields(
        self, schema: FormSchema, answers: AnswerBank, job: JobSummary
    ) -> FieldMapping:
        return self._parse_call(
            "map_fields", _MAP_SYSTEM, _map_message(schema, answers, job), FieldMapping
        )

    def classify_mail(
        self, msg: MailMessage, open_jobs: list[JobSummary]
    ) -> MailClassification:
        user = "\n".join(
            [
                "Message (JSON):",
                msg.model_dump_json(),
                "",
                "Open applications (JSON):",
                "[" + ", ".join(job.model_dump_json(exclude={"rules"}) for job in open_jobs) + "]",
            ]
        )
        return self._parse_call(
            "classify_mail", _CLASSIFY_SYSTEM, user, MailClassification
        )
