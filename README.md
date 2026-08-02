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

## Development

Requires Python 3.11 or newer.

```
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Copy `config.example.toml` to `config.toml` to override defaults. Secrets are
read from environment variables named in the config, never from the file
itself.
