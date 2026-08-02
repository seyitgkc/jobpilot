# JobPilot — Job Application Assistant: Build Specification

Status: v1.0 — agreed design, ready for implementation
Audience: Claude Code (implementer). Written as a directive spec; deviations require flagging back to the owner.

---

## 1. Summary

A local-first system that helps the owner apply to jobs. The owner submits job posting URLs; the system archives the posting, tailors the owner's LaTeX resume honestly against the job description, renders a PDF, and fills the application in a **visible (headed) browser**, one tab per application. Anything the system cannot resolve on its own becomes a **blocker**: a screenshot + field spec surfaced to the owner (in-chat via Cowork when available, admin panel otherwise). The owner answers; the system executes the agreed action on their behalf. IMAP polling tracks application progress from confirmation/rejection/interview emails. Everything is recorded in SQLite + local filesystem so every job maps to its exact resume, answers, and timeline.

### Core principles (non-negotiable)

1. **Local-first, durable-state-in-DB.** Conversations (Claude threads) are transport, never the system of record. Every agreement, answer, approval, selector map, and checkpoint is persisted to SQLite the moment it exists. Any session must be resumable cold from the DB.
2. **AI-platform agnostic core.** Exactly three functions require a model (tailoring, mail classification, field mapping). Each sits behind a provider interface with structured input/output. Everything else is deterministic code.
3. **Honesty enforced mechanically.** The resume is a fact base + template, not freeform text. Every tailored bullet must trace to a master fact ID; a validator rejects anything that doesn't.
4. **Human owns the click when it matters.** Two independent approval gates (resume glance, submit approval), each with a global default and per-job override. Default posture: fill-and-park; auto-submit is opt-in.
5. **Read the form, then fill the form.** Never let a model improvise against a live DOM. Extract schema → map answers as data → apply mechanically with a constrained verb set.
6. **Per-job context isolation.** Each application carries its own persisted context (DB rows + `jobs/{job_id}/` files). Work on job A never reads or leaks into job B's context; cross-job knowledge flows only through the explicit shared stores (answer_bank, global rules, resume master). Any session working a job hydrates from that job's context alone.
7. **Global rules, always applied, always current.** A single owner-editable rules store (banned words, banned constructs, "always/never" directives) is injected into every AI call and enforced deterministically where possible. The AI is responsible for keeping rules, contexts, and the answer bank up to date as it learns from resolutions — proposing updates rather than silently mutating owner-authored entries.

### Non-goals (v1)

- No Workday full automation. Workday jobs are **fill-assist-only** (open tab, park, blocker: "manual application required"). Detect and mark at capture time.
- No headless mode. The browser is always visible; the owner may intervene at any time.
- No multi-user support. Single owner, single machine.
- No auto-answering of novel personal/subjective questions. These always become blockers.
- No CAPTCHAs solving. CAPTCHA = blocker, owner solves in the visible tab.

---

## 2. Architecture

```
┌────────────────────────────────────────────────────────────┐
│                     jobpilot (Python package)               │
│  core/        deterministic library — ALL business logic    │
│  providers/   AI provider interface + implementations       │
│  browserd/    browser daemon (owns the single headed        │
│               Chromium context; tab registry)               │
│  mcp/         MCP server — thin wrapper over core           │
│  admin/       FastAPI + HTMX admin panel                    │
│  worker/      scheduler fallback (IMAP poll, retries)       │
└────────────────────────────────────────────────────────────┘
        │                │                  │
   Claude (MCP      Owner (browser     APScheduler / Cowork
   client, incl.    → admin panel)     scheduled tasks
   Cowork)
```

### 2.1 Surfaces share one brain

`core/` contains all logic. `mcp/` and `admin/` are thin adapters over identical core calls — an action taken from chat and the same action taken from the panel execute the same function and write the same rows. No logic lives in the MCP server.

### 2.2 Browser daemon (`browserd`) — the single owner of the browser

**Problem it solves:** a visible browser is one long-lived GUI process, but MCP tools, the admin panel, and the worker all need to touch it. Multiple Playwright launches = multiple browsers.

