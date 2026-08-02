# jobpilot

A job application assistant for a single owner on a single machine. It archives
job postings, tailors a LaTeX resume against each posting from an
owner-maintained fact base, fills application forms in a visible browser, and
tracks outcomes by reading application email over IMAP. Durable state lives in
SQLite and on the filesystem under `data/`; chat sessions and the admin panel
are frontends over that record.

The full design is in [spec.md](spec.md).

## Layout

- `src/jobpilot/core/` holds all business logic; other packages are thin
  adapters over it.
- `src/jobpilot/migrations/` holds numbered SQL files applied in order at
  startup.
- Runtime data (database, browser profile, artifacts, mail archive) lives
  under `data/`, which stays out of git.

## Running

Start the browser daemon, which owns the single visible Chromium window:

```
jobpilot-browserd
```

Then capture a posting:

```
jobpilot capture https://boards.greenhouse.io/company/jobs/123456
```

Capture archives the rendered page as single-file HTML and PDF, extracts the
posting text to markdown, detects the board, and creates the job row. Workday
postings are marked for manual application and parked behind a blocker.

Tailor a resume against a captured job:

```
jobpilot tailor 1
```

Tailoring reads the fact base at `data/resume_master/facts.yaml`, produces a
plan, validates every rendered line against its master fact (no new
entities, unchanged numbers and dates, bounded paraphrase, owner rules
enforced), renders LaTeX, and compiles the PDF with latexmk. Rejections get
one repair round and then park behind a blocker.

The plan can come from two places. The CLI and worker use the configured AI
provider, which needs an API key. A Claude session driving the system over
MCP is itself the model: it reads the tailoring inputs (`tailor_context`),
authors the plan, and submits it (`apply_plan`); core validates and persists
identically, and no API key is involved on that path.

Fill the application form, then submit it:

```
jobpilot fill 1
```

```
jobpilot submit 1
```

Filling reads the form before touching it: the schema comes from the board's
JSON endpoint when one exists (Greenhouse) or a scan of the rendered page,
selectors resolve through labels and roles with CSS as a last resort, and
required controls that cannot be located park the job before anything is
typed. Mapped answers come from the answer bank and the resume profile; the
only DOM mutations anywhere are the constrained verbs (set, select, check,
upload, click on mapped buttons), each recorded as a per-field checkpoint
with read-back verification. Fields the owner must decide become one batched
blocker while everything resolvable is filled. Both approval gates default
on: filling waits for the resume review and submitting waits for your
approval, so the resting posture is a filled application parked in its tab.
The same two-path split as tailoring applies: `fill_context` plus
`apply_mapping` serve a session authoring the mapping itself, with no API
key.
Building the PDF requires a TeX installation with `latexmk` and `pdflatex` on
PATH; without one the plan and .tex source are still produced.

## Chat surface

The MCP server exposes the whole system to a Claude session:

```
claude mcp add jobpilot -- jobpilot-mcp
```

Every tool is a thin wrapper over the same core calls the panel makes, so an
action taken in chat and the same action taken in the panel write the same
rows. Tailoring and field mapping have context and submit pairs; the session
reads the inputs, authors the plan or mapping itself, and core validates
mechanically, which is why chat-driven runs need no API key. The judgment
layer lives in [skills/jobpilot/SKILL.md](skills/jobpilot/SKILL.md); install
it next to the server so the session follows the pipeline order, the blocker
contract, and the context hygiene rules.

## Panel, worker, and mail

`jobpilot-admin` serves the panel on `127.0.0.1:8378`: dashboard, job pages,
the resume review, the blocker resolve queue, unassigned mail, rules, and
settings. `jobpilot-worker` polls the configured IMAP account on an
interval, matches messages to jobs (requisition id first, then mail threads,
then a scored heuristic with an unassigned queue for the rest), classifies
them, advances application statuses, and sends a summary reminder when
blockers sit unanswered. Notifications go through ntfy or local mail when
configured; every one links into the panel.

## Checks

```
make test
```

```
make corpus-check
```

The corpus check replays form extraction over every archived posting page
and reports drift against the schema each application was actually filled
with, so board markup changes show up before they cost an application.

## Development

Requires Python 3.11 or newer.

```
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/playwright install chromium
.venv/bin/pytest
```

The browser tests use a headless Chromium against a local fixture site and
skip themselves when no Chromium build is installed.

Copy `config.example.toml` to `config.toml` to override defaults. Secrets are
read from environment variables named in the config, never from the file
itself.
