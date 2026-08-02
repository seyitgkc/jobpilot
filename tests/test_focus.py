import asyncio

from jobpilot.browserd import focus

LSAPPINFO_OUTPUT = '"CFBundleIdentifier"="com.apple.Terminal"\n'


def test_parse_front_bundle():
    assert focus.parse_front(LSAPPINFO_OUTPUT) == "com.apple.Terminal"
    assert focus.parse_front('CFBundleIdentifier = "org.chromium.Chromium"') == (
        "org.chromium.Chromium"
    )
    assert focus.parse_front("no bundle here") is None


def test_restore_skips_pointless_cases(monkeypatch):
    calls = []

    async def fake_front():
        return "com.apple.Terminal"

    monkeypatch.setattr(focus, "front_bundle", fake_front)

    # Nothing to restore to, or the browser itself was frontmost before.
    assert asyncio.run(focus.restore(None, delay=0)) is False
    assert asyncio.run(focus.restore("org.chromium.Chromium", delay=0)) is False

    # The owner's app is still frontmost; no reactivation needed.
    assert asyncio.run(focus.restore("com.apple.Terminal", delay=0)) is False
    assert calls == []


def test_restore_reactivates_when_browser_took_over(monkeypatch):
    launched = []

    async def fake_front():
        return "org.chromium.Chromium"

    class FakeProcess:
        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kwargs):
        launched.append(cmd)
        return FakeProcess()

    monkeypatch.setattr(focus, "front_bundle", fake_front)
    monkeypatch.setattr(focus.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(focus.sys, "platform", "darwin")

    assert asyncio.run(focus.restore("com.apple.Terminal", delay=0)) is True
    assert launched == [("open", "-b", "com.apple.Terminal")]
