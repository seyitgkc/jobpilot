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
from ..core import (
    blockers,
    capture,
    context,
    credentials,
    events,
    fill,
    mail,
    repo,
    resolve,
    rules,
    settings,
    state,
    tailor,
)
from ..core.browser_client import BrowserClient, BrowserDaemonError
from ..core.repo import NotFound
from ..util import from_json

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


def create_app(
    config: Config, paths: DataPaths, browser_client_factory=None
) -> FastAPI:
    if browser_client_factory is None:
        browser_client_factory = lambda: BrowserClient(  # noqa: E731
            f"http://127.0.0.1:{config.browser.daemon_port}"
        )

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

    def blocker_view(conn, blocker):
        spec = from_json(blocker.field_spec) if blocker.field_spec else None
        agreed = from_json(blocker.agreed_action) if blocker.agreed_action else None
        try:
            job = repo.get_job(conn, blocker.job_id)
        except NotFound:
            job = None
        return {
            "blocker": blocker,
            "job": job,
            "fields": (spec or {}).get("fields", []),
            "agreed": (agreed or {}).get("actions", []),
            "answerable": bool((spec or {}).get("fields"))
            and blocker.kind not in ("drift", "extraction_failed"),
        }

    def message(request: Request, title: str, text: str, back: str):
        return render(request, "message.html", title=title, text=text, back=back)

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
        tabs = None
        try:
            health = browser_client_factory().health()
            tabs = {"open": len(health.get("open_tabs", [])), "cap": config.browser.tab_cap}
        except Exception:
            pass
        return render(
            request,
            "dashboard.html",
            groups=groups,
            counts=counts,
            total=len(jobs),
            blocked=blocked,
            open_blockers=sum(blocked.values()),
            unassigned_mail=repo.unassigned_mail_count(conn),
            lost_tabs=[
                job
                for job in repo.jobs_with_tab_state(conn, "lost")
                if job.status not in state.TAB_IRRELEVANT
            ],
            tabs=tabs,
        )

    @app.post("/capture")
    def capture_job(request: Request, url: str = Form(...)):
        conn = db(request)
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="enter a full posting URL")
        try:
            result = capture.capture(
                conn, paths, config, browser_client_factory(), url, actor="owner"
            )
        except BrowserDaemonError as error:
            return message(
                request, "Capture needs the browser",
                f"The browser daemon did not respond ({error}). Start it with "
                f"jobpilot-browserd, then capture again.",
                "/",
            )
        return RedirectResponse(f"/jobs/{result.job.id}", status_code=303)

    @app.get("/jobs/{job_id}")
    def job_detail(request: Request, job_id: int):
        conn = db(request)
        job = load_job(conn, job_id)
        rows = context.active(conn, job_id)
        artifacts_list = repo.list_artifacts(conn, job_id)
        filled_shots = [a for a in artifacts_list if a.kind == "filled_screenshot"]
        return render(
            request,
            "job_detail.html",
            job=job,
            mail_rows=mail.list_for_job(conn, job_id),
            application=repo.get_application(conn, job_id),
            artifacts=artifacts_list,
            filled_shot=filled_shots[-1] if filled_shots else None,
            submit_approvable=job.status in ("filling", "filled") or bool(filled_shots),
            post_submit=job.status in (
                "submitted", "submitted_manually", "acknowledged",
                "in_process", "interview", "offer", "rejected",
            ),
            markable_manual=job.status in (
                "captured", "tailored", "filling", "filled", "manual_required"
            ),
            open_blockers=blockers.list_open(conn, job_id),
            summary_rows=[row for row in rows if row.kind == "summary"],
            context_rows=[row for row in rows if row.kind != "summary"],
            timeline=list(reversed(events.timeline(conn, job_id))),
            has_glance=tailor.glance(conn, paths, config, job_id) is not None,
            abandonable=state.is_legal(job.status, "abandoned"),
        )

    @app.post("/jobs/{job_id}/submit")
    def submit_now(request: Request, job_id: int):
        conn = db(request)
        job = load_job(conn, job_id)
        state.approve_submit(conn, job_id, "owner")
        for blocker in blockers.list_open(conn, job_id):
            if blocker.kind == "captcha":
                blockers.set_status(conn, blocker.id, "resolved", "owner")
        try:
            result = fill.submit_job(
                conn, paths, config, browser_client_factory(), job_id, actor="owner"
            )
        except BrowserDaemonError as error:
            return message(
                request, "Nothing was submitted",
                f"The browser daemon did not respond ({error}). Start it with "
                f"jobpilot-browserd, reopen the tab, and submit again.",
                f"/jobs/{job_id}",
            )
        if result.blockers:
            return RedirectResponse(f"/blockers/{result.blockers[0].id}", status_code=303)
        if result.error:
            return message(
                request, "Nothing was submitted",
                f"{result.error} The application is untouched.",
                f"/jobs/{job_id}",
            )
        return back_to_job(job_id)

    @app.post("/jobs/{job_id}/mark-submitted")
    def mark_submitted_route(request: Request, job_id: int):
        conn = db(request)
        load_job(conn, job_id)
        try:
            resolve.mark_submitted_manually(conn, job_id, "owner")
        except state.IllegalTransition as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return back_to_job(job_id)

    @app.post("/jobs/{job_id}/delete")
    def delete_job_route(request: Request, job_id: int):
        conn = db(request)
        load_job(conn, job_id)
        try:
            browser_client_factory().close_tab(job_id)
        except Exception:
            pass
        repo.delete_job(conn, paths, job_id, "owner")
        return RedirectResponse("/", status_code=303)

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
            resolve.abandon_job(conn, job_id, "owner")
        except state.IllegalTransition as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return back_to_job(job_id)

    @app.post("/jobs/{job_id}/replay")
    def replay(request: Request, job_id: int):
        conn = db(request)
        job = load_job(conn, job_id)
        try:
            result = resolve.replay_tab(
                conn, paths, config, browser_client_factory(), job_id, actor="owner"
            )
        except BrowserDaemonError as error:
            return message(
                request, "Tab not reopened",
                f"The browser daemon did not respond ({error}). Start it with "
                f"jobpilot-browserd and try again.",
                f"/jobs/{job_id}",
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if result.blockers or result.failed:
            return back_to_job(job_id)
        return back_to_job(job_id)

    @app.get("/blockers")
    def blockers_queue(request: Request):
        conn = db(request)
        views = [blocker_view(conn, blocker) for blocker in blockers.list_open(conn)]
        return render(request, "blockers.html", views=views)

    @app.get("/blockers/{blocker_id}")
    def blocker_detail(request: Request, blocker_id: int):
        conn = db(request)
        try:
            blocker = blockers.get_blocker(conn, blocker_id)
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return render(request, "blocker_detail.html", view=blocker_view(conn, blocker))

    async def _answer_from_form(request: Request, conn, blocker):
        form = await request.form()
        view = blocker_view(conn, blocker)
        actions = []
        reusable = []
        for spec in view["fields"]:
            field_id = spec["field_id"]
            field_type = spec.get("type") or "text"
            if field_type == "multiselect":
                values = form.getlist(f"value_{field_id}")
                if not values:
                    raise HTTPException(
                        status_code=400, detail=f"{field_id} needs an answer"
                    )
                actions.append({"verb": "select", "field_id": field_id, "value": values})
            else:
                raw = (form.get(f"value_{field_id}") or "").strip()
                if not raw:
                    raise HTTPException(
                        status_code=400, detail=f"{field_id} needs an answer"
                    )
                if field_type == "checkbox":
                    actions.append(
                        {"verb": "check", "field_id": field_id, "value": raw == "yes"}
                    )
                elif field_type in ("select", "radio"):
                    actions.append({"verb": "select", "field_id": field_id, "value": raw})
                else:
                    actions.append({"verb": "set", "field_id": field_id, "value": raw})
            if form.get(f"bank_{field_id}"):
                reusable.append(field_id)
        return actions, tuple(reusable)

    def _execute(request: Request, conn, blocker_id: int):
        try:
            result = resolve.execute_blocker(
                conn, paths, config, browser_client_factory(), blocker_id, actor="owner"
            )
        except BrowserDaemonError as error:
            return message(
                request, "Answers saved",
                f"The answers are recorded, but nothing was filled because the "
                f"browser did not respond ({error}). Start jobpilot-browserd "
                f"or reopen the tab, then use the fill button on the blocker.",
                "/blockers",
            )
        if result.drift is not None:
            return RedirectResponse(f"/blockers/{result.drift.id}", status_code=303)
        if result.failed:
            failed = ", ".join(outcome["field_id"] for outcome in result.failed)
            return message(
                request, "Some fields were not filled",
                f"These fields did not take the value: {failed}. The answers "
                f"are recorded; check the tab and retry the fill.",
                f"/blockers/{blocker_id}",
            )
        return RedirectResponse("/blockers", status_code=303)

    @app.post("/blockers/{blocker_id}/answer")
    async def answer_blocker_route(request: Request, blocker_id: int):
        conn = db(request)
        try:
            blocker = blockers.get_blocker(conn, blocker_id)
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        actions, reusable = await _answer_from_form(request, conn, blocker)
        try:
            resolve.answer_blocker(conn, blocker_id, actions, "owner", reusable)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return _execute(request, conn, blocker_id)

    @app.post("/blockers/{blocker_id}/execute")
    def execute_blocker_route(request: Request, blocker_id: int):
        conn = db(request)
        try:
            blockers.get_blocker(conn, blocker_id)
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return _execute(request, conn, blocker_id)

    @app.post("/blockers/{blocker_id}/resolve")
    def resolve_blocker_route(request: Request, blocker_id: int):
        conn = db(request)
        try:
            blocker = blockers.get_blocker(conn, blocker_id)
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        try:
            if blocker.kind == "resume_approval":
                state.approve_resume(conn, blocker.job_id, "owner")
            elif blocker.kind == "submit_approval":
                state.approve_submit(conn, blocker.job_id, "owner")
            resolve.mark_manual_resolved(conn, blocker_id, "owner")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse("/blockers", status_code=303)

    @app.get("/mail")
    def mail_page(request: Request):
        conn = db(request)
        unassigned = [
            {
                "mail": item,
                "preview": mail.body_text(paths, item, 400),
                "candidates": mail.candidates(conn, paths, item),
            }
            for item in mail.list_unassigned(conn)
        ]
        return render(
            request, "mail.html",
            unassigned=unassigned, jobs=repo.list_jobs(conn),
            tracking_start=mail.tracking_start(conn),
            tracked_address=config.mail.tracked_address,
        )

    @app.get("/mail/{mail_id}")
    def mail_detail(request: Request, mail_id: int):
        conn = db(request)
        try:
            item = mail.get_mail(conn, mail_id)
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        job = None
        if item.job_id is not None:
            try:
                job = repo.get_job(conn, item.job_id)
            except NotFound:
                job = None
        return render(
            request, "mail_detail.html",
            item=item, job=job, body=mail.body_text(paths, item, 20000),
        )

    @app.post("/mail/{mail_id}/assign")
    def mail_assign(request: Request, mail_id: int, job_id: int = Form(...)):
        conn = db(request)
        try:
            mail.assign(conn, paths, mail_id, job_id, "owner")
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return RedirectResponse("/mail", status_code=303)

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

    @app.get("/credentials")
    def credentials_page(request: Request, prefill: str = ""):
        conn = db(request)
        prefill_type, prefill_value = "domain", ""
        if ":" in prefill:
            head, _, tail = prefill.partition(":")
            if head in ("job", "company", "domain"):
                prefill_type, prefill_value = head, tail
        return render(
            request,
            "credentials.html",
            entries=credentials.list_credentials(conn),
            prefill_type=prefill_type,
            prefill_value=prefill_value,
        )

    @app.post("/credentials")
    def credential_add(
        request: Request,
        scope_type: str = Form(...),
        scope_value: str = Form(...),
        username: str = Form(...),
        password: str = Form(...),
        note: str = Form(""),
    ):
        conn = db(request)
        scope_value = scope_value.strip()
        if scope_type not in ("job", "company", "domain") or not scope_value:
            raise HTTPException(status_code=400, detail="pick a scope and value")
        if scope_type == "job" and not scope_value.isdigit():
            raise HTTPException(status_code=400, detail="job scope takes the job number")
        try:
            credentials.set_credential(
                conn, f"{scope_type}:{scope_value}", username, password, "owner",
                note=note.strip() or None,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse("/credentials", status_code=303)

    @app.post("/credentials/{credential_id}/delete")
    def credential_delete(request: Request, credential_id: int):
        conn = db(request)
        try:
            credentials.delete_credential(conn, credential_id, "owner")
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return RedirectResponse("/credentials", status_code=303)

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
