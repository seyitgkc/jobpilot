"""Structured inputs and outputs for provider calls, validated with pydantic.

The fact base is the owner's resume as addressable entries; the system reads
it and never writes it. A TailorPlan references facts by id, which is what
makes the honesty validator mechanical.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

SECTIONS = (
    "summary",
    "skills",
    "experience",
    "projects",
    "education",
    "certifications",
    "languages",
)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    headline: str | None = None
    location: str | None = None
    phone: str | None = None
    email: str | None = None
    linkedin: str | None = None
    github: str | None = None
    languages: str | None = None


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal[
        "summary",
        "skill_group",
        "experience",
        "bullet",
        "project",
        "education",
        "certification",
    ]
    text: str = ""
    label: str | None = None
    parent: str | None = None
    org: str | None = None
    role: str | None = None
    location: str | None = None
    dates: str | None = None
    tags: list[str] = []
    weight: float = 1.0


class FactBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Profile
    facts: list[Fact]

    @model_validator(mode="after")
    def _check_integrity(self) -> "FactBase":
        seen: dict[str, Fact] = {}
        for fact in self.facts:
            if fact.id in seen:
                raise ValueError(f"duplicate fact id {fact.id!r}")
            seen[fact.id] = fact
        bullet_parents = ("experience", "project", "education", "certification")
        for fact in self.facts:
            if fact.kind == "bullet":
                if fact.parent is None:
                    raise ValueError(f"bullet {fact.id!r} has no parent")
                parent = seen.get(fact.parent)
                if parent is None or parent.kind not in bullet_parents:
                    raise ValueError(
                        f"bullet {fact.id!r} parent {fact.parent!r} must be an "
                        f"experience, project, education, or certification"
                    )
                if not fact.text:
                    raise ValueError(f"bullet {fact.id!r} has empty text")
            if fact.kind == "skill_group" and not fact.label:
                raise ValueError(f"skill_group {fact.id!r} needs a label")
            if fact.kind == "experience" and not (fact.org and fact.role and fact.dates):
                raise ValueError(f"experience {fact.id!r} needs org, role, and dates")
        return self

    def by_id(self, fact_id: str) -> Fact | None:
        for fact in self.facts:
            if fact.id == fact_id:
                return fact
        return None

    def of_kind(self, kind: str) -> list[Fact]:
        return [fact for fact in self.facts if fact.kind == kind]

    def bullets_of(self, experience_id: str) -> list[Fact]:
        return [
            fact
            for fact in self.facts
            if fact.kind == "bullet" and fact.parent == experience_id
        ]


class TailorConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[str] = []
    token_overlap_min: float = 0.35
    notes: str | None = None
    validator_feedback: list[str] | None = None
    compile_log: str | None = None


class RenderedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    rendered_text: str


class PlannedExperience(BaseModel):
    """One selected experience or project with its selected bullets. The
    heading fields (org, role, dates, stack, name) always render from the
    fact itself, so they are never model-writable."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str
    bullets: list[RenderedFact]


class TailorPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_order: list[str] = list(SECTIONS)
    summary: RenderedFact | None = None
    skills: list[RenderedFact] = []
    experience: list[PlannedExperience] = []
    projects: list[PlannedExperience] = []


FIELD_TYPES = (
    "text",
    "textarea",
    "select",
    "multiselect",
    "checkbox",
    "radio",
    "file",
    "email",
    "phone",
    "url",
    "number",
    "date",
)


class FormField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str
    label: str | None = None
    type: Literal[
        "text",
        "textarea",
        "select",
        "multiselect",
        "checkbox",
        "radio",
        "file",
        "email",
        "phone",
        "url",
        "number",
        "date",
    ] = "text"
    options: list[str] = []
    maxlen: int | None = None
    required: bool = False
    section: str | None = None


class FormSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: list[FormField] = []
    submit_button: FormField | None = None
    pagination: bool = False

    def by_id(self, field_id: str) -> FormField | None:
        for field in self.fields:
            if field.field_id == field_id:
                return field
        return None


class AnswerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    answer: str
    answer_type: Literal["text", "select", "bool", "number", "date"] = "text"
    sensitive: bool = False
    scope: str = "global"


class AnswerBank(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[AnswerEntry] = []


class JobSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    company: str | None = None
    title: str | None = None
    board: str | None = None
    url: str | None = None
    rules: list[str] = []


class MailMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    sender: str | None = None
    sender_domain: str | None = None
    subject: str | None = None
    body_text: str = ""
    received_at: str | None = None


class MailClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: Literal[
        "confirmation", "rejection", "interview", "action_required", "other"
    ]
    confidence: float = 0.0


class MappedField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str
    value: str | bool | list[str] | None = None
    source: Literal["answer_bank", "profile", "derived", "resume_file"] | None = None
    confidence: float = 0.0
    needs_owner: bool = False
    reason: str | None = None
    proposed: str | None = None


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: list[MappedField] = []

    def by_id(self, field_id: str) -> MappedField | None:
        for field in self.fields:
            if field.field_id == field_id:
                return field
        return None
