"""Deterministic honesty validation of tailor plans.

Every rendered text must trace to its master fact: no new named entities, no
changed numbers or dates, bounded paraphrase (token overlap), bullets only
under their own experience, skills selected but never invented. Hard rules
from the rules store are enforced here as well. The validator rejects plans;
it never edits them.
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


def _check_text(
    fact_id: str,
    master: str,
    rendered: str,
    overlap_min: float,
    out: list[Violation],
) -> None:
    master_lower = master.lower()
    master_tokens = set(_tokens(master))

    for entity in sorted(_entity_candidates(rendered)):
        if entity.lower() not in master_lower:
            out.append(
                Violation(
                    code="entity_added",
                    fact_id=fact_id,
                    message=f"{entity!r} does not appear in the master text",
                )
            )

    master_numbers = set(_NUMBER.findall(master))
    for number in set(_NUMBER.findall(rendered)):
        if number not in master_numbers:
            out.append(
                Violation(
                    code="number_changed",
                    fact_id=fact_id,
                    message=f"{number!r} does not appear in the master text",
                )
            )

    for date in set(_MONTH_YEAR.findall(rendered)):
        if date not in master:
            out.append(
                Violation(
                    code="date_changed",
                    fact_id=fact_id,
                    message=f"{date!r} does not appear in the master text",
                )
            )

    rendered_tokens = _tokens(rendered)
    if rendered_tokens:
        overlap = sum(1 for token in rendered_tokens if token in master_tokens) / len(
            rendered_tokens
        )
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

    if plan.summary is not None:
        fact = resolve(plan.summary.fact_id, "summary")
        if fact is not None:
            _check_text(
                fact.id, fact.text, plan.summary.rendered_text, overlap_min, violations
            )

    for item in plan.skills:
        fact = resolve(item.fact_id, "skill_group")
        if fact is None:
            continue
        master_items = set(_split_items(fact.text))
        for skill in _split_items(item.rendered_text):
            if skill not in master_items:
                violations.append(
                    Violation(
                        code="skill_added",
                        fact_id=fact.id,
                        message=f"{skill!r} is not in the master skill group",
                    )
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
                    fact.id, fact.text, bullet.rendered_text, overlap_min, violations
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
    if plan.summary is not None:
        rendered.append(plan.summary)
    for planned in list(plan.experience) + list(plan.projects):
        rendered.extend(planned.bullets)
    return rendered
