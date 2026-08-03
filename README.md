# jobpilot

A job application assistant for a single owner on a single machine. It archives
job postings, tailors a LaTeX resume against each posting from an
owner-maintained fact base, fills application forms in a visible browser, and
tracks outcomes by reading application email over IMAP. Durable state lives in
SQLite and on the filesystem under `data/`; chat sessions and the admin panel
are frontends over that record.

The full design is in [spec.md](spec.md).

## Setup

Requires Python 3.11 or newer and a TeX distribution for PDF builds.

```
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/playwright install chromium
```

Copy `config.example.toml` to `config.toml` and edit; set `paths.data_root`
to an absolute path so every process finds the same data regardless of where
it starts. Secrets never go in the file: the IMAP password is read from the
environment variable named in `mail.imap_pass_env`, or from the macOS
keychain when that variable is unset.

TeX, if not already installed:

```
brew install --cask basictex
```

```
sudo /Library/TeX/texbin/tlmgr update --self
```

```
sudo /Library/TeX/texbin/tlmgr install latexmk titlesec marvosym enumitem preprint needspace cm-super
```

`cm-super` carries the Type 1 outlines for the T1 encoding the template
uses. Without it pdflatex falls back to METAFONT bitmaps, and the PDF looks
jagged on screen and prints badly while still compiling without error.

The resume master lives in `data/resume_master/`: `facts.yaml` is the fact
base every tailored resume draws from, and an optional `template.tex.j2`
there overrides the built-in layout.

The commands live in the project virtualenv. To run them from any terminal
without activating it, drop shims onto the PATH (adjust the repo path):

```
for name in jobpilot jobpilot-browserd jobpilot-admin jobpilot-worker; do
  printf '#!/bin/sh\nexport JOBPILOT_CONFIG="${JOBPILOT_CONFIG:-%s/config.toml}"\nexec %s/.venv/bin/%s "$@"\n' \
    "$HOME/projects/jobpilot" "$HOME/projects/jobpilot" "$name" > /opt/homebrew/bin/$name
  chmod +x /opt/homebrew/bin/$name
done
```

## Processes

Each process runs independently and none needs the others to stay alive.
They share state through the database, so start only what the moment needs.

```
jobpilot-browserd
```

The browser daemon owns the single visible browser window. Required for
capturing postings and filling forms; everything else works without it. It
drives the Google Chrome installed on this machine (`browser.channel`),
which renders with the real GPU; set the channel to an empty string to use
the Playwright build instead, which renders in software.

Some boards refuse automated sessions outright, and no configuration
changes that. Those applications are filled in by hand: the job page links
to an answer sheet with the tailored resume and every value jobpilot would
have entered, and Mark submitted by hand puts the application back under
mail tracking once it is in.
Under the default `browser.focus_policy = "notify_only"`, the daemon hands
focus back to whatever app you were using whenever launching or opening a
tab pulls Chromium to the front; expect a sub-second flash instead of a
stolen desktop. The tab focus action still brings the window forward on
purpose.

```
jobpilot-admin
```

The panel at `http://127.0.0.1:8378`: capture postings, job pages with
timeline and artifacts, the resume review with approve, the blocker resolve
queue, unassigned mail with assignment, rules with the propose and accept
flow, login details for application sites, and settings.

```
jobpilot-worker
```

Background mail: polls IMAP on the configured interval, matches and
classifies messages, advances application statuses, and sends a summary
reminder when blockers sit unanswered. Chat sessions can run the same sync
on demand, so the worker is optional.

The MCP server (`jobpilot-mcp`) is not started by hand; the chat client
launches it per session.

## Applying

Capture first, always; postings vanish. From the panel's capture form, from
chat by pasting the posting URL, or from the terminal:

```
jobpilot capture https://boards.greenhouse.io/company/jobs/123456
```

Capture archives the rendered page as single-file HTML and PDF, extracts the
posting text, detects the board and requisition id, and creates the job.
Workday postings are marked for manual application and parked behind a
blocker.

```
jobpilot tailor 1
```

Tailoring produces a plan against the fact base, validates every rendered
line mechanically (no new entities, unchanged numbers and dates, bounded
paraphrase, owner rules enforced), renders LaTeX, and compiles the PDF.
Rejections get one repair round and then park behind a blocker. The plan
comes from the configured AI provider (needs `ANTHROPIC_API_KEY`) or from a
chat session authoring it directly, which needs no key.

```
jobpilot fill 1
```

```
jobpilot submit 1
```

Filling reads the form before touching it, resolves selectors, maps answers
from the answer bank and resume profile, applies them with the constrained
verb set under per-field checkpoints, and batches anything needing you into
one blocker. Both approval gates default on: filling waits for the resume
review and submitting waits for your approval, so the resting posture is a
filled application parked in its tab. Forms behind a login use the saved
login details for the job, company, or domain; add them on the panel's
login details page.

```
jobpilot corpus-check
```

Replays form extraction over every archived posting page and reports drift
against the schema each application was filled with, so board markup changes
show up before they cost an application.

## Mail

Set `mail.imap_host` and `mail.imap_user` in `config.toml`. For iCloud:
host `imap.mail.me.com`, user is the primary iCloud address (custom-domain
mail arrives in the same mailbox), and the password is an app-specific
password from account.apple.com. Store it in the keychain so it stays out of
dotfiles and history:

```
security add-generic-password -s jobpilot-imap -a you@icloud.com -w
```

The first sync pulls the last 30 days and then continues incrementally.
IMAP usage is read-only; messages are never marked, moved, or deleted.

## Chat surface

Register the MCP server once (user scope, absolute paths so any session
directory works):

```
claude mcp add --scope user jobpilot -e JOBPILOT_CONFIG=/absolute/path/to/config.toml -- /absolute/path/to/.venv/bin/jobpilot-mcp
```

Install the skill by linking it into the user skills directory:

```
ln -s /absolute/path/to/repo/skills/jobpilot ~/.claude/skills/jobpilot
```

A session can also stand watch: `agenda_wait` blocks until something needs
doing and returns as soon as it does, so a posting captured in the panel is
picked up within seconds without asking twice. Tell a session to watch and
it loops on that call until you stop it.

Every tool is a thin wrapper over the same core calls the panel makes.
Tailoring, field mapping, and mail classification have context and submit
pairs: the session reads the inputs, authors the output itself, and core
validates mechanically, so chat-driven runs need no API key. The skill
carries the judgment layer: pipeline order, the blocker contract, banking
policy, and context hygiene.

Cowork keeps its own connector and skill lists, separate from the two
registrations above, and installs both at once from a plugin package:

```
make plugin
```

The package lands at `dist/jobpilot.plugin`; drop it into a Cowork
conversation and accept the install. It bundles the skill together with the
server config, so jobpilot appears in Cowork's skills and connectors in one
step. The package embeds this checkout's absolute paths; rebuild it if the
repo moves.

## Checks

```
make test
```

```
make corpus-check
```

## Development

Layout: `src/jobpilot/core/` holds all business logic; `browserd/`,
`admin/`, `mcp/`, and `worker/` are thin surfaces over it. Migrations are
numbered SQL files in `src/jobpilot/migrations/` applied at startup. Runtime
data stays under `data/`, out of git. The browser tests use a headless
Chromium against a local fixture site and skip themselves when no Chromium
build is installed; one LaTeX compile test skips without TeX.
