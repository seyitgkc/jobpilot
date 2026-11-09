"""Deterministic honesty validation of tailor plans.

Every rendered text must trace to the fact base, with a grounding scope that
matches what the text claims:

- A bullet speaks about one experience or project. Its named entities may
  come from anywhere in that entry (the bullet, its sibling bullets, and the
  heading), so a rewrite can surface a technology the same job already
  lists. Numbers and dates must appear in the bullet's own master text.
- The summary, the headline, and skill-group labels speak about the whole
  career, so they may draw entities, numbers, and dates from anywhere in
  the fact base.
- Skills may move between groups and be reordered or dropped; a skill that
  exists in no group is never accepted.

Paraphrase stays bounded by token overlap against the same scope. Anything
the owner has not stated in the fact base is rejected with a pointer to
fact_add, which needs the owner's explicit decision. Hard rules from the
rules store are enforced here as well. The validator rejects plans; it
never edits them.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from ..providers.schemas import FactBase, RenderedFact, SECTIONS, TailorPlan
from . import rules as rules_module
from .models import Rule

_WORD = re.compile(r"[A-Za-z][\w+#./-]*")
_TOKEN = re.compile(r"[a-z0-9+#]+")
_NUMBER = re.compile(r"\d[\d,.]*%?\+?")
_MONTH_YEAR = re.compile(
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}"
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;:])\s+")


class Violation(BaseModel):
    code: str
    fact_id: str | None = None
    message: str


class ValidatorReport(BaseModel):
    ok: bool
    violations: list[Violation]

    def summary_lines(self) -> list[str]:
        return [
            f"{violation.code}"
            + (f" [{violation.fact_id}]" if violation.fact_id else "")
            + f": {violation.message}"
            for violation in self.violations
        ]


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _entity_candidates(text: str) -> set[str]:
    """Words that read as named entities: capitalized off sentence start,
    all-caps acronyms, and tokens carrying digits."""
    candidates: set[str] = set()
    for sentence in _SENTENCE_SPLIT.split(text):
        words = _WORD.findall(sentence)
        for index, word in enumerate(words):
            if len(word) > 1 and word.isupper():
                candidates.add(word)
            elif index > 0 and word[0].isupper():
                candidates.add(word)
            elif any(ch.isdigit() for ch in word):
                candidates.add(word)
    return candidates


_GAP_HINT = (
    "; if the owner has this, ask them whether to add it to the fact base "
    "or to this resume only (fact_add) before using it"
)


def _check_text(
    fact_id: str,
    grounding: str,
    rendered: str,
    overlap_min: float,
    out: list[Violation],
    number_source: str | None = None,
    scope: str = "the master text",
) -> None:
    """grounding is the text named entities and overlap are measured
    against; number_source (default: grounding) is where numbers and dates
    must appear verbatim."""
    grounding_lower = grounding.lower()
    grounding_tokens = set(_tokens(grounding))
    numbers_from = grounding if number_source is None else number_source

    for entity in sorted(_entity_candidates(rendered)):
        bare = entity.rstrip(".:;")
        if entity.lower() not in grounding_lower and bare.lower() not in grounding_lower:
            out.append(
                Violation(
                    code="entity_added",
                    fact_id=fact_id,
                    message=f"{bare!r} does not appear in {scope}{_GAP_HINT}",
                )
            )

    master_numbers = set(_NUMBER.findall(numbers_from))
    for number in set(_NUMBER.findall(rendered)):
        if number not in master_numbers and number.rstrip(".,") not in {
            n.rstrip(".,") for n in master_numbers
        }:
            out.append(
                Violation(
                    code="number_changed",
                    fact_id=fact_id,
                    message=f"{number!r} does not appear in the master text",
                )
            )

    for date in set(_MONTH_YEAR.findall(rendered)):
        if date not in numbers_from:
            out.append(
                Violation(
                    code="date_changed",
                    fact_id=fact_id,
                    message=f"{date!r} does not appear in the master text",
                )
            )

    rendered_tokens = _tokens(rendered)
    if rendered_tokens:
        overlap = sum(
            1 for token in rendered_tokens if token in grounding_tokens
        ) / len(rendered_tokens)
        if overlap < overlap_min:
            out.append(
                Violation(
                    code="low_overlap",
                    fact_id=fact_id,
                    message=(
                        f"token overlap {overlap:.2f} is below the "
                        f"{overlap_min:.2f} paraphrase bound"
                    ),
                )
            )


def _fact_strings(fact) -> list[str]:
    return [
        value
        for value in (fact.text, fact.label, fact.org, fact.role, fact.location)
        if value
    ]


def corpus_text(facts: FactBase) -> str:
    """Everything the owner has stated, as one grounding text."""
    parts: list[str] = []
    profile = facts.profile
    for value in (profile.headline, profile.location, profile.languages):
        if value:
            parts.append(value)
    for fact in facts.facts:
        parts.extend(_fact_strings(fact))
    return "\n".join(parts)


def entry_text(facts: FactBase, parent_id: str) -> str:
    """One experience or project: its heading plus all of its bullets."""
    parts: list[str] = []
    parent = facts.by_id(parent_id)
    if parent is not None:
        parts.extend(_fact_strings(parent))
    for bullet in facts.bullets_of(parent_id):
        parts.append(bullet.text)
    return "\n".join(parts)


def _split_items(text: str) -> list[str]:
    return [item.strip().lower() for item in text.split(",") if item.strip()]


def validate_plan(
    plan: TailorPlan,
    facts: FactBase,
    active_rules: list[Rule],
    overlap_min: float,
) -> ValidatorReport:
    violations: list[Violation] = []

    unknown_sections = [s for s in plan.section_order if s not in SECTIONS]
    for section in unknown_sections:
        violations.append(
            Violation(code="bad_section", message=f"unknown section {section!r}")
        )
    if len(set(plan.section_order)) != len(plan.section_order):
        violations.append(
            Violation(code="bad_section", message="section_order repeats a section")
        )

    def resolve(item_id: str, expected_kind: str):
        fact = facts.by_id(item_id)
        if fact is None:
            violations.append(
                Violation(
                    code="unknown_fact",
                    fact_id=item_id,
                    message="fact id does not exist in the fact base",
                )
            )
            return None
        if fact.kind != expected_kind:
            violations.append(
                Violation(
                    code="kind_mismatch",
                    fact_id=item_id,
                    message=f"expected a {expected_kind} fact, got {fact.kind}",
                )
            )
            return None
        return fact

    corpus = corpus_text(facts)

    if plan.headline is not None:
        _check_text(
            "profile.headline", corpus, plan.headline, 0.0, violations,
            scope="the fact base",
        )

    if plan.summary is not None:
        fact = resolve(plan.summary.fact_id, "summary")
        if fact is not None:
            _check_text(
                fact.id, corpus, plan.summary.rendered_text, overlap_min,
                violations, scope="the fact base",
            )

    all_skills = {
        skill
        for group in facts.of_kind("skill_group")
        for skill in _split_items(group.text)
    }
    for item in plan.skills:
        fact = resolve(item.fact_id, "skill_group")
        if fact is None:
            continue
        for skill in _split_items(item.rendered_text):
            if skill not in all_skills:
                violations.append(
                    Violation(
                        code="skill_added",
                        fact_id=fact.id,
                        message=(
                            f"{skill!r} is not in any skill group of the fact "
                            f"base{_GAP_HINT}"
                        ),
                    )
                )
        if item.label is not None:
            _check_text(
                fact.id, corpus, item.label, 0.0, violations,
                scope="the fact base",
            )

    seen_bullets: set[str] = set()

    def check_group(planned_entries, parent_kind: str) -> None:
        for planned in planned_entries:
            parent = resolve(planned.fact_id, parent_kind)
            for bullet in planned.bullets:
                fact = resolve(bullet.fact_id, "bullet")
                if fact is None:
                    continue
                if bullet.fact_id in seen_bullets:
                    violations.append(
                        Violation(
                            code="duplicate_fact",
                            fact_id=bullet.fact_id,
                            message="bullet appears more than once in the plan",
                        )
                    )
                    continue
                seen_bullets.add(bullet.fact_id)
                if parent is not None and fact.parent != parent.id:
                    violations.append(
                        Violation(
                            code="wrong_parent",
                            fact_id=bullet.fact_id,
                            message=(
                                f"bullet belongs to {fact.parent!r}, placed "
                                f"under {parent.id!r}"
                            ),
                        )
                    )
                    continue
                _check_text(
                    fact.id,
                    entry_text(facts, fact.parent),
                    bullet.rendered_text,
                    overlap_min,
                    violations,
                    number_source=fact.text,
                    scope=f"this {parent_kind} in the fact base",
                )

    check_group(plan.experience, "experience")
    check_group(plan.projects, "project")

    for rendered in _all_rendered(plan):
        for hit in rules_module.hard_violations(rendered.rendered_text, active_rules):
            violations.append(
                Violation(
                    code="hard_rule",
                    fact_id=rendered.fact_id,
                    message=f"matches banned pattern ({hit.rule_text}): {hit.match!r}",
                )
            )

    return ValidatorReport(ok=not violations, violations=violations)


def _all_rendered(plan: TailorPlan) -> list[RenderedFact]:
    rendered = list(plan.skills)
    rendered.extend(
        RenderedFact(fact_id=item.fact_id, rendered_text=item.label)
        for item in plan.skills
        if item.label
    )
    if plan.headline:
        rendered.append(
            RenderedFact(fact_id="profile.headline", rendered_text=plan.headline)
        )
    if plan.summary is not None:
        rendered.append(plan.summary)
    for planned in list(plan.experience) + list(plan.projects):
        rendered.extend(planned.bullets)
    return rendered
