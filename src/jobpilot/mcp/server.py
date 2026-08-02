"""Tool surface for MCP clients (Claude, Cowork).

The session driving these tools is the model: tailoring and field mapping
have context/submit pairs so the session authors plans and mappings itself
and core validates mechanically. No tool here contains logic; everything is
a core call, so a chat action and the same panel action write the same rows.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Image

from ..config import Config, DataPaths, load_config
from ..db import open_db
from ..core import agenda as agenda_module
from ..core import answers as answers_module
from ..core import (
    blockers,
    context as context_module,
    credentials,
    events,
    fill,
    repo,
    resolve,
    rules as rules_module,
    settings,
    state,
    tailor,
)
from ..core.browser_client import BrowserClient
from ..core import capture as capture_module
from ..core import mail as mail_module
from ..core.injection import UNTRUSTED_NOTICE, findings, wrap_untrusted
from ..providers.schemas import FieldMapping, TailorPlan
from ..util import from_json


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(value).items()}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class JobpilotApi:
    """The callable surface behind the MCP tools, testable directly."""

    def __init__(self, config: Config, paths: DataPaths, conn, client_factory):
        self.config = config
        self.paths = paths
        self.conn = conn
        self.client_factory = client_factory

    def agenda(self) -> dict:
        """Everything waiting across the pipeline: jobs to tailor, reviews
        and approvals pending, answered blockers to execute, unassigned
        mail, lost tabs. The natural first call of any working session."""
        return agenda_module.agenda(self.conn)

    async def agenda_wait(
        self, after_event_id: int = 0, timeout_seconds: int = 60
    ) -> dict:
        """Wait for work, then return the agenda. Blocks until something the
        session can act on appears (a posting captured in the panel, an
        answered blocker, mail to classify) or the timeout expires.

        Pass the last_event_id from the previous result to wake only on what
        happened since. On timeout it returns work=false; call it again to
        keep standing watch."""
        timeout_seconds = max(5, min(int(timeout_seconds), 300))
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            view = agenda_module.agenda(self.conn)
            fresh = view["last_event_id"] > after_event_id
            if agenda_module.has_work(view) and (fresh or not after_event_id):
                return {"work": True, "timed_out": False, "agenda": view}
            if asyncio.get_running_loop().time() >= deadline:
                return {
                    "work": False,
                    "timed_out": True,
                    "last_event_id": view["last_event_id"],
                }
            await asyncio.sleep(2)

    # Jobs

    def job_capture(self, url: str) -> dict:
        result = capture_module.capture(
            self.conn, self.paths, self.config, self.client_factory(), url
        )
        return _plain(
            {
                "job": result.job,
                "artifacts": {k: v.path for k, v in result.artifacts.items()},
                "blockers": result.blockers,
            }
        )

    def job_get(self, job_id: int) -> dict:
        bundle = context_module.assemble(self.conn, job_id)
        application = bundle["application"]
        return _plain(
            {
                "job": bundle["job"],
                "application": application,
                "artifacts": bundle["artifacts"],
                "context": bundle["context"],
                "open_blocker_count": bundle["open_blocker_count"],
                "field_fills": (
                    repo.list_field_fills(self.conn, application.id)
                    if application
                    else []
                ),
            }
        )

    def job_list(self, status: str | None = None) -> list[dict]:
        return _plain(repo.list_jobs(self.conn, status))

    def job_approve_resume(self, job_id: int) -> dict:
        state.approve_resume(self.conn, job_id, "owner")
        return _plain(repo.get_application(self.conn, job_id))

    def job_approve_submit(self, job_id: int) -> dict:
        state.approve_submit(self.conn, job_id, "owner")
        return _plain(repo.get_application(self.conn, job_id))

    def job_abandon(self, job_id: int) -> dict:
        return _plain(resolve.abandon_job(self.conn, job_id, "owner"))

    def job_delete(self, job_id: int) -> dict:
        """Remove the job with all of its records, artifacts, and files.
        Assigned mail returns to the unassigned queue. No undo."""
        try:
            self.client_factory().close_tab(job_id)
        except Exception:
            pass
        repo.delete_job(self.conn, self.paths, job_id, "owner")
        return {"deleted": job_id}

    def job_mark_manual_submitted(self, job_id: int, note: str = "") -> dict:
        return _plain(
            resolve.mark_submitted_manually(self.conn, job_id, "owner", note or None)
        )

    # Tailoring: the session authors the plan.

    def job_tailor_context(self, job_id: int, notes: str = "") -> dict:
        payload = _plain(
            tailor.tailor_context(
                self.conn, self.paths, self.config, job_id, notes=notes or None
            )
        )
        suspicious = findings(payload["posting_md"])
        payload["posting_md"] = wrap_untrusted(payload["posting_md"], "job posting")
        payload["untrusted_notice"] = UNTRUSTED_NOTICE
        if suspicious:
            payload["injection_warnings"] = suspicious
        return payload

    def job_tailor_submit(self, job_id: int, plan: dict) -> dict:
        result = tailor.apply_plan(
            self.conn, self.paths, self.config, job_id,
            TailorPlan.model_validate(plan),
        )
        return _plain(
            {
                "ok": result.ok,
                "error": result.error,
                "job": result.job,
                "report": result.report,
                "diff": result.diff,
                "artifacts": {k: v.path for k, v in result.artifacts.items()},
            }
        )

    def job_glance(self, job_id: int) -> dict | None:
        data = tailor.glance(self.conn, self.paths, self.config, job_id)
        return _plain(data) if data is not None else None

    # Filling: the session authors the mapping.

    def form_extract(self, job_id: int) -> dict:
        result = fill.extract_form(
            self.conn, self.paths, self.config, self.client_factory(), job_id
        )
        return _plain(
            {
                "ok": result.ok,
                "schema": result.schema,
                "blockers": result.blockers,
            }
        )

    def form_map_context(self, job_id: int) -> dict:
        payload = _plain(fill.fill_context(self.conn, self.paths, self.config, job_id))
        payload["untrusted_notice"] = UNTRUSTED_NOTICE
        return payload

    def form_fill(self, job_id: int, mapping: dict) -> dict:
        result = fill.apply_mapping(
            self.conn, self.paths, self.config, self.client_factory(), job_id,
            FieldMapping.model_validate(mapping),
        )
        return _plain(
            {
                "ok": result.ok,
                "error": result.error,
                "job": result.job,
                "applied": result.applied,
                "blockers": result.blockers,
            }
        )

    def job_submit(self, job_id: int) -> dict:
        result = fill.submit_job(
            self.conn, self.paths, self.config, self.client_factory(), job_id
        )
        return _plain(
            {
                "ok": result.ok,
                "error": result.error,
                "job": result.job,
                "blockers": result.blockers,
            }
        )

    # Blockers

    def blocker_list(self, job_id: int | None = None) -> list[dict]:
        return _plain(blockers.list_open(self.conn, job_id))

    def blocker_get(self, blocker_id: int) -> dict:
        blocker = blockers.get_blocker(self.conn, blocker_id)
        payload = _plain(blocker)
        payload["field_spec"] = from_json(blocker.field_spec)
        payload["agreed_action"] = from_json(blocker.agreed_action)
        return payload

    def blocker_answer(
        self, blocker_id: int, actions: list[dict], reusable_fields: list[str] | None = None
    ) -> dict:
        return _plain(
            resolve.answer_blocker(
                self.conn, blocker_id, actions, "owner",
                tuple(reusable_fields or ()),
            )
        )

    def blocker_execute(self, blocker_id: int) -> dict:
        result = resolve.execute_blocker(
            self.conn, self.paths, self.config, self.client_factory(), blocker_id
        )
        return _plain(
            {
                "ok": result.ok,
                "applied": result.applied,
                "failed": result.failed,
                "drift_blocker": result.drift,
                "job": result.finalized_job,
            }
        )

    def blocker_mark_manual_resolved(self, blocker_id: int, note: str = "") -> dict:
        return _plain(
            resolve.mark_manual_resolved(self.conn, blocker_id, "owner", note or None)
        )

    # Tabs

    def tab_focus(self, job_id: int) -> dict:
        self.client_factory().focus(job_id)
        return {"focused": job_id}

    def tab_screenshot(self, job_id: int, selector: str = "") -> Image:
        image = self.client_factory().screenshot(job_id, selector or None)
        return Image(data=image, format="png")

    def tab_replay(self, job_id: int) -> dict:
        result = resolve.replay_tab(
            self.conn, self.paths, self.config, self.client_factory(), job_id, "owner"
        )
        return _plain(
            {
                "ok": result.ok,
                "replayed": result.replayed,
                "failed": result.failed,
                "missing": result.missing,
                "blockers": result.blockers,
            }
        )

    # Shared stores

    def answer_bank_list(self) -> list[dict]:
        entries = answers_module.bank(self.conn).entries
        return [
            {**entry.model_dump(), "answer": "[redacted]"}
            if entry.sensitive
            else entry.model_dump()
            for entry in entries
        ]

    def answer_bank_set(
        self, question: str, answer: str, sensitive: bool = False, scope: str = "global"
    ) -> dict:
        answers_module.set_answer(
            self.conn, question, answer, "owner", sensitive=sensitive, scope=scope
        )
        return {"saved": question}

    def rules_list(self, status: str | None = None) -> list[dict]:
        return _plain(rules_module.list_rules(self.conn, status))

    def rules_propose(
        self, kind: str, scope: str, text: str,
        pattern: str = "", enforcement: str = "soft",
    ) -> dict:
        return _plain(
            rules_module.propose_rule(
                self.conn, kind, scope, text,
                pattern=pattern or None, enforcement=enforcement,
            )
        )

    def rules_set_status(self, rule_id: int, status: str) -> dict:
        return _plain(rules_module.set_status(self.conn, rule_id, status, "owner"))

    def context_get(self, job_id: int) -> list[dict]:
        return _plain(context_module.active(self.conn, job_id))

    def context_append(self, job_id: int, kind: str, content: str, source: str) -> dict:
        return _plain(
            context_module.append(self.conn, self.paths, job_id, kind, content, source)
        )

    def context_supersede(self, job_id: int, old_id: int, new_content: str) -> dict:
        return _plain(
            context_module.supersede(
                self.conn, self.paths, job_id, old_id, new_content, "claude"
            )
        )

    def mail_sync(self) -> dict:
        return mail_module.sync(self.conn, self.paths, self.config, actor="owner")

    def mail_unassigned(self) -> list[dict]:
        result = []
        for mail in mail_module.list_unassigned(self.conn):
            ranked = mail_module.candidates(self.conn, self.paths, mail)
            result.append(
                _plain(
                    {
                        "mail": mail,
                        "body_preview": mail_module.body_text(self.paths, mail, 600),
                        "candidates": [
                            {"job": item["job"], "score": item["score"]}
                            for item in ranked
                        ],
                    }
                )
            )
        return result

    def mail_assign(self, mail_id: int, job_id: int) -> dict:
        return _plain(mail_module.assign(self.conn, self.paths, mail_id, job_id, "owner"))

    def mail_list(self, job_id: int) -> list[dict]:
        return _plain(mail_module.list_for_job(self.conn, job_id))

    def mail_classify_context(self, mail_id: int) -> dict:
        mail = mail_module.get_mail(self.conn, mail_id)
        payload = _plain(
            {
                "message": mail_module.to_message(self.paths, mail),
                "open_jobs": mail_module.open_job_summaries(self.conn),
            }
        )
        body = payload["message"].get("body_text") or ""
        suspicious = findings(body)
        payload["message"]["body_text"] = wrap_untrusted(body, "email message")
        payload["untrusted_notice"] = UNTRUSTED_NOTICE
        if suspicious:
            payload["injection_warnings"] = suspicious
        return payload

    def mail_classify(self, mail_id: int, classification: str) -> dict:
        return _plain(
            mail_module.set_classification(self.conn, mail_id, classification, "owner")
        )

    def credential_set(self, scope: str, username: str, password: str) -> dict:
        credentials.set_credential(self.conn, scope, username, password, "owner")
        return {"saved": scope, "username": username}

    def credential_list(self) -> list[dict]:
        return credentials.list_credentials(self.conn)

    def settings_get(self) -> dict:
        return settings.all_settings(self.conn)

    def settings_set(self, key: str, value: str) -> dict:
        settings.set(self.conn, key, value, "owner")
        return {key: value}


def create_server(
    config: Config | None = None,
    paths: DataPaths | None = None,
    conn=None,
    client_factory=None,
) -> tuple[MCPServer, JobpilotApi]:
    config = config or load_config()
    paths = paths or DataPaths.from_config(config)
    conn = conn or open_db(paths, config)
    if client_factory is None:
        client_factory = lambda: BrowserClient(  # noqa: E731
            f"http://127.0.0.1:{config.browser.daemon_port}"
        )
    api = JobpilotApi(config, paths, conn, client_factory)

    mcp = MCPServer("jobpilot")
    for name in (
        "agenda", "agenda_wait",
        "job_capture", "job_get", "job_list", "job_approve_resume",
        "job_approve_submit", "job_abandon", "job_delete",
        "job_mark_manual_submitted",
        "job_tailor_context", "job_tailor_submit", "job_glance",
        "form_extract", "form_map_context", "form_fill", "job_submit",
        "blocker_list", "blocker_get", "blocker_answer", "blocker_execute",
        "blocker_mark_manual_resolved",
        "tab_focus", "tab_screenshot", "tab_replay",
        "mail_sync", "mail_unassigned", "mail_assign", "mail_list",
        "mail_classify_context", "mail_classify",
        "answer_bank_list", "answer_bank_set",
        "rules_list", "rules_propose", "rules_set_status",
        "context_get", "context_append", "context_supersede",
        "credential_set", "credential_list",
        "settings_get", "settings_set",
    ):
        mcp.tool()(getattr(api, name))
    return mcp, api