**Design:**
- A small local daemon (FastAPI on localhost, e.g. `127.0.0.1:8377`) that launches **one persistent Chromium context** (`launch_persistent_context`, `headless=False`, dedicated `user_data_dir` under the data root). Logins to Greenhouse/Lever/etc. persist across applications.
- **Tab registry:** `job_id → page`. One tab per application. Registry is mirrored to DB (`job.tab_state`) so a daemon restart can report which tabs were lost and offer replay.
- **Tab lifecycle:** open at fill-start; stay open while `blocked` or `waiting_approval`; close on `submitted` or `abandoned`. Soft cap **8 open tabs** — beyond it, opening a new tab requires closing/parking an old one (surfaced as a panel action, never silent).
- **Focus policy:** default notify-only. `focus_tab` is called only when a blocker is actively waiting on the owner or when the owner clicks "show me" in the panel/chat.
- **HTTP API (clients: mcp/, admin/, worker/):**
  - `POST /tabs {job_id, url}` → open/navigate
  - `POST /tabs/{job_id}/focus`
  - `GET  /tabs/{job_id}/snapshot` → `{a11y_tree, url, title}` (accessibility snapshot, NOT screenshot, for model consumption)
  - `GET  /tabs/{job_id}/screenshot?selector=` → PNG (full page or cropped to element; for the owner's eyes)
  - `POST /tabs/{job_id}/resolve` `{form_schema}` → selector map (see §5)
  - `POST /tabs/{job_id}/apply` `{actions:[...]}` → constrained verb execution (see §5.4)
  - `GET  /tabs/{job_id}/validate` `{expected_fields}` → drift check before applying deferred answers
  - `DELETE /tabs/{job_id}`
- Everything the daemon does is appended to `event` with `actor='browserd'`.

**Constrained verb set** (the ONLY DOM mutations permitted anywhere in the system):
`set(field_id, value)` · `select(field_id, option)` · `upload(field_id, artifact_path)` · `check(field_id, bool)` · `click_mapped(field_id)` (only for buttons present in the resolved selector map, e.g. "Next", "Submit"). No free-form clicking, no coordinate clicks, no JS injection for input. Anything unmapped ⇒ raise blocker.

**Selectors:** resolve via semantic locators first (`get_by_label`, `get_by_role`, `aria` attributes), CSS path as last resort, and persist the resolved map to `blocker.page_context` / `application.selector_map` so deferred execution reuses it after re-validation.

### 2.3 AI provider interface (`providers/`)

Only three model-backed functions. Each: structured input → structured output (JSON), validated with pydantic, retry-with-repair on parse failure (max 2 repairs, then hard fail → blocker).

```python
class AIProvider(Protocol):
    def tailor(self, facts: FactBase, job_md: str, constraints: TailorConstraints) -> TailorPlan: ...
    def classify_mail(self, msg: MailMessage, open_jobs: list[JobSummary]) -> MailClassification: ...
    def map_fields(self, schema: FormSchema, answers: AnswerBank, job: JobSummary) -> FieldMapping: ...
```

All three calls receive `active_rules(scope)` inside their constraint/context objects — core assembles and injects them; no provider call path exists without rules (§5.8).

Implementations: `AnthropicProvider` (default, `claude-sonnet-4-6` via API key in env) and `NullProvider` (raises → everything becomes a manual blocker; system degrades but functions). Model name, temperature, max_tokens in config, never hardcoded in call sites.

**Note on Cowork-driven flows:** when Claude itself is running the pipeline via MCP tools, the *interactive* judgment (deciding what's a blocker, composing the question) happens in the session — but the three functions above are still invoked through core so their outputs land in the DB identically regardless of who triggered them.

### 2.4 Notification strategy interface

```python
class Notifier(Protocol):
    def notify_blocker(self, blocker: Blocker) -> None
    def notify_status(self, job: Job, msg: str) -> None
```

- `CoworkNotifier`: no-op push (Cowork handles push when Claude asks in-thread); its job is formatting the in-chat blocker presentation contract (§6.3).
- `FallbackNotifier`: ntfy.sh topic and/or SMTP email containing a deep link to `admin/blockers/{id}`.
- Both may be active. Config: `notify.channels = [cowork, ntfy, email]`.

Degraded path must be fully functional: every blocker resolvable from the admin panel alone.

---

## 3. Data model (SQLite)

Single file `data/jobpilot.db`, WAL mode, accessed via a thin repository layer in core (no ORM required; `sqlite3` + dataclasses or SQLModel — implementer's choice, but keep queries readable). All timestamps UTC ISO-8601. All mutations also append to `event`.

```sql
job(
  id INTEGER PK,
  url TEXT NOT NULL,
  canonical_url TEXT,            -- after redirect resolution
  company TEXT, title TEXT,
  board TEXT,                    -- greenhouse|lever|ashby|smartrecruiters|workday|other
  req_id TEXT,                   -- ATS requisition id extracted at capture (mail anchor)
  status TEXT NOT NULL,          -- see state machine §4
  workday_flag INTEGER DEFAULT 0,-- fill-assist-only
  resume_glance_gate TEXT,       -- inherit|on|off  (per-job override)
  submit_gate TEXT,              -- inherit|on|off
  tab_state TEXT,                -- none|open|parked|lost
  created_at, updated_at
)

artifact(
  id INTEGER PK, job_id FK,
  kind TEXT NOT NULL,  -- snapshot_html|snapshot_pdf|posting_md|resume_tex|resume_pdf|
                       -- tailor_plan_json|form_schema_json|filled_screenshot|confirmation_screenshot
  path TEXT NOT NULL,  -- relative to data root
  sha256 TEXT NOT NULL,
  created_at
)

application(
  id INTEGER PK, job_id FK UNIQUE,
  resume_artifact_id FK,          -- exact PDF used
  tailor_plan_artifact_id FK,
  form_schema_artifact_id FK,
  selector_map TEXT,              -- JSON, persisted resolved map
  method TEXT,                    -- adapter:greenhouse|adapter:lever|adapter:ashby|
                                  -- adapter:smartrecruiters|dom_generic|manual
  filled_at, approved_at, submitted_at,
  confirmation_ref TEXT,          -- from confirmation page/mail
  submitted_by TEXT               -- system|owner
)

field_fill(                        -- per-field checkpoint (§5.5)
  id INTEGER PK, application_id FK,
  field_id TEXT, field_label TEXT, field_type TEXT,
  value_redacted TEXT,             -- store value unless field marked sensitive
  source TEXT,                     -- answer_bank|blocker:{id}|derived|owner_manual
  status TEXT,                     -- pending|applied|verified|failed
  applied_at
)

blocker(
  id INTEGER PK, job_id FK,
  kind TEXT NOT NULL,   -- question_personal|question_subjective|missing_answer|captcha|
                        -- login_required|extraction_failed|drift|workday_manual|
                        -- submit_approval|resume_approval|session_expired|other
  question TEXT,                 -- owner-facing question text
  field_spec TEXT,               -- JSON: {field_id,label,type,options,maxlen,required}
  screenshot_artifact_id FK,     -- cropped screenshot scrolled to element
  page_context TEXT,             -- JSON: url, a11y hash, selector map slice
  proposed_action TEXT,          -- JSON action (constrained verb) Claude proposes
  agreed_action TEXT,            -- JSON action after owner confirms/corrects
  answer_reusable INTEGER,       -- owner-flagged: write to answer_bank on resolve
  status TEXT NOT NULL,          -- open|answered|executing|resolved|abandoned
  raised_at, answered_at, resolved_at
)

answer_bank(
  id INTEGER PK,
  question_norm TEXT UNIQUE,     -- normalized question key (lowercase, stripped, stemmed-lite)
  question_raw TEXT,
  answer TEXT,
  answer_type TEXT,              -- text|select|bool|number|date
  sensitive INTEGER DEFAULT 0,   -- if 1: value redacted in field_fill/logs
  scope TEXT DEFAULT 'global',   -- global|company:{name}  (job-specific answers are NOT banked)
  created_from_blocker_id FK NULL,
  updated_at
)

mail(
  id INTEGER PK,
  message_id TEXT UNIQUE, thread_refs TEXT,   -- References/In-Reply-To chain
  sender TEXT, sender_domain TEXT, subject TEXT,
  received_at, body_path TEXT,                -- raw .eml on disk
  classification TEXT,   -- confirmation|rejection|interview|action_required|other|unmatched
  job_id FK NULL,        -- NULL until matched/assigned
  match_score REAL, match_method TEXT,  -- req_id|thread|heuristic|manual
  status TEXT            -- auto_matched|needs_assignment|assigned|ignored
)

event(                            -- append-only audit timeline
  id INTEGER PK, job_id FK NULL,
  ts, actor TEXT,                 -- claude|owner|browserd|worker|admin
  kind TEXT, payload TEXT         -- JSON
)

rule(                             -- global rules store (§5.8)
  id INTEGER PK,
  kind TEXT NOT NULL,   -- banned_word|banned_construct|always|never|style|fact_correction
  scope TEXT DEFAULT 'global',    -- global|tailoring|answers|mail|company:{name}
  text TEXT NOT NULL,             -- the rule itself, owner-readable
  pattern TEXT,                   -- optional regex for deterministic enforcement
  enforcement TEXT NOT NULL,      -- hard (validator rejects) | soft (prompt-injected only)
  source TEXT NOT NULL,           -- owner|ai_proposed
  status TEXT NOT NULL,           -- active|proposed|rejected|retired
  created_at, updated_at
)

job_context(                      -- per-job working memory (§5.9)
  id INTEGER PK, job_id FK,
  kind TEXT NOT NULL,   -- note|decision|owner_pref|constraint|followup|summary
  content TEXT NOT NULL,
  source TEXT,                    -- claude|owner|derived_from_blocker:{id}|mail:{id}
  superseded_by FK NULL,          -- update-by-supersession, never destructive edit
  created_at
)

credential(                       -- per-job/company login drop (§5.10)
  id INTEGER PK,
  scope TEXT NOT NULL,            -- job:{id}|company:{name}|domain:{host}
  username TEXT, password TEXT,   -- plaintext in local DB by design (single machine, local-first);
                                  -- never echoed to chat, panel masks, redacted in event/field_fill
  note TEXT, created_at, last_used_at
)

setting(key TEXT PK, value TEXT)  -- global gates, notify config, caps
```

**Filesystem layout** (`data/` root, path stored relative in `artifact.path`):

```
data/
  jobpilot.db
  browser_profile/               # persistent Chromium user_data_dir
  jobs/{job_id}/
    snapshot.html                # monolith-style single file
    snapshot.pdf                 # print-to-pdf
    posting.md                   # extracted text (what models read)
    resume.tex  resume.pdf
    tailor_plan.json  form_schema.json
    context.md                   # rendered view of job_context rows (regenerated, DB is truth)
    screenshots/…
  mail/{yyyy-mm}/{message_id}.eml
  resume_master/
    facts.yaml                   # fact base (owner-maintained)
    template.tex.j2              # Jinja2 LaTeX template
```

---

## 4. Job state machine

```
captured → tailored → [gate: resume_approval] → filling → filled
        → [gate: submit_approval] → submitting → submitted → (mail-driven:)
          acknowledged → in_process → interview / rejected / offer
Any state may branch to: blocked (returns to prior state on resolve)
Terminal manual paths: manual_required (Workday etc.) → submitted_manually (owner marks)
Owner action: abandoned (from any non-terminal state)
```

Rules:
- Transitions only through `core.state.transition(job, to, actor, payload)` which validates legality, writes `job.status`, appends `event`. No direct status writes anywhere else.
- `blocked` is an overlay: `job.status` keeps the substantive state; open blockers are queried from `blocker`. A job is "blocked" iff it has blockers with `status='open'|'answered'`. (Simplifies resume logic; the UI derives the badge.)
- Gates: if the effective gate is on, entering `filling` (resume gate) or `submitting` (submit gate) requires an `approved_at` stamp; otherwise the system raises a `resume_approval`/`submit_approval` blocker and parks.
- Effective gate = per-job override if not `inherit`, else global `setting`. Defaults: `resume_glance=on`, `submit_approval=on` (fill-and-park posture).

---

## 5. Pipelines

### 5.1 Capture (first action, always — postings vanish)

1. Resolve redirects → `canonical_url`; detect board from URL/DOM fingerprints (greenhouse.io, lever.co, ashbyhq.com, smartrecruiters.com, myworkdayjobs.com → set `workday_flag`).
2. Open in browserd tab, wait `networkidle` + settle delay (ATS pages are JS-hydrated; plain HTTP GET is useless).
3. Save three artifacts: single-file HTML (inline all resources — implement monolith-style inlining in Python: fetch subresources, inline as data URIs; acceptable to shell out to `monolith` binary if available, else fallback to Playwright's serialized DOM + inlined CSS/images), print-to-PDF, and extracted `posting.md` (readability-style extraction → markdown; strip nav/boilerplate).
4. Extract: company, title, **req_id** (board-specific regexes: e.g. Greenhouse `gh_jid`/numeric path id, Lever posting UUID, Ashby job id; keep patterns in `core/boards.py` with the golden-corpus tests, §9).
5. Multi-page postings (description + separate application page): capture both; `posting.md` concatenates with source markers.
6. Insert `job` (status `captured`) + artifacts + events. If `workday_flag`: raise `workday_manual` blocker immediately.

### 5.2 Tailoring (honest by construction)

- **Fact base** `facts.yaml`: every experience bullet, skill, project, education entry as an addressable entry: `{id, kind, text, tags[], dates, weight}`. Owner-maintained; system never writes it.
- **Template** `template.tex.j2`: Jinja2 with LaTeX-safe delimiters (`block_start='\BLOCK{'`, `variable_start='\VAR{'` — standard pattern so braces don't fight).
- **TailorPlan** (provider output, JSON): ordered section layout; per-bullet `{fact_id, rendered_text}`; skills selection; optional summary referencing fact ids used.
- **Honesty validator** (deterministic, in core): every `rendered_text` must trace to its `fact_id`'s master text — enforce (a) no new named entities (orgs, products, certs, numbers) absent from the master fact, via simple NER/regex diff; (b) no date changes; (c) length/paraphrase bound (token overlap or embedding similarity threshold — start with conservative token-overlap ≥ 0.35 with entity check doing the heavy lifting). Violations reject the plan → provider repair round → then blocker with the flagged bullets shown.
- Render: Jinja → `.tex` → `latexmk -pdf` (available: latexmk + pdflatex; **do not** assume tectonic). Compile errors → one auto-repair attempt (provider sees the log) → blocker.
- Persist `tailor_plan.json`, `resume.tex`, `resume.pdf` as artifacts. Status → `tailored`.
- **Resume-glance UI contract** (panel + chat): show (1) PDF link, (2) diff vs master: bullets added/dropped/reordered, per-bullet master-vs-rendered text pairs, (3) validator report. Approval writes `application.approved_at` (resume gate), status advances.

### 5.3 Form extraction ("read before fill")

- **Adapters** for greenhouse, lever, ashby, smartrecruiters: prefer the board's JSON endpoints for the form schema (fields, types, required, options) where available; else parse the known DOM shape. Output: `FormSchema` JSON `{fields:[{field_id,label,type,options?,maxlen?,required,section}] ,submit_button, pagination?}`.
- **Generic DOM fallback** for everything else: build schema from a11y tree (labeled inputs, selects, checkboxes, file inputs). Unlabelable/ambiguous controls → `extraction_failed` blocker BEFORE any typing.
- Persist `form_schema.json`. `browserd /resolve` maps schema fields → live locators; persist selector map.

### 5.4 Field mapping and fill

1. `map_fields(schema, answer_bank, job)` → `FieldMapping`: per field `{value?, source, confidence}` or `{needs_owner: true, reason}`. Resume file field maps to the tailored PDF artifact. Cover-letter-type freeform fields: only if owner has a banked template; else blocker.
2. **Batch blockers:** collect ALL `needs_owner` fields, raise them as one blocker group (one notification, N sub-questions each with cropped screenshot + field spec + proposed action where Claude can propose one). Never ping-pong per field.
3. Apply resolved values via constrained verbs, **checkpoint per field** (`field_fill` row per action: write `pending` → apply → `applied`; read-back verify where feasible → `verified`).
4. On completion: full-page screenshot artifact (`filled_screenshot`), status → `filled`. Submit gate then applies.
5. **Submit:** `click_mapped(submit)` → wait → capture confirmation page screenshot + any confirmation ref → `submitted`. If auto-submit disabled (default), submit only after owner approval (from chat "approve" or panel button — same core call).

### 5.5 Interruption, drift, recovery

- Owner may touch the visible tab at any time. After ANY owner intervention or any deferred execution (answer arrived later): call `/validate` — re-snapshot, confirm each pending field still exists and matches its spec (type/options). Match → proceed. Drift → `drift` blocker with before/after, never blind-fill.
- Session expired / tab lost: reopen from `canonical_url`, replay `verified` field_fills from checkpoints, re-raise only what cannot be replayed (e.g. file re-upload usually replayable; CAPTCHA not). Never re-ask the owner something already answered — answers live in blocker rows and answer_bank.
- Daemon restart: mark open tabs `lost`, surface a panel list with one-click "reopen & replay".

### 5.6 Blocker resolution loop (the owner interaction contract)

Presentation (identical content in chat and panel):
```
[Job: {company} — {title}]  Blocker {n}/{total} in this batch
{cropped screenshot}
Field: {label} ({type}, required={r}, options={...}, maxlen={m})
Question: {owner-facing question}
Proposed: {concrete action, e.g. select "5-7 years"}   [if Claude can propose]
Reusable? [yes → answer_bank / no → this job only]
```
- Owner confirms or corrects → `agreed_action` written → execution is mechanical (verbs only) after `/validate`.
- What the owner approves is an **action**, not prose. If the owner's prose answer doesn't fit the field (e.g. "about five years" vs dropdown), Claude maps and echoes the mapped action for confirmation before executing.
- `answer_reusable=yes` → upsert `answer_bank` keyed by `question_norm`. Job-specific answers (e.g. "why this company") are never banked automatically.
- In Cowork: the ask happens in-thread; push notification is platform-provided. Elsewhere: FallbackNotifier fires with panel deep link. Same rows either way.

### 5.7 IMAP tracking

- Poll via IMAPClient (IDLE if server supports, else interval poll — worker job, or Cowork scheduled task invoking the same core function; both call `core.mail.sync()`).
- Store every message: raw `.eml` on disk + `mail` row. Never delete/move server-side in v1 (read-only IMAP usage; mark-seen optional config).
- **Matching ladder** (stop at first hit):
  1. `req_id` verbatim in subject/body → exact match.
  2. Thread inheritance: `References`/`In-Reply-To` chains to an assigned message → inherit `job_id`.
  3. Heuristic score: sender_domain ∈ known ATS domains (weak signal alone — no-reply@ashbyhq.com serves hundreds of companies) + company token match (strong disambiguator) + title-token overlap + time-window after `submitted_at` (decay). Threshold in config (`mail.match_threshold`, start 0.75). ≥ threshold → `auto_matched`; else `needs_assignment`.
  4. **Unassigned queue** in panel: message + ranked candidate jobs, one-click assign; assignment trains nothing in v1 (no ML), it just resolves and inherits the thread.
- Classification (provider): `confirmation|rejection|interview|action_required|other`. `action_required` (e.g. "complete this assessment") raises a blocker on the matched job. Classifications advance the mail-driven states (§4).

---

### 5.8 Global rules engine

- **What rules are:** owner directives that apply across all jobs. Examples: banned words ("synergy", "rockstar"), banned constructs ("no first-person in bullets", "no passive voice in summary", "never phrase salary as negotiable"), always/never directives ("always attach the PDF, never the .tex", "never answer demographic EEO questions without a blocker", "always use the long-form name of degree X").
- **Two enforcement tiers:**
  - `hard` rules with a `pattern` are enforced deterministically by the honesty/output validators — a tailored bullet containing a banned word is rejected in code, not by asking the model nicely. Hard rules also gate `map_fields` output before any fill.
  - `soft` rules are injected verbatim into every provider call's constraints (`tailor`, `map_fields`, `classify_mail` where relevant) and into the skill's runtime context.
- **Rule injection is unconditional:** core assembles `active_rules(scope)` and passes them into every provider invocation; there is no code path that calls a provider without rules.
- **AI keeps the store current, but doesn't own it:** when Claude notices a recurring correction (owner rephrases the same thing twice, rejects the same construct, resolves the same blocker pattern), it writes a `rule` row with `source='ai_proposed', status='proposed'`. Proposed rules appear in the panel and in-chat for one-tap accept/reject; only `active` rules are enforced/injected. Claude may freely update rules it proposed; owner-authored rules are edited only by the owner. Same supersession discipline applies to answer_bank updates Claude wants to make: propose, don't overwrite.
- Panel: Rules CRUD page with per-rule scope, tier, status, and a "test against corpus" button (run pattern rules over existing tailor plans to preview impact).

### 5.9 Per-job context

- **Isolation:** every pipeline step and every session working a job hydrates exclusively from: that job's rows (job, artifacts, application, field_fills, blockers, mail, job_context) + the shared stores (facts, answer_bank, active rules). Nothing from other jobs enters a prompt or a decision. Core enforces this structurally — context-assembly helpers take a `job_id` and cannot join across jobs.
- **What goes in `job_context`:** owner preferences stated for this job ("emphasize the infra work", "don't mention employer X here"), Claude's working decisions ("chose template variant B because posting stresses leadership"), constraints discovered mid-fill ("form rejects LaTeX ligatures in text fields"), follow-ups ("recruiter said reply by Friday" — from mail), and a rolling `summary` row so a cold session can load one compact digest instead of the full history.
- **Persistence discipline:** anything decided or learned during a session is written as a `job_context` row *at the moment it happens* — same rule as blockers/answers: the thread is transport, the DB is the record. Updates supersede (`superseded_by`), never destructively edit, so the timeline stays auditable.
- **`context.md` on disk** is a regenerated human-readable render of active (non-superseded) rows, kept next to the artifacts so the owner can read a job's full state without the panel. Regenerated on every write; DB remains the source of truth.
- **Currency obligation (skill-enforced):** at the end of any working turn on a job, Claude refreshes the `summary` row if material state changed, and reconciles stale context (e.g. a `followup` that was completed gets superseded). "Everything up to date, all the time" is an explicit skill directive with the DB writes to back it.

### 5.10 Logins: credential drop or manual hand-back (no extra engineering)

When a `login_required` blocker is raised (login wall, account-required ATS, Workday tenant), exactly two resolution paths — both reusing existing machinery:

1. **Credential drop.** Owner supplies username/password via panel form or chat (`credential_set`). Stored in the `credential` table scoped to job, company, or domain (domain scope means the next job at the same tenant reuses it automatically). The login form is treated like any other form: extract its two fields + submit button, fill via constrained verbs, `/validate` after. Passwords are `sensitive` by definition: masked in panel, redacted in `field_fill.value_redacted` and `event`, never echoed back into chat. Plaintext in the local SQLite is accepted by design (single machine, local-first) — no keyring/vault in v1.
2. **Manual hand-back.** Owner clicks into the visible tab, logs in themselves (including any 2FA/email verification), then marks the blocker resolved in panel or chat. Resolution triggers the standard post-intervention path (§5.5): re-snapshot, `/validate`, continue the pipeline from where it parked. Since the persistent context keeps cookies, the login survives for future jobs at the same tenant with zero further owner involvement.

2FA, email-verification links, and account *creation* flows are always path 2 — the system never automates those, it just parks and waits. The rough edges are the owner's by declaration; the system's only obligation is to resume cleanly afterward.

## 6. Surfaces

### 6.1 MCP server (`jobpilot-mcp`)

stdio transport, registered in Claude/Cowork. Thin: every tool = validate args → core call → structured result. Tools (final surface, keep to this):

```
job_capture(url) → job summary + artifacts
job_get(job_id) / job_list(filter)
job_tailor(job_id) → tailor plan summary + validator report + pdf path
job_approve_resume(job_id) / job_request_retailor(job_id, notes)
form_extract(job_id) → schema summary
form_map(job_id) → mapping + list of needs_owner fields
form_fill(job_id) → fill result + checkpoints
blocker_list(job_id?) / blocker_get(id)
blocker_answer(id, agreed_action_json, reusable: bool)
blocker_execute(id)                   # validate-then-apply
job_approve_submit(job_id) / job_submit(job_id)
job_mark_manual_submitted(job_id, note)
tab_focus(job_id) / tab_screenshot(job_id, selector?)
mail_sync() / mail_unassigned() / mail_assign(mail_id, job_id)
answer_bank_list() / answer_bank_set(question, answer, sensitive)
rules_list(scope?) / rules_propose(kind, scope, text, pattern?, enforcement)
rules_set_status(rule_id, status)          # owner-side accept/reject (panel or chat)
context_get(job_id) → active rows + summary
context_append(job_id, kind, content, source)
context_supersede(job_id, old_id, new_content)
credential_set(scope, username, password) / credential_list()   # values masked in output
blocker_mark_manual_resolved(id)         # hand-back after owner logged in / acted in tab
settings_get() / settings_set(key, value)
```

### 6.2 Skill (`jobpilot` skill, markdown)

The procedure and judgment layer; also serves verbatim as a system prompt for non-Claude API loops (portability). Contents: pipeline order; blocker batching rule; presentation contract (§5.6); honesty rules and what to do on validator rejection; what is/ isn't bankable; Workday policy; tone for owner-facing questions; "never re-ask what's in the DB"; "never mutate DOM outside constrained verbs"; **context hygiene**: hydrate only the current job's context + shared stores, write `job_context` rows as decisions happen, refresh the summary before ending a turn; **rules duty**: apply active rules in every output, propose (never silently apply) new rules when patterns recur.

### 6.3 Admin panel (FastAPI + HTMX, no build step)

Pages: Dashboard (jobs by state, open blockers, unassigned mail counts) · Job detail (timeline from `event`, artifacts, resume diff view, field_fill table, tab controls) · Blockers queue (resolve UI identical to chat contract) · Mail (unassigned queue with ranked candidates; per-job mail thread) · Answer bank CRUD (sensitive flag) · Settings (global gates, notify channels, tab cap, match threshold).
Deep links: `/blockers/{id}` (notification target). Localhost only; no auth in v1 (bind 127.0.0.1).

### 6.4 Worker

APScheduler process: mail sync interval, retry queue (failed renders, transient browserd errors), stale-blocker reminders (re-notify after N hours, config). Every worker job is a core function also invocable from MCP — so Cowork scheduled tasks can replace the worker without code changes.

---

## 7. Configuration (`config.toml`)

```toml
[gates]        resume_glance = true; submit_approval = true; auto_submit = false
[browser]      tab_cap = 8; focus_policy = "notify_only"; settle_ms = 1500
[ai]           provider = "anthropic"; model = "claude-sonnet-4-6"
[notify]       channels = ["cowork", "ntfy"]; ntfy_topic = ""; email_to = ""
[mail]         imap_host=""; imap_user=""; imap_pass_env="JOBPILOT_IMAP_PASS"
               poll_seconds = 300; match_threshold = 0.75; mark_seen = false
[validator]    token_overlap_min = 0.35
[paths]        data_root = "./data"
```
Secrets only via env vars referenced from config; never in DB or config values.

---

## 8. Implementation milestones (build in this order; each independently testable)

1. **M1 Foundation:** package layout, config, SQLite schema + migrations (simple numbered SQL files), repository layer, state machine with transition tests, event log, `rule` + `job_context` tables with context-assembly helpers (job-scoped, no cross-job joins) and `context.md` renderer.
2. **M2 Capture:** browserd (launch, tabs, snapshot, screenshot), capture pipeline, board detection + req_id extraction, artifact store. *Deliverable: send URL → three artifacts + job row.*
3. **M3 Resume:** facts.yaml schema + example, Jinja LaTeX template, provider interface + AnthropicProvider.tailor, honesty validator **including hard-rule pattern enforcement**, latexmk render, resume diff data. *Deliverable: job → tailored PDF + validator report.*
4. **M4 Panel core:** dashboard, job detail (incl. context view), resume glance/approve UI, rules CRUD with propose/accept flow, settings. Gates enforced in state machine.
5. **M5 Fill:** adapters (greenhouse, lever, ashby, smartrecruiters) + generic fallback, selector resolution, map_fields, constrained verbs, checkpoints, filled screenshot, submit path with gate.
6. **M6 Blockers:** blocker model end-to-end, batching, screenshot cropping, panel resolve UI, validate-before-apply, drift handling, recovery/replay.
7. **M7 MCP + skill:** full tool surface over core, skill document, Cowork presentation contract.
8. **M8 Mail:** IMAP sync, matching ladder, classification, unassigned queue, mail-driven state advances, worker/scheduled task.
9. **M9 Hardening:** golden-corpus regression suite wired to CI-style local run, notification fallback, daemon-restart recovery, tab cap UX.

## 9. Testing strategy

- **Golden corpus = the capture archive.** Every captured `snapshot.html` is a fixture. Adapter tests replay extraction against saved pages and assert schema stability — board markup changes surface as test failures, not botched applications. Add a `make corpus-check` target.
- Honesty validator: table-driven cases (entity injection, date drift, acceptable paraphrase).
- State machine: exhaustive legal/illegal transition matrix.
- Fill: Playwright against local fixture HTML forms (no network) exercising verbs, checkpoints, drift detection.
- Mail matcher: fixture `.eml` set covering req-id hit, thread inheritance, heuristic scores around threshold, shared-ATS-domain ambiguity.
- Provider calls mocked everywhere except an optional live smoke test behind an env flag.

## 10. Decision log (context for the implementer — do not relitigate silently)

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Fact base + template, not freeform LaTeX rewriting | Mechanical honesty check; deterministic render; no broken TeX |
| 2 | Headed browser, single persistent context, tab per job, daemon owns it | Owner can intervene; logins persist; one owner process avoids multi-launch chaos |
| 3 | Blocker → screenshot+field-spec → agree in chat → system executes | Owner never touches DOM in the happy path ⇒ no staleness handshake; approval is an action, not prose |
| 4 | Batch blockers per form | Avoid N round trips on long forms |
| 5 | Validate-before-apply on any deferred execution | Answers may arrive hours later; page may have drifted |
| 6 | Adapters for big 4 ATS + constrained-verb generic fallback | Fragility isolated to the fallback; adapters testable against golden corpus |
| 7 | Workday = fill-assist-only | Cost/fragility not worth it in v1 |
| 8 | Fill-and-park default; both gates default ON | Filled+reviewed is 95% of value at ~0 risk; submit click is opt-in |
| 9 | req_id as mail anchor; heuristics + manual queue (no plus-addressing available) | ATS sender domains are shared across companies; req_id is near-exact when present |
| 10 | Durable state in SQLite, never in conversation | Platform agnosticism; cold-resume; audit trail |
| 11 | Three provider functions behind interface; MCP as portability layer; skill doubles as system prompt | Paying for Claude/Cowork without lock-in; degraded path stays functional |
| 12 | latexmk/pdflatex (not tectonic) | Present in target environment; no new toolchain |
| 13 | Per-job context rows + regenerated context.md; job-scoped assembly, no cross-job joins | Isolation prevents leakage between applications; cold sessions resume from one digest; DB stays the record |
| 14 | Global rules store: hard (regex-validated) vs soft (prompt-injected) tiers; AI proposes, owner activates | Deterministic enforcement where possible; AI keeps the store current without silently mutating owner intent |
| 15 | Logins: credential drop (login form = just another form) or manual hand-back via existing intervention path; plaintext creds in local DB; 2FA/account-creation always manual | Zero new machinery; owner absorbs the roughness by declaration; persistent context makes each login pay off across future jobs |

## 11. Open items for the owner (answer before/during M3–M8)

1. Provide `facts.yaml` content (I'll convert the existing LaTeX resume) and preferred template style.
2. ntfy topic / email for FallbackNotifier.
3. IMAP host/credentials (env), and whether mark-seen is acceptable.
4. Initial answer_bank seed (work authorization, relocation, salary band, notice period, pronouns/EEO preferences — mark sensitive ones).
5. Tab cap comfortable value; stale-blocker reminder interval.
