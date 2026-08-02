"""Provider interface and the degraded-mode implementation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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


class ProviderUnavailable(Exception):
    """No provider can serve this call; the caller parks the work behind a
    blocker and the system keeps functioning manually."""


class ProviderCallFailed(Exception):
    """The provider was called and did not produce usable output."""


@runtime_checkable
class AIProvider(Protocol):
    def tailor(
        self, facts: FactBase, job_md: str, constraints: TailorConstraints
    ) -> TailorPlan: ...

    def map_fields(
        self, schema: FormSchema, answers: AnswerBank, job: JobSummary
    ) -> FieldMapping: ...

    def classify_mail(
        self, msg: MailMessage, open_jobs: list[JobSummary]
    ) -> MailClassification: ...


class NullProvider:
    def tailor(
        self, facts: FactBase, job_md: str, constraints: TailorConstraints
    ) -> TailorPlan:
        raise ProviderUnavailable("no AI provider is configured")

    def map_fields(
        self, schema: FormSchema, answers: AnswerBank, job: JobSummary
    ) -> FieldMapping:
        raise ProviderUnavailable("no AI provider is configured")

    def classify_mail(
        self, msg: MailMessage, open_jobs: list[JobSummary]
    ) -> MailClassification:
        raise ProviderUnavailable("no AI provider is configured")
