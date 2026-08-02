"""Tailoring pipeline: job posting in, validated resume artifacts out.

Flow: assemble constraints (active rules always injected), ask the provider
for a plan, validate mechanically, one provider repair round on rejection,
render LaTeX, compile with one repair round on failure. Anything that cannot
be resolved becomes a blocker and the job parks where it is; the .tex and
plan are stored even when the PDF cannot be built.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..config import Config, DataPaths
from ..providers.base import ProviderCallFailed, ProviderUnavailable
from ..providers.schemas import TailorConstraints, TailorPlan
from ..util import from_json
from . import artifacts, blockers, events, honesty, latex, repo, resume, rules, state
from .models import Artifact, Blocker, Job


@dataclass
class TailorResult:
    job: Job
    plan: TailorPlan | None = None
    report: honesty.ValidatorReport | None = None
    diff: dict | None = None
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    blocker: Blocker | None = None
    repairs: int = 0

    @property
    def ok(self) -> bool:
        return self.blocker is None


def _posting_markdown(conn: sqlite3.Connection, paths: DataPaths, job_id: int) -> str:
    postings = [
        artifact
        for artifact in repo.list_artifacts(conn, job_id)
        if artifact.kind == "posting_md"
    ]
    if not postings:
        raise ValueError(f"job {job_id} has no posting.md artifact; capture it first")
    return paths.absolute(postings[-1].path).read_text()


def _park(
    conn: sqlite3.Connection,
    job: Job,
    actor: str,
    question: str,
    result: TailorResult,
    payload: dict | None = None,
) -> TailorResult:
    if payload:
        events.append(conn, actor=actor, kind="tailor_failed", job_id=job.id, payload=payload)
        conn.commit()
    result.blocker = blockers.raise_blocker(
        conn, job.id, "other", actor, question=question
    )
    return result


def tailor_job(
    conn: sqlite3.Connection,
    paths: DataPaths,
    config: Config,
    provider,
    job_id: int,
    actor: str = "claude",
    notes: str | None = None,
    compile_fn=latex.compile_tex,
) -> TailorResult:
    job = repo.get_job(conn, job_id)
    result = TailorResult(job=job)
    posting = _posting_markdown(conn, paths, job_id)
    facts = resume.load_facts(paths)
    template_source = resume.load_template_source(paths)
    active = rules.active_rules(conn, "tailoring")
    overlap_min = config.validator.token_overlap_min

    constraints = TailorConstraints(
        rules=[rule.text for rule in active],
        token_overlap_min=overlap_min,
        notes=notes,
    )

    def call_provider(current: TailorConstraints) -> TailorPlan:
        return provider.tailor(facts, posting, current)

    try:
        plan = call_provider(constraints)
    except ProviderUnavailable as error:
        return _park(
            conn, job, actor,
            f"Resume tailoring needs an AI provider and none is available "
            f"({error}). Configure one, or tailor by hand and mark this "
            f"resolved.",
            result,
        )
    except ProviderCallFailed as error:
        return _park(
            conn, job, actor,
            f"The tailoring call failed: {error}. Retry once the cause is "
            f"addressed.",
            result,
        )

    report = honesty.validate_plan(plan, facts, active, overlap_min)
    if not report.ok:
        result.repairs += 1
        repair_constraints = constraints.model_copy(
            update={"validator_feedback": report.summary_lines()}
        )
        try:
            plan = call_provider(repair_constraints)
        except (ProviderUnavailable, ProviderCallFailed) as error:
            return _park(
                conn, job, actor,
                f"The tailoring repair call failed: {error}.",
                result,
            )
        report = honesty.validate_plan(plan, facts, active, overlap_min)
        if not report.ok:
            result.plan, result.report = plan, report
            flagged = "; ".join(report.summary_lines()[:6])
            return _park(
                conn, job, actor,
                f"Tailoring was rejected by the honesty check twice. "
                f"Flagged: {flagged}. Review the fact base or give guidance, "
                f"then request a new run.",
                result,
                payload={"violations": [v.model_dump() for v in report.violations]},
            )

    result.plan, result.report = plan, report
    result.diff = resume.plan_diff(facts, plan)

    tex = resume.render_tex(facts, plan, template_source)
    result.artifacts["tailor_plan_json"] = artifacts.store_text(
        conn, paths, job.id, "tailor_plan_json", "tailor_plan.json",
        plan.model_dump_json(indent=2), actor,
    )
    tex_artifact = artifacts.store_text(
        conn, paths, job.id, "resume_tex", "resume.tex", tex, actor
    )
    result.artifacts["resume_tex"] = tex_artifact

    def compile_current() -> Artifact:
        pdf_path = compile_fn(paths.absolute(tex_artifact.path))
        return artifacts.store_bytes(
            conn, paths, job.id, "resume_pdf", "resume.pdf",
            pdf_path.read_bytes(), actor,
        )

    try:
        try:
            result.artifacts["resume_pdf"] = compile_current()
        except latex.CompileError as error:
            result.repairs += 1
            repair_constraints = constraints.model_copy(
                update={"compile_log": error.log_excerpt}
            )
            plan = call_provider(repair_constraints)
            report = honesty.validate_plan(plan, facts, active, overlap_min)
            if not report.ok:
                raise latex.CompileError(
                    "repair plan failed validation", error.log_excerpt
                ) from error
            result.plan, result.report = plan, report
            result.diff = resume.plan_diff(facts, plan)
            tex = resume.render_tex(facts, plan, template_source)
            result.artifacts["tailor_plan_json"] = artifacts.store_text(
                conn, paths, job.id, "tailor_plan_json", "tailor_plan.json",
                plan.model_dump_json(indent=2), actor,
            )
            tex_artifact = artifacts.store_text(
                conn, paths, job.id, "resume_tex", "resume.tex", tex, actor
            )
            result.artifacts["resume_tex"] = tex_artifact
            result.artifacts["resume_pdf"] = compile_current()
    except latex.LatexUnavailable as error:
        _finish_partial(conn, job, actor, result)
        return _park(
            conn, job, actor,
            f"The resume PDF could not be built: {error}. The .tex source and "
            f"plan are saved; request a new run once TeX is installed.",
            result,
        )
    except latex.CompileError as error:
        _finish_partial(conn, job, actor, result)
        return _park(
            conn, job, actor,
            "The tailored resume failed to compile twice. The log excerpt is "
            "in the job timeline; adjust the template or facts, then request "
            "a new run.",
            result,
            payload={"log_excerpt": error.log_excerpt},
        )
    except (ProviderUnavailable, ProviderCallFailed) as error:
        _finish_partial(conn, job, actor, result)
        return _park(
            conn, job, actor,
            f"The compile repair call failed: {error}.",
            result,
        )

    repo.update_application_artifacts(
        conn, job.id, actor,
        resume_artifact_id=result.artifacts["resume_pdf"].id,
        tailor_plan_artifact_id=result.artifacts["tailor_plan_json"].id,
    )
    if job.status == "captured":
        result.job = state.transition(
            conn, job.id, "tailored", actor, payload={"repairs": result.repairs}
        )
    else:
        result.job = repo.get_job(conn, job.id)
    return result


def _finish_partial(
    conn: sqlite3.Connection, job: Job, actor: str, result: TailorResult
) -> None:
    """Link whatever was produced before the failure so the glance UI and a
    later manual path can use it."""
    plan_artifact = result.artifacts.get("tailor_plan_json")
    if plan_artifact is not None:
        repo.update_application_artifacts(
            conn, job.id, actor, tailor_plan_artifact_id=plan_artifact.id
        )


def load_plan_artifact(paths: DataPaths, artifact: Artifact) -> TailorPlan:
    data = from_json(paths.absolute(artifact.path).read_text())
    return TailorPlan.model_validate(data)
