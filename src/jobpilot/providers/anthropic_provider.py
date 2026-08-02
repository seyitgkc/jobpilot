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
from .schemas import FactBase, TailorConstraints, TailorPlan

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
- A bullet may only appear under the experience it belongs to.
- A skill group's rendered_text may drop or reorder the master items and may \
never add new ones.
- Project entries keep their master names unchanged.
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


class AnthropicProvider:
    def __init__(self, config: AIConfig):
        self.config = config

    def _client(self) -> anthropic.Anthropic:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ProviderUnavailable("ANTHROPIC_API_KEY is not set")
        return anthropic.Anthropic()

    def tailor(
        self, facts: FactBase, job_md: str, constraints: TailorConstraints
    ) -> TailorPlan:
        client = self._client()
        kwargs: dict = {}
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        try:
            response = client.messages.parse(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=_TAILOR_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": _user_message(facts, job_md, constraints),
                    }
                ],
                output_format=TailorPlan,
                **kwargs,
            )
        except anthropic.APIStatusError as error:
            raise ProviderCallFailed(
                f"tailor call failed: {error.status_code} {error.message}"
            ) from error
        except anthropic.APIConnectionError as error:
            raise ProviderCallFailed(f"tailor call failed: {error}") from error
        if response.stop_reason == "refusal":
            raise ProviderCallFailed("tailor call was refused by the model")
        if response.stop_reason == "max_tokens":
            raise ProviderCallFailed(
                "tailor call hit max_tokens before completing; raise ai.max_tokens"
            )
        if response.parsed_output is None:
            raise ProviderCallFailed("tailor call returned no parsable plan")
        return response.parsed_output
