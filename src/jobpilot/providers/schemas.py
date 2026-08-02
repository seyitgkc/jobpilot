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
        for fact in self.facts:
            if fact.kind == "bullet":
                if fact.parent is None:
                    raise ValueError(f"bullet {fact.id!r} has no parent experience")
                parent = seen.get(fact.parent)
                if parent is None or parent.kind != "experience":
                    raise ValueError(
                        f"bullet {fact.id!r} parent {fact.parent!r} is not an experience"
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
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    bullets: list[RenderedFact]


class TailorPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_order: list[str] = list(SECTIONS)
    summary: RenderedFact | None = None
    skills: list[RenderedFact] = []
    experience: list[PlannedExperience] = []
    projects: list[RenderedFact] = []
