"""Fact base loading, LaTeX rendering, and resume diff data.

facts.yaml and an optional template override live in data/resume_master/;
a packaged default template is used otherwise. The renderer builds a fully
escaped context in Python and keeps the Jinja template dumb, so template
errors stay template errors and content errors stay content errors.
"""

from __future__ import annotations

import re
from importlib import resources

import jinja2
import yaml

from ..config import DataPaths
from ..providers.schemas import (
    Fact,
    FactBase,
    PlannedExperience,
    RenderedFact,
    SECTIONS,
    TailorPlan,
)


class ResumeSetupError(Exception):
    pass


_TEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "|": r"$|$",
}

_TEX_PATTERN = re.compile("|".join(re.escape(ch) for ch in _TEX_SPECIALS))


def tex_escape(text: str) -> str:
    return _TEX_PATTERN.sub(lambda match: _TEX_SPECIALS[match.group(0)], text)


def _tex_dates(dates: str | None) -> str:
    """Render date ranges with a LaTeX en-dash ligature."""
    if not dates:
        return ""
    return tex_escape(dates).replace(" - ", " -- ")


def load_facts(paths: DataPaths) -> FactBase:
    file = paths.resume_master_dir / "facts.yaml"
    if not file.exists():
        raise ResumeSetupError(
            f"fact base not found at {file}; create it before tailoring"
        )
    try:
        data = yaml.safe_load(file.read_text())
        return FactBase.model_validate(data)
    except Exception as error:
        raise ResumeSetupError(f"invalid facts.yaml: {error}") from error


def load_template_source(paths: DataPaths) -> str:
    override = paths.resume_master_dir / "template.tex.j2"
    if override.exists():
        return override.read_text()
    return resources.files("jobpilot.templates").joinpath("resume.tex.j2").read_text()


def _jinja_env() -> jinja2.Environment:
    return jinja2.Environment(
        block_start_string="\\BLOCK{",
        block_end_string="}",
        variable_start_string="\\VAR{",
        variable_end_string="}",
        comment_start_string="\\#{",
        comment_end_string="}",
        trim_blocks=True,
        autoescape=False,
        undefined=jinja2.StrictUndefined,
    )


def _contact_line(facts: FactBase) -> str:
    profile = facts.profile
    pieces = []
    if profile.location:
        pieces.append(tex_escape(profile.location))
    if profile.phone:
        pieces.append(tex_escape(profile.phone))
    if profile.email:
        pieces.append(
            rf"\href{{mailto:{profile.email}}}{{\underline{{{tex_escape(profile.email)}}}}}"
        )
    for link in (profile.linkedin, profile.github):
        if link:
            pieces.append(
                rf"\href{{https://{link}}}{{\underline{{{tex_escape(link)}}}}}"
            )
    return " $|$ ".join(pieces)


def _subheading(fact: Fact) -> dict:
    return {
        "org": tex_escape(fact.org or ""),
        "role": tex_escape(fact.role or ""),
        "location": tex_escape(fact.location or ""),
        "dates": _tex_dates(fact.dates),
        "title": tex_escape(fact.text or ""),
    }


def _build_sections(facts: FactBase, plan: TailorPlan) -> list[dict]:
    sections: list[dict] = []
    for name in plan.section_order:
        if name == "summary" and plan.summary is not None:
            sections.append(
                {"kind": "summary", "text": tex_escape(plan.summary.rendered_text)}
            )
        elif name == "skills" and plan.skills:
            groups = []
            for item in plan.skills:
                fact = facts.by_id(item.fact_id)
                groups.append(
                    {
                        "label": tex_escape(fact.label or ""),
                        "entries": tex_escape(item.rendered_text),
                    }
                )
            sections.append({"kind": "skills", "groups": groups})
        elif name == "experience" and plan.experience:
            entries = []
            for planned in plan.experience:
                fact = facts.by_id(planned.fact_id)
                entry = _subheading(fact)
                entry["bullets"] = [
                    tex_escape(bullet.rendered_text) for bullet in planned.bullets
                ]
                entries.append(entry)
            sections.append({"kind": "experience", "entries": entries})
        elif name == "projects" and plan.projects:
            names = ", ".join(
                tex_escape(item.rendered_text) for item in plan.projects
            )
            sections.append({"kind": "projects", "names": names})
        elif name == "education":
            entries = [_subheading(fact) for fact in facts.of_kind("education")]
            if entries:
                sections.append({"kind": "education", "entries": entries})
        elif name == "certifications":
            entries = [_subheading(fact) for fact in facts.of_kind("certification")]
            if entries:
                sections.append({"kind": "certifications", "entries": entries})
        elif name == "languages" and facts.profile.languages:
            sections.append(
                {"kind": "languages", "text": tex_escape(facts.profile.languages)}
            )
    return sections


