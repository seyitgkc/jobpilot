-- Full schema. All timestamps are UTC ISO-8601 TEXT. Status/kind columns are
-- TEXT with the allowed values noted; core validates them (SQLite CHECKs are
-- avoided so value sets can evolve by migration-free code change).

CREATE TABLE job(
  id INTEGER PRIMARY KEY,
  url TEXT NOT NULL,
  canonical_url TEXT,                        -- after redirect resolution
  company TEXT,
  title TEXT,
  board TEXT,                                -- greenhouse|lever|ashby|smartrecruiters|workday|other
  req_id TEXT,                               -- ATS requisition id, mail anchor
  status TEXT NOT NULL,                      -- state machine values, see core/state.py
  workday_flag INTEGER NOT NULL DEFAULT 0,   -- fill-assist-only
  resume_glance_gate TEXT NOT NULL DEFAULT 'inherit',  -- inherit|on|off
  submit_gate TEXT NOT NULL DEFAULT 'inherit',         -- inherit|on|off
  tab_state TEXT NOT NULL DEFAULT 'none',    -- none|open|parked|lost
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX job_status_idx ON job(status);

CREATE TABLE artifact(
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES job(id),
  kind TEXT NOT NULL,   -- snapshot_html|snapshot_pdf|posting_md|resume_tex|resume_pdf|
                        -- tailor_plan_json|form_schema_json|filled_screenshot|confirmation_screenshot
  path TEXT NOT NULL,   -- relative to data root
  sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX artifact_job_idx ON artifact(job_id);

CREATE TABLE application(
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL UNIQUE REFERENCES job(id),
  resume_artifact_id INTEGER REFERENCES artifact(id),
  tailor_plan_artifact_id INTEGER REFERENCES artifact(id),
  form_schema_artifact_id INTEGER REFERENCES artifact(id),
  selector_map TEXT,             -- JSON, persisted resolved map
  method TEXT,                   -- adapter:greenhouse|adapter:lever|adapter:ashby|
                                 -- adapter:smartrecruiters|dom_generic|manual
  filled_at TEXT,
  resume_approved_at TEXT,       -- resume glance gate stamp
  submit_approved_at TEXT,       -- submit approval gate stamp
  submitted_at TEXT,
  confirmation_ref TEXT,
  submitted_by TEXT              -- system|owner
);

CREATE TABLE field_fill(
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  field_id TEXT NOT NULL,
  field_label TEXT,
  field_type TEXT,
  value_redacted TEXT,           -- value unless field marked sensitive
  source TEXT,                   -- answer_bank|blocker:{id}|derived|owner_manual
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|verified|failed
  applied_at TEXT
);
CREATE INDEX field_fill_application_idx ON field_fill(application_id);

CREATE TABLE blocker(
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES job(id),
  kind TEXT NOT NULL,   -- question_personal|question_subjective|missing_answer|captcha|
                        -- login_required|extraction_failed|drift|workday_manual|
                        -- submit_approval|resume_approval|session_expired|other
  question TEXT,
  field_spec TEXT,               -- JSON: field_id, label, type, options, maxlen, required
  screenshot_artifact_id INTEGER REFERENCES artifact(id),
  page_context TEXT,             -- JSON: url, a11y hash, selector map slice
  proposed_action TEXT,          -- JSON constrained-verb action
  agreed_action TEXT,            -- JSON action after owner confirms or corrects
  answer_reusable INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open',  -- open|answered|executing|resolved|abandoned
  raised_at TEXT NOT NULL,
  answered_at TEXT,
  resolved_at TEXT
);
CREATE INDEX blocker_job_idx ON blocker(job_id);
CREATE INDEX blocker_status_idx ON blocker(status);

CREATE TABLE answer_bank(
  id INTEGER PRIMARY KEY,
  question_norm TEXT NOT NULL UNIQUE,   -- normalized question key
  question_raw TEXT,
  answer TEXT,
  answer_type TEXT NOT NULL DEFAULT 'text',  -- text|select|bool|number|date
  sensitive INTEGER NOT NULL DEFAULT 0,      -- if 1: value redacted in field_fill/logs
  scope TEXT NOT NULL DEFAULT 'global',      -- global|company:{name}
  created_from_blocker_id INTEGER REFERENCES blocker(id),
  updated_at TEXT NOT NULL
);

CREATE TABLE mail(
  id INTEGER PRIMARY KEY,
  message_id TEXT UNIQUE,
  thread_refs TEXT,              -- References/In-Reply-To chain
  sender TEXT,
  sender_domain TEXT,
  subject TEXT,
  received_at TEXT,
  body_path TEXT,                -- raw .eml on disk, relative to data root
  classification TEXT,           -- confirmation|rejection|interview|action_required|other|unmatched
  job_id INTEGER REFERENCES job(id),
  match_score REAL,
  match_method TEXT,             -- req_id|thread|heuristic|manual
  status TEXT                    -- auto_matched|needs_assignment|assigned|ignored
);
CREATE INDEX mail_job_idx ON mail(job_id);
CREATE INDEX mail_status_idx ON mail(status);

-- Append-only audit timeline.
CREATE TABLE event(
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES job(id),
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,           -- claude|owner|browserd|worker|admin
  kind TEXT NOT NULL,
  payload TEXT                   -- JSON
);
CREATE INDEX event_job_idx ON event(job_id);

CREATE TABLE rule(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,   -- banned_word|banned_construct|always|never|style|fact_correction
  scope TEXT NOT NULL DEFAULT 'global',  -- global|tailoring|answers|mail|company:{name}
  text TEXT NOT NULL,            -- the rule itself, owner-readable
  pattern TEXT,                  -- regex for deterministic enforcement
  enforcement TEXT NOT NULL,     -- hard (validator rejects) | soft (prompt-injected only)
  source TEXT NOT NULL,          -- owner|ai_proposed
  status TEXT NOT NULL,          -- active|proposed|rejected|retired
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX rule_status_scope_idx ON rule(status, scope);

-- Per-job working memory. Rows supersede, never destructively edit.
CREATE TABLE job_context(
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES job(id),
  kind TEXT NOT NULL,   -- note|decision|owner_pref|constraint|followup|summary
  content TEXT NOT NULL,
  source TEXT,          -- claude|owner|derived_from_blocker:{id}|mail:{id}
  superseded_by INTEGER REFERENCES job_context(id),
  created_at TEXT NOT NULL
);
CREATE INDEX job_context_job_idx ON job_context(job_id);

-- Plaintext by design: single machine, local-first. Values are masked in the
-- panel and redacted in event/field_fill; never echoed to chat.
CREATE TABLE credential(
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL,           -- job:{id}|company:{name}|domain:{host}
  username TEXT,
  password TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  last_used_at TEXT
);

CREATE TABLE setting(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
