"""Panel routes. Thin by design: validate input, call core, render."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from importlib import resources

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Config, DataPaths
from ..db import open_db
from ..core import blockers, context, events, repo, rules, settings, state, tailor
from ..core.repo import NotFound

STATUS_ORDER = (
    "captured",
    "tailored",
    "filling",
    "filled",
    "submitting",
    "submitted",
    "acknowledged",
    "in_process",
    "interview",
    "offer",
    "manual_required",
    "submitted_manually",
    "rejected",
    "abandoned",
)

RULE_STATUS_ORDER = ("proposed", "active", "retired", "rejected")


def create_app(config: Config, paths: DataPaths) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.conn = open_db(paths, config)
        yield
        app.state.conn.close()

    app = FastAPI(title="jobpilot-admin", lifespan=lifespan)
    package = resources.files("jobpilot.admin")
    app.mount("/static", StaticFiles(directory=str(package / "static")), name="static")
    templates = Jinja2Templates(directory=str(package / "templates"))
    templates.env.filters["human"] = lambda value: str(value).replace("_", " ")

    def db(request: Request) -> sqlite3.Connection:
        return request.app.state.conn

    def render(request: Request, name: str, **ctx):
        return templates.TemplateResponse(request, name, ctx)

    def load_job(conn: sqlite3.Connection, job_id: int):
        try:
            return repo.get_job(conn, job_id)
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    def back_to_job(job_id: int) -> RedirectResponse:
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/")
    def dashboard(request: Request):
        conn = db(request)
        jobs = repo.list_jobs(conn)
        counts = repo.status_counts(conn)
        blocked = blockers.open_counts(conn)
        groups = []
        for status in STATUS_ORDER:
            members = sorted(
                (job for job in jobs if job.status == status),
                key=lambda job: job.updated_at,
                reverse=True,
            )
            if members:
                groups.append({"status": status, "jobs": members})
        return render(
            request,
            "dashboard.html",
            groups=groups,
            counts=counts,
            total=len(jobs),
            blocked=blocked,
            open_blockers=sum(blocked.values()),
            unassigned_mail=repo.unassigned_mail_count(conn),
        )

    @app.get("/jobs/{job_id}")
    def job_detail(request: Request, job_id: int):
        conn = db(request)
        job = load_job(conn, job_id)
        rows = context.active(conn, job_id)
        return render(
            request,
            "job_detail.html",
            job=job,
            application=repo.get_application(conn, job_id),
            artifacts=repo.list_artifacts(conn, job_id),
            open_blockers=blockers.list_open(conn, job_id),
            summary_rows=[row for row in rows if row.kind == "summary"],
            context_rows=[row for row in rows if row.kind != "summary"],
            timeline=list(reversed(events.timeline(conn, job_id))),
            has_glance=tailor.glance(conn, paths, config, job_id) is not None,
            abandonable=state.is_legal(job.status, "abandoned"),
        )

    @app.get("/jobs/{job_id}/artifacts/{artifact_id}")
    def artifact_file(request: Request, job_id: int, artifact_id: int):
        conn = db(request)
        load_job(conn, job_id)
        try:
            artifact = repo.get_artifact(conn, artifact_id)
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if artifact.job_id != job_id:
            raise HTTPException(status_code=404, detail="artifact not on this job")
        file_path = paths.absolute(artifact.path)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="artifact file missing on disk")
        return FileResponse(file_path)

    @app.get("/jobs/{job_id}/glance")
    def glance_page(request: Request, job_id: int):
        conn = db(request)
        job = load_job(conn, job_id)
        data = tailor.glance(conn, paths, config, job_id)
        if data is None:
            return render(request, "glance.html", job=job, data=None)
        return render(request, "glance.html", job=job, data=data)

    @app.post("/jobs/{job_id}/approve-resume")
    def approve_resume(request: Request, job_id: int):
        conn = db(request)
        load_job(conn, job_id)
        state.approve_resume(conn, job_id, "owner")
        return back_to_job(job_id)

    @app.post("/jobs/{job_id}/approve-submit")
    def approve_submit(request: Request, job_id: int):
        conn = db(request)
        load_job(conn, job_id)
        state.approve_submit(conn, job_id, "owner")
        return back_to_job(job_id)

    @app.post("/jobs/{job_id}/gates")
    def set_gates(
        request: Request,
        job_id: int,
        resume_glance_gate: str = Form(...),
        submit_gate: str = Form(...),
    ):
        conn = db(request)
        load_job(conn, job_id)
        try:
            repo.set_job_gates(
                conn, job_id, "owner",
                resume_glance_gate=resume_glance_gate, submit_gate=submit_gate,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return back_to_job(job_id)

    @app.post("/jobs/{job_id}/abandon")
    def abandon(request: Request, job_id: int):
        conn = db(request)
        load_job(conn, job_id)
        try:
            state.transition(conn, job_id, "abandoned", "owner")
        except state.IllegalTransition as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return back_to_job(job_id)

    @app.get("/rules")
    def rules_page(request: Request):
        conn = db(request)
        all_rules = rules.list_rules(conn)
        groups = {
            status: [rule for rule in all_rules if rule.status == status]
            for status in RULE_STATUS_ORDER
        }
        return render(
            request,
            "rules.html",
            groups=groups,
            kinds=sorted(rules.KINDS),
            enforcements=sorted(rules.ENFORCEMENTS),
        )

    @app.post("/rules")
    def add_rule(
        request: Request,
        kind: str = Form(...),
        scope: str = Form("global"),
        text: str = Form(...),
        pattern: str = Form(""),
        enforcement: str = Form("soft"),
    ):
        conn = db(request)
        try:
            rules.add_owner_rule(
                conn, kind, scope.strip() or "global", text.strip(),
                pattern=pattern.strip() or None, enforcement=enforcement,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse("/rules", status_code=303)

    @app.post("/rules/{rule_id}/status")
    def rule_status(request: Request, rule_id: int, status: str = Form(...)):
        conn = db(request)
        try:
            rules.set_status(conn, rule_id, status, "owner")
        except (ValueError, NotFound, rules.NotFound) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse("/rules", status_code=303)

    @app.get("/settings")
    def settings_page(request: Request):
        conn = db(request)
        return render(
            request,
            "settings.html",
            gates={
                key: settings.get(conn, key, "on")
                for key in settings.GATE_KEYS
            },
            config_values=[
                ("Browser tab cap", config.browser.tab_cap),
                ("Browser settle delay (ms)", config.browser.settle_ms),
                ("AI provider", config.ai.provider),
                ("AI model", config.ai.model),
                ("Notify channels", ", ".join(config.notify.channels)),
                ("Mail match threshold", config.mail.match_threshold),
                ("Paraphrase overlap minimum", config.validator.token_overlap_min),
            ],
        )

    @app.post("/settings/gates")
    def set_settings(
        request: Request,
        resume_glance: str = Form(...),
        submit_approval: str = Form(...),
        auto_submit: str = Form(...),
    ):
        conn = db(request)
        values = {
            "gates.resume_glance": resume_glance,
            "gates.submit_approval": submit_approval,
            "gates.auto_submit": auto_submit,
        }
        for key, value in values.items():
            if value not in ("on", "off"):
                raise HTTPException(status_code=400, detail=f"{key} must be on or off")
            if settings.get(conn, key) != value:
                settings.set(conn, key, value, "owner")
        return RedirectResponse("/settings", status_code=303)

    return app