def render_tex(facts: FactBase, plan: TailorPlan, template_source: str) -> str:
    template = _jinja_env().from_string(template_source)
    context = {
        "name": tex_escape(facts.profile.name),
        "headline": tex_escape(facts.profile.headline) if facts.profile.headline else None,
        "contact": _contact_line(facts),
        "sections": _build_sections(facts, plan),
    }
    return template.render(**context)


def full_plan(facts: FactBase) -> TailorPlan:
    """The identity plan: everything in the fact base, master order and
    master text. Renders the master resume; also the reference for diffs."""
    summaries = facts.of_kind("summary")
    return TailorPlan(
        section_order=list(SECTIONS),
        summary=(
            RenderedFact(fact_id=summaries[0].id, rendered_text=summaries[0].text)
            if summaries
            else None
        ),
        skills=[
            RenderedFact(fact_id=fact.id, rendered_text=fact.text)
            for fact in facts.of_kind("skill_group")
        ],
        experience=[
            PlannedExperience(
                fact_id=fact.id,
                bullets=[
                    RenderedFact(fact_id=bullet.id, rendered_text=bullet.text)
                    for bullet in facts.bullets_of(fact.id)
                ],
            )
            for fact in facts.of_kind("experience")
        ],
        projects=[
            RenderedFact(fact_id=fact.id, rendered_text=fact.text)
            for fact in facts.of_kind("project")
        ],
    )


def _bullet_pair(facts: FactBase, bullet: RenderedFact) -> dict:
    master_fact = facts.by_id(bullet.fact_id)
    master_text = master_fact.text if master_fact else None
    return {
        "fact_id": bullet.fact_id,
        "master": master_text,
        "rendered": bullet.rendered_text,
        "changed": master_text != bullet.rendered_text,
    }


def plan_diff(facts: FactBase, plan: TailorPlan) -> dict:
    """Master-versus-plan data for the resume glance: what was dropped,
    reordered, and reworded."""
    experiences = []
    planned_by_id = {entry.fact_id: entry for entry in plan.experience}
    for fact in facts.of_kind("experience"):
        planned = planned_by_id.get(fact.id)
        master_bullets = facts.bullets_of(fact.id)
        master_ids = [bullet.id for bullet in master_bullets]
        if planned is None:
            experiences.append(
                {
                    "fact_id": fact.id,
                    "org": fact.org,
                    "role": fact.role,
                    "included": False,
                    "dropped_bullets": master_ids,
                    "order_changed": False,
                    "bullets": [],
                }
            )
            continue
        planned_ids = [bullet.fact_id for bullet in planned.bullets]
        kept_master_order = [b for b in master_ids if b in planned_ids]
        experiences.append(
            {
                "fact_id": fact.id,
                "org": fact.org,
                "role": fact.role,
                "included": True,
                "dropped_bullets": [b for b in master_ids if b not in planned_ids],
                "order_changed": planned_ids != kept_master_order,
                "bullets": [
                    _bullet_pair(facts, bullet) for bullet in planned.bullets
                ],
            }
        )

    def selection(kind: str, planned_items: list[RenderedFact]) -> dict:
        master = [fact.id for fact in facts.of_kind(kind)]
        chosen = [item.fact_id for item in planned_items]
        return {
            "included": chosen,
            "dropped": [fact_id for fact_id in master if fact_id not in chosen],
        }

    summary = None
    if plan.summary is not None:
        master_summary = facts.by_id(plan.summary.fact_id)
        summary = {
            "master": master_summary.text if master_summary else None,
            "rendered": plan.summary.rendered_text,
        }
    return {
        "summary": summary,
        "experiences": experiences,
        "skills": selection("skill_group", plan.skills),
        "projects": selection("project", plan.projects),
    }
