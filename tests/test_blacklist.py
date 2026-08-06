import pytest

from jobpilot.config import Config
from jobpilot.core import blacklist, capture, mail, repo


def test_add_is_idempotent_and_normalises(conn):
    first = blacklist.add(conn, "Genius AI", "owner")
    again = blacklist.add(conn, "genius ai", "owner")
    assert first.id == again.id
    assert first.normalised == "geniusai"
    assert len(blacklist.list_all(conn)) == 1


def test_match_ignores_punctuation_and_suffixes(conn):
    blacklist.add(conn, "Achievers", "owner")
    assert blacklist.match(conn, "Achievers") is not None
    assert blacklist.match(conn, "ACHIEVERS, Inc.") is not None
    # A shorter, different employer is not swept up by the entry.
    assert blacklist.match(conn, "Achiever") is None
    assert blacklist.match(conn, "Xanadu") is None


def test_domain_entries_match_subdomains(conn):
    blacklist.add(conn, "lever.co", "owner", kind="domain")
    assert blacklist.match(conn, None, "https://jobs.lever.co/acme/1") is not None
    assert blacklist.match(conn, None, "https://notlever.co/acme/1") is None


def test_remove_reports_whether_anything_went(conn):
    entry = blacklist.add(conn, "Genius AI", "owner")
    assert blacklist.remove(conn, entry.id, "owner") is True
    assert blacklist.remove(conn, entry.id, "owner") is False
    assert blacklist.list_all(conn) == []


def test_bad_input_is_refused(conn):
    with pytest.raises(ValueError):
        blacklist.add(conn, "   ", "owner")
    with pytest.raises(ValueError):
        blacklist.add(conn, "Acme", "owner", kind="nonsense")


def test_rejections_come_back_with_the_entry(conn, paths):
    raw = (
        b"Message-ID: <r1@example>\r\n"
        b"From: no-reply@greenhouse.io\r\n"
        b"To: owner@example.com\r\n"
        b"Subject: Update on your application to Genius AI\r\n"
        b"Date: Tue, 04 Aug 2026 00:00:00 +0000\r\n\r\n"
        b"We will not be moving forward with your application.\r\n"
    )
    stored = mail.store_message(conn, paths, raw, "worker")
    assert stored is not None
    conn.execute(
        "UPDATE mail SET classification = 'rejection' WHERE id = ?", (stored.id,)
    )
    conn.commit()

    entry = blacklist.add(conn, "Genius AI", "owner")
    found = blacklist.rejections(conn, entry)
    assert [m.subject for m in found] == [
        "Update on your application to Genius AI"
    ]


def test_capture_abandons_a_blacklisted_employer(conn, paths, monkeypatch):
    from test_fill import FakeFillClient

    blacklist.add(conn, "Acme", "owner")
    client = FakeFillClient(html="<html><head><title>Engineer - Acme</title></head></html>")
    result = capture.capture(
        conn, paths, Config(), client, "https://boards.example.com/acme/1"
    )

    assert result.blacklisted is not None
    assert result.blacklisted.pattern == "Acme"
    assert result.job.status == "abandoned"
    assert result.blockers == []
    # The archive is kept: it is the record of why nothing followed.
    assert "snapshot_html" in result.artifacts


def test_capture_leaves_other_employers_alone(conn, paths):
    from test_fill import FakeFillClient

    blacklist.add(conn, "Acme", "owner")
    client = FakeFillClient(html="<html><head><title>Engineer - Globex</title></head></html>")
    result = capture.capture(
        conn, paths, Config(), client, "https://boards.example.com/globex/1"
    )
    assert result.blacklisted is None
    assert result.job.status == "captured"
