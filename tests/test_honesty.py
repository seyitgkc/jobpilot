import pytest

from conftest import write_facts
from jobpilot.core import honesty, resume, rules
from jobpilot.providers.schemas import PlannedExperience, RenderedFact  # noqa: F401

OVERLAP_MIN = 0.35


@pytest.fixture
def facts(paths):
    write_facts(paths)
    return resume.load_facts(paths)


def validate(plan, facts, active_rules=()):
    return honesty.validate_plan(plan, facts, list(active_rules), OVERLAP_MIN)


def codes(report):
    return {violation.code for violation in report.violations}


def with_bullet(facts, text, experience="exp.acme", bullet="exp.acme.gateway"):
    plan = resume.full_plan(facts)
    return plan.model_copy(
        update={
            "experience": [
                PlannedExperience(
                    fact_id=experience,
                    bullets=[RenderedFact(fact_id=bullet, rendered_text=text)],
                )
            ]
        }
    )


def test_identity_plan_is_clean(facts):
    report = validate(resume.full_plan(facts), facts)
    assert report.ok
    assert report.violations == []


def test_faithful_trim_passes(facts):
    plan = with_bullet(facts, "Built the payments gateway in Go with 99.9% uptime")
    assert validate(plan, facts).ok


def test_entity_injection_rejected(facts):
    plan = with_bullet(
        facts, "Built the payments gateway in Go at Google scale, 2M requests per day"
    )
    report = validate(plan, facts)
    assert "entity_added" in codes(report)
    assert any("Google" in v.message for v in report.violations)


def test_number_drift_rejected(facts):
    plan = with_bullet(
        facts,
        "Rebuilt product search on PostgreSQL, improving relevance for 97% of queries",
        experience="exp.globex",
        bullet="exp.globex.search",
    )
    assert "number_changed" in codes(validate(plan, facts))


def test_date_drift_rejected(facts):
    plan = with_bullet(
        facts,
        "Migrated the billing monolith to microservices in June 2023, cutting deploy time 40%",
        bullet="exp.acme.migration",
    )
    report = validate(plan, facts)
    assert "date_changed" in codes(report)


def test_unrelated_rewrite_rejected(facts):
    plan = with_bullet(facts, "Responsible for various technical initiatives and stakeholder alignment")
    assert "low_overlap" in codes(validate(plan, facts))


def test_bullet_under_wrong_experience_rejected(facts):
    plan = resume.full_plan(facts)
    plan = plan.model_copy(
        update={
            "experience": [
                PlannedExperience(
                    fact_id="exp.globex",
                    bullets=[
                        RenderedFact(
                            fact_id="exp.acme.gateway",
                            rendered_text="Built the payments gateway in Go",
                        )
                    ],
                )
            ]
        }
    )
    assert "wrong_parent" in codes(validate(plan, facts))


def test_unknown_and_mismatched_facts_rejected(facts):
    plan = resume.full_plan(facts)
    plan = plan.model_copy(
        update={
            "summary": RenderedFact(fact_id="nope", rendered_text="x"),
            "projects": [PlannedExperience(fact_id="exp.acme", bullets=[])],
        }
    )
    report = validate(plan, facts)
    assert "unknown_fact" in codes(report)
    assert "kind_mismatch" in codes(report)


def test_duplicate_bullet_rejected(facts):
    plan = resume.full_plan(facts)
    acme = plan.experience[0]
    duplicated = acme.model_copy(update={"bullets": [acme.bullets[0], acme.bullets[0]]})
    plan = plan.model_copy(update={"experience": [duplicated]})
    assert "duplicate_fact" in codes(validate(plan, facts))


def test_skill_addition_rejected_selection_allowed(facts):
    plan = resume.full_plan(facts)
    plan = plan.model_copy(
        update={
            "skills": [
                RenderedFact(fact_id="skills.languages", rendered_text="SQL, Go, Rust")
            ]
        }
    )
    report = validate(plan, facts)
    assert "skill_added" in codes(report)
    assert any("rust" in v.message for v in report.violations)

    plan = plan.model_copy(
        update={
            "skills": [
                RenderedFact(fact_id="skills.languages", rendered_text="SQL, Go")
            ]
        }
    )
    assert validate(plan, facts).ok


