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

Tailoring reads the fact base at `data/resume_master/facts.yaml`, asks the
configured AI provider for a plan, validates every rendered line against its
master fact (no new entities, unchanged numbers and dates, bounded
paraphrase, owner rules enforced), renders LaTeX, and compiles the PDF with
latexmk. Rejections get one repair round and then park behind a blocker.
Building the PDF requires a TeX installation with `latexmk` and `pdflatex` on
PATH; without one the plan and .tex source are still produced.

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
