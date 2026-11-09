import pytest

from conftest import write_facts
from jobpilot.core import blockers, events, factgaps, honesty, repo, resolve, resume
from jobpilot.providers.schemas import PlannedExperience, RenderedFact


@pytest.fixture
def job(conn, paths):
    write_facts(paths)
    return repo.create_job(conn, "https://example.com/jobs/1", "claude")


CPANEL = {
    "id": "exp.acme.cpanel",
    "kind": "bullet",
    "parent": "exp.acme",
    "text": "Administered client hosting through cPanel and Plesk",
}


def _plan_with_cpanel(facts):
    plan = resume.full_plan(facts)
    return plan.model_copy(
        update={
            "experience": [
                PlannedExperience(
                    fact_id="exp.acme",
                    bullets=[
                        RenderedFact(
                            fact_id="exp.acme.cpanel",
                            rendered_text="Administered client hosting through cPanel and Plesk",
                        )
                    ],
                )
            ]
        }
    )


def test_fact_add_requires_the_owners_answer(conn, paths, job):
    with pytest.raises(ValueError, match="owner_answer"):
        factgaps.add_fact(conn, paths, job.id, CPANEL, "master", "  ")
    assert resume.load_facts(paths).by_id("exp.acme.cpanel") is None


def test_job_scope_is_visible_only_to_that_job(conn, paths, job):
    factgaps.add_fact(conn, paths, job.id, CPANEL, "job", "yes, this resume only")
    assert resume.load_facts(paths).by_id("exp.acme.cpanel") is None
    job_facts = resume.load_facts(paths, job.id)
    assert job_facts.by_id("exp.acme.cpanel") is not None
    report = honesty.validate_plan(_plan_with_cpanel(job_facts), job_facts, [], 0.25)
    assert report.ok
    timeline = events.timeline(conn, job.id)
    added = [e for e in timeline if e.kind == "fact_added"]
    assert added and "this resume only" in added[-1].payload


def test_master_scope_appends_and_keeps_comments(conn, paths, job):
    file = paths.resume_master_dir / "facts.yaml"
    file.write_text("# owner comment\n" + file.read_text())
    factgaps.add_fact(conn, paths, job.id, CPANEL, "master", "add it to my facts")
    text = file.read_text()
    assert text.startswith("# owner comment\n")
    assert resume.load_facts(paths).by_id("exp.acme.cpanel").parent == "exp.acme"


def test_bad_fact_never_reaches_disk(conn, paths, job):
    file = paths.resume_master_dir / "facts.yaml"
    before = file.read_text()
    with pytest.raises(Exception):
        factgaps.add_fact(
            conn, paths, job.id, CPANEL | {"parent": "exp.nowhere"}, "master", "yes"
        )
    with pytest.raises(Exception):
        factgaps.add_fact(
            conn, paths, job.id, CPANEL | {"id": "exp.acme.gateway"}, "master", "yes"
        )
    assert file.read_text() == before


def test_gap_blocker_roundtrip(conn, paths, job):
    blocker = factgaps.ask_gaps(
        conn, paths, job.id,
        [
            {"fact": CPANEL, "posting_ask": "cPanel a plus", "suggested_scope": "master"},
            {
                "fact": {
                    "id": "skills.perl", "kind": "skill_group",
                    "label": "Scripting", "text": "Perl",
                },
                "posting_ask": "Perl a plus",
            },
        ],
    )
    assert blocker.kind == "fact_gap"
    with pytest.raises(ValueError, match="fact_gaps_resolve"):
        resolve.answer_blocker(conn, blocker.id, [], "owner")
    with pytest.raises(ValueError, match="no decision"):
        factgaps.resolve_gaps(
            conn, paths, blocker.id, [{"index": 0, "decision": "master"}], "first only"
        )
    result = factgaps.resolve_gaps(
        conn, paths, blocker.id,
        [
            {"index": 0, "decision": "master", "text": "Ran client hosting on cPanel and Plesk"},
            {"index": 1, "decision": "skip"},
        ],
        "yes to cPanel, I don't know Perl",
    )
    assert result["skipped"] == [1]
    fact = resume.load_facts(paths).by_id("exp.acme.cpanel")
    assert fact.text == "Ran client hosting on cPanel and Plesk"
    assert resume.load_facts(paths).by_id("skills.perl") is None
    assert blockers.get_blocker(conn, blocker.id).status == "resolved"


def test_gap_proposals_are_checked_up_front(conn, paths, job):
    with pytest.raises(ValueError, match="already exists"):
        factgaps.ask_gaps(
            conn, paths, job.id, [{"fact": CPANEL | {"id": "exp.acme.gateway"}}]
        )
    with pytest.raises(ValueError, match="parent"):
        factgaps.ask_gaps(
            conn, paths, job.id, [{"fact": CPANEL | {"parent": "exp.nowhere"}}]
        )


def test_headline_and_labels_render(paths):
    write_facts(paths)
    facts = resume.load_facts(paths)
    plan = resume.full_plan(facts).model_copy(
        update={
            "headline": "Go Backend Engineer",
            "skills": [
                RenderedFact(
                    fact_id="skills.infra", rendered_text="Go, AWS", label="Cloud Backend"
                )
            ],
        }
    )
    tex = resume.render_tex(facts, plan, resume.load_template_source(paths))
    assert "Go Backend Engineer" in tex
    assert "Cloud Backend" in tex
    diff = resume.plan_diff(facts, plan)
    assert diff["headline"]["rendered"] == "Go Backend Engineer"
    assert diff["skills"]["groups"][0]["label"] == "Cloud Backend"