def test_project_bullet_checks(facts):
    plan = resume.full_plan(facts)
    embellished = plan.model_copy(
        update={
            "projects": [
                PlannedExperience(
                    fact_id="proj.widget",
                    bullets=[
                        RenderedFact(
                            fact_id="proj.widget.rules",
                            rendered_text="Built a rules engine processing 50k events per second for Netflix",
                        )
                    ],
                )
            ]
        }
    )
    report = validate(embellished, facts)
    assert "number_changed" in codes(report)
    assert "entity_added" in codes(report)

    moved = plan.model_copy(
        update={
            "projects": [
                PlannedExperience(
                    fact_id="proj.widget",
                    bullets=[
                        RenderedFact(
                            fact_id="exp.acme.gateway",
                            rendered_text="Built the payments gateway in Go",
                        )
                    ],
                )
            ],
            "experience": [],
        }
    )
    assert "wrong_parent" in codes(validate(moved, facts))


def test_bad_section_rejected(facts):
    plan = resume.full_plan(facts)
    plan = plan.model_copy(update={"section_order": ["summary", "summary", "awards"]})
    report = validate(plan, facts)
    assert "bad_section" in codes(report)


def test_hard_rule_enforced(conn, facts):
    rules.add_owner_rule(
        conn, "banned_word", "tailoring", "never use the word synergy",
        pattern=r"\bsynergy\b", enforcement="hard",
    )
    active = rules.active_rules(conn, "tailoring")
    plan = with_bullet(
        facts, "Built the payments gateway in Go, driving synergy across 2M requests per day"
    )
    report = validate(plan, facts, active)
    assert "hard_rule" in codes(report)
    assert not report.ok


def test_bullet_may_use_entities_from_its_own_experience(facts):
    plan = with_bullet(
        facts,
        "Built the Go payments gateway behind the microservices billing stack, 2M requests per day",
    )
    assert validate(plan, facts).ok


def test_bullet_may_not_borrow_entities_from_another_experience(facts):
    plan = with_bullet(
        facts, "Built the payments gateway in Go on PostgreSQL, 2M requests per day"
    )
    report = validate(plan, facts)
    assert "entity_added" in codes(report)
    assert any("fact_add" in v.message for v in report.violations)


def test_bullet_numbers_stay_local_to_the_bullet(facts):
    plan = with_bullet(
        facts, "Built the payments gateway in Go, cutting deploy time 40%"
    )
    assert "number_changed" in codes(validate(plan, facts))


def test_summary_draws_on_the_whole_fact_base(facts):
    plan = resume.full_plan(facts).model_copy(
        update={
            "summary": RenderedFact(
                fact_id="summary.main",
                rendered_text=(
                    "Backend engineer with 5+ years building Go APIs and "
                    "PostgreSQL search on Kubernetes, handling 2M requests per day."
                ),
            )
        }
    )
    assert validate(plan, facts).ok


def test_summary_still_rejects_unknown_entities(facts):
    plan = resume.full_plan(facts).model_copy(
        update={
            "summary": RenderedFact(
                fact_id="summary.main",
                rendered_text="Backend engineer with 5+ years building APIs in Rust.",
            )
        }
    )
    assert "entity_added" in codes(validate(plan, facts))


def test_trailing_punctuation_does_not_fake_an_entity(facts):
    plan = resume.full_plan(facts).model_copy(
        update={
            "summary": RenderedFact(
                fact_id="summary.main",
                rendered_text="Backend engineer with 5+ years building APIs in Go and C#.",
            )
        }
    )
    assert validate(plan, facts).ok


def test_headline_is_grounded_in_the_fact_base(facts):
    plan = resume.full_plan(facts).model_copy(
        update={"headline": "Senior Backend Engineer | Go, PostgreSQL & AWS"}
    )
    assert validate(plan, facts).ok
    bad = plan.model_copy(update={"headline": "Senior Backend Engineer | Rust & Azure"})
    assert "entity_added" in codes(validate(bad, facts))


def test_skills_move_between_groups_and_take_new_labels(facts):
    plan = resume.full_plan(facts).model_copy(
        update={
            "skills": [
                RenderedFact(
                    fact_id="skills.infra",
                    rendered_text="Go, Docker, AWS, SQL",
                    label="Cloud Backend",
                )
            ]
        }
    )
    assert validate(plan, facts).ok
    bad_label = plan.model_copy(
        update={
            "skills": [
                RenderedFact(
                    fact_id="skills.infra", rendered_text="Docker", label="Azure Stack"
                )
            ]
        }
    )
    assert "entity_added" in codes(validate(bad_label, facts))
