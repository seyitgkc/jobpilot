from jobpilot.config import Config, NotifyConfig
from jobpilot.core import blockers, notify, repo, settings


def make_notifier(config, sent):
    def fake_post(url, content=b"", headers=None, timeout=None):
        sent.append({"url": url, "body": content.decode(), "headers": headers or {}})

    return notify.FallbackNotifier(config, post=fake_post)


def test_fallback_ntfy_sends_with_deep_link():
    config = Config(notify=NotifyConfig(channels=("ntfy",), ntfy_topic="jp-test"))
    sent = []
    notifier = make_notifier(config, sent)

    class FakeBlocker:
        id = 7
        kind = "missing_answer"
        question = "Two fields need your answer"

    notifier.notify_blocker(FakeBlocker())
    assert sent[0]["url"] == "https://ntfy.sh/jp-test"
    assert "Two fields" in sent[0]["body"]
    assert sent[0]["headers"]["Click"].endswith("/blockers/7")


def test_no_topic_sends_nothing():
    config = Config()
    sent = []
    notifier = make_notifier(config, sent)
    notifier._send("title", "body")
    assert sent == []


def test_stale_reminder_once_per_window(conn):
    config = Config(notify=NotifyConfig(channels=("ntfy",), ntfy_topic="jp-test", reminder_hours=1))
    job = repo.create_job(conn, "https://example.com/a", "claude")
    blocker = blockers.raise_blocker(conn, job.id, "captcha", "claude", question="solve it")
    conn.execute(
        "UPDATE blocker SET raised_at = '2026-08-01T00:00:00+00:00' WHERE id = ?",
        (blocker.id,),
    )
    conn.commit()

    sent = []
    notifier = make_notifier(config, sent)
    assert notify.remind_stale_blockers(conn, config, notifier) == 1
    assert "solve it" in sent[0]["body"]
    assert settings.get(conn, "notify.last_reminder") is not None

    assert notify.remind_stale_blockers(conn, config, notifier) == 0
    assert len(sent) == 1


def test_fresh_blockers_not_reminded(conn):
    config = Config(notify=NotifyConfig(channels=("ntfy",), ntfy_topic="jp-test", reminder_hours=6))
    job = repo.create_job(conn, "https://example.com/a", "claude")
    blockers.raise_blocker(conn, job.id, "captcha", "claude", question="fresh")
    sent = []
    assert notify.remind_stale_blockers(conn, config, make_notifier(config, sent)) == 0
    assert sent == []
