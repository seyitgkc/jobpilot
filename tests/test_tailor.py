import pytest

from conftest import write_facts
from jobpilot.config import Config
from jobpilot.core import artifacts, blockers, latex, repo, resume, rules, tailor
from jobpilot.providers.base import NullProvider, ProviderUnavailable
from jobpilot.providers.schemas import PlannedExperience, RenderedFact

POSTING = "# Platform Engineer\n\nGo, PostgreSQL, payments experience.\n"


class FakeProvider:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def tailor(self, facts, job_md, constraints):
        self.calls.append(constraints)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome(facts)


def good_plan(facts):
    return resume.full_plan(facts)


def bad_plan(facts):
    plan = resume.full_plan(facts)
    return plan.model_copy(
        update={
            "experience": [
                PlannedExperience(
                    fact_id="exp.acme",
                    bullets=[
                        RenderedFact(
                            fact_id="exp.acme.gateway",
                            rendered_text="Built the payments gateway at Google",
                        )
                    ],
                )
            ]
        }
    )


def fake_compile(tex_path):
    pdf_path = tex_path.with_suffix(".pdf")
    pdf_path.write_bytes(b"%PDF-1.4 fake\n")
    return pdf_path


class FailingCompiler:
    def __init__(self, failures, error=None):
        self.failures = failures
        self.error = error or latex.CompileError(
            "latexmk failed", "! Undefined control sequence."
        )

    def __call__(self, tex_path):
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        return fake_compile(tex_path)


@pytest.fixture
def job(conn, paths):
    write_facts(paths)
    job = repo.create_job(conn, "https://example.com/job", "claude")
    artifacts.store_text(conn, paths, job.id, "posting_md", "posting.md", POSTING, "claude")
    return job


def run(conn, paths, provider, job, compile_fn=fake_compile, notes=None):
    return tailor.tailor_job(
        conn, paths, Config(), provider, job.id, notes=notes, compile_fn=compile_fn
    )


def test_happy_path(conn, paths, job):
    rules.add_owner_rule(conn, "style", "tailoring", "no first-person in bullets")
    provider = FakeProvider([good_plan])
    result = run(conn, paths, provider, job)

    assert result.ok
    assert result.repairs == 0
    assert result.job.status == "tailored"
    assert result.report.ok
    assert set(result.artifacts) == {"tailor_plan_json", "resume_tex", "resume_pdf"}
    for artifact in result.artifacts.values():
        assert paths.absolute(artifact.path).exists()

    application = repo.get_application(conn, job.id)
    assert application.resume_artifact_id == result.artifacts["resume_pdf"].id
    assert application.tailor_plan_artifact_id == result.artifacts["tailor_plan_json"].id

    assert "no first-person in bullets" in provider.calls[0].rules
    stored = tailor.load_plan_artifact(paths, result.artifacts["tailor_plan_json"])
    assert stored == result.plan
    assert result.diff["projects"]["dropped"] == []


def test_validator_reject_then_repair(conn, paths, job):
    provider = FakeProvider([bad_plan, good_plan])
    result = run(conn, paths, provider, job)

    assert result.ok
    assert result.repairs == 1
    assert result.job.status == "tailored"
    feedback = provider.calls[1].validator_feedback
    assert feedback and any("entity_added" in line for line in feedback)


def test_validator_reject_twice_parks(conn, paths, job):
    provider = FakeProvider([bad_plan, bad_plan])
    result = run(conn, paths, provider, job)

    assert not result.ok
    assert result.blocker.kind == "other"
    assert "honesty check" in result.blocker.question
    assert repo.get_job(conn, job.id).status == "captured"
    assert result.artifacts == {}
    assert blockers.is_blocked(conn, job.id)


def test_provider_unavailable_parks(conn, paths, job):
    result = run(conn, paths, NullProvider(), job)

    assert not result.ok
    assert "AI provider" in result.blocker.question
    assert repo.get_job(conn, job.id).status == "captured"


def test_provider_error_on_repair_parks(conn, paths, job):
    provider = FakeProvider([bad_plan, ProviderUnavailable("key expired")])
    result = run(conn, paths, provider, job)
    assert not result.ok
    assert repo.get_job(conn, job.id).status == "captured"


def test_compile_failure_then_repair(conn, paths, job):
    provider = FakeProvider([good_plan, good_plan])
    result = run(conn, paths, provider, job, compile_fn=FailingCompiler(1))

    assert result.ok
    assert result.repairs == 1
    assert result.job.status == "tailored"
    assert "Undefined control sequence" in provider.calls[1].compile_log


def test_compile_failure_twice_parks(conn, paths, job):
    provider = FakeProvider([good_plan, good_plan])
    result = run(conn, paths, provider, job, compile_fn=FailingCompiler(2))

    assert not result.ok
    assert "failed to compile" in result.blocker.question
    assert "resume_pdf" not in result.artifacts
    assert paths.absolute(result.artifacts["resume_tex"].path).exists()
    assert repo.get_job(conn, job.id).status == "captured"
    application = repo.get_application(conn, job.id)
    assert application.tailor_plan_artifact_id is not None
    assert application.resume_artifact_id is None


def test_latex_unavailable_parks_with_sources(conn, paths, job):
    def no_latex(tex_path):
        raise latex.LatexUnavailable("latexmk and pdflatex are required")

    provider = FakeProvider([good_plan])
    result = run(conn, paths, provider, job, compile_fn=no_latex)

    assert not result.ok
    assert "TeX" in result.blocker.question
    assert paths.absolute(result.artifacts["resume_tex"].path).exists()
    assert repo.get_job(conn, job.id).status == "captured"


def test_retailor_keeps_status_and_updates_links(conn, paths, job):
    first = run(conn, paths, FakeProvider([good_plan]), job)
    assert first.job.status == "tailored"

    second = run(conn, paths, FakeProvider([good_plan]), first.job)
    assert second.ok
    assert second.job.status == "tailored"
    application = repo.get_application(conn, job.id)
    assert application.resume_artifact_id == second.artifacts["resume_pdf"].id
    assert second.artifacts["resume_pdf"].id != first.artifacts["resume_pdf"].id


def test_missing_posting_raises(conn, paths):
    write_facts(paths)
    job = repo.create_job(conn, "https://example.com/x", "claude")
    with pytest.raises(ValueError):
        tailor.tailor_job(conn, paths, Config(), FakeProvider([good_plan]), job.id)
