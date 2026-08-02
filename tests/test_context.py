import pytest

from jobpilot.core import context, repo


def make_job(conn, url="https://example.com/a", company=None, title=None):
    return repo.create_job(conn, url, "claude", company=company, title=title)


def test_append_and_active(conn, paths):
    job = make_job(conn)
    row = context.append(conn, paths, job.id, "decision", "use template B", "claude")
    assert row.kind == "decision"
    active = context.active(conn, job.id)
    assert [r.id for r in active] == [row.id]


def test_unknown_kind_rejected(conn, paths):
    job = make_job(conn)
    with pytest.raises(ValueError):
        context.append(conn, paths, job.id, "musing", "text", "claude")


def test_unknown_job_rejected(conn, paths):
    with pytest.raises(repo.NotFound):
        context.append(conn, paths, 999, "note", "text", "claude")


def test_supersede_replaces_row(conn, paths):
    job = make_job(conn)
    old = context.append(conn, paths, job.id, "followup", "reply by Friday", "mail:1")
    new = context.supersede(conn, paths, job.id, old.id, "replied", "claude")
    active = context.active(conn, job.id)
    assert [r.id for r in active] == [new.id]
    assert new.kind == "followup"
    with pytest.raises(ValueError):
        context.supersede(conn, paths, job.id, old.id, "again", "claude")


def test_supersede_enforces_job_isolation(conn, paths):
    job_a = make_job(conn, "https://example.com/a")
    job_b = make_job(conn, "https://example.com/b")
    row_a = context.append(conn, paths, job_a.id, "note", "a-note", "claude")
    with pytest.raises(context.IsolationError):
        context.supersede(conn, paths, job_b.id, row_a.id, "hijack", "claude")


def test_set_summary_rolls(conn, paths):
    job = make_job(conn)
    context.set_summary(conn, paths, job.id, "first digest", "claude")
    context.set_summary(conn, paths, job.id, "second digest", "claude")
    summaries = [r for r in context.active(conn, job.id) if r.kind == "summary"]
    assert len(summaries) == 1
    assert summaries[0].content == "second digest"


def test_assemble_is_job_scoped(conn, paths):
    job_a = make_job(conn, "https://example.com/a", company="Acme", title="Platform Engineer")
    job_b = make_job(conn, "https://example.com/b", company="Globex", title="Data Engineer")
    context.append(conn, paths, job_a.id, "note", "ALPHA-ONLY-MARKER", "claude")
    context.append(conn, paths, job_b.id, "note", "BETA-ONLY-MARKER", "claude")
    repo.add_artifact(conn, job_b.id, "posting_md", "jobs/2/posting.md", "deadbeef", "claude")

    bundle = context.assemble(conn, job_a.id)
    assert bundle["job"].id == job_a.id
    assert bundle["artifacts"] == []
    flattened = repr(bundle)
    assert "ALPHA-ONLY-MARKER" in flattened
    assert "BETA-ONLY-MARKER" not in flattened
    assert "Globex" not in flattened


def test_render_md(conn, paths):
    job = make_job(conn, company="Acme", title="Platform Engineer")
    context.append(conn, paths, job.id, "owner_pref", "emphasize infra work", "owner")
    old = context.append(conn, paths, job.id, "note", "stale note", "claude")
    context.supersede(conn, paths, job.id, old.id, "fresh note", "claude")

    rendered = (paths.job_dir(job.id) / "context.md").read_text()
    assert "Job 1: Platform Engineer at Acme" in rendered
    assert "Status: captured" in rendered
    assert "emphasize infra work" in rendered
    assert "fresh note" in rendered
    assert "stale note" not in rendered
