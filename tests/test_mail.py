import pytest

from jobpilot.config import Config
from jobpilot.core import blockers, mail, repo, state
from jobpilot.providers.base import ProviderUnavailable
from jobpilot.providers.schemas import MailClassification


def eml(
    message_id="<m1@ats.example>",
    sender="Acme Recruiting <no-reply@greenhouse-mail.io>",
    subject="Thank you for applying to Acme",
    body="We received your application for Platform Engineer.",
    references=None,
    date="Mon, 03 Aug 2026 10:00:00 +0000",
    to="jamie@example.com",
    cc=None,
):
    headers = [
        f"Message-ID: {message_id}",
        f"From: {sender}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Date: {date}",
        "Content-Type: text/plain; charset=utf-8",
    ]
    if cc:
        headers.append(f"Cc: {cc}")
    if references:
        headers.append(f"References: {references}")
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode()


def make_job(conn, *, company="Acme", title="Platform Engineer", req_id=None, submitted=True):
    job = repo.create_job(
        conn, "https://boards.greenhouse.io/acme/jobs/1", "claude",
        company=company, title=title, board="greenhouse", req_id=req_id,
    )
    if submitted:
        for status in ("tailored", "filling", "filled", "submitting", "submitted"):
            conn.execute("UPDATE job SET status = ? WHERE id = ?", (status, job.id))
        conn.commit()
        repo.mark_submitted(conn, job.id, "claude", submitted_by="system")
        conn.execute(
            "UPDATE application SET submitted_at = '2026-08-01T00:00:00+00:00'"
            " WHERE job_id = ?",
            (job.id,),
        )
        conn.commit()
    return repo.get_job(conn, job.id)


def sync_with(conn, paths, raws, provider=None, config=None):
    return mail.sync(
        conn, paths, config or Config(), provider=provider, fetch=lambda: iter(raws)
    )


def test_store_parses_and_dedupes(conn, paths):
    result = sync_with(conn, paths, [eml(), eml()])
    assert result["fetched"] == 2
    assert result["stored"] == 1
    rows = mail.list_unassigned(conn)
    assert len(rows) == 0 or rows  # matched or queued depending on jobs
    row = mail.get_mail(conn, 1)
    assert row.message_id == "m1@ats.example"
    assert row.sender_domain == "greenhouse-mail.io"
    assert paths.absolute(row.body_path).exists()
    assert "Platform Engineer" in mail.body_text(paths, row)


def test_req_id_match_wins(conn, paths):
    job = make_job(conn, req_id="4567890")
    make_job(conn, company="Globex", title="Data Engineer")
    sync_with(conn, paths, [eml(subject="Your application 4567890 at Acme")])
    row = mail.get_mail(conn, 1)
    assert row.job_id == job.id
    assert row.match_method == "req_id"
    assert row.status == "auto_matched"


def test_thread_inheritance(conn, paths):
    job = make_job(conn)
    sync_with(conn, paths, [eml(subject="Application received at Acme for Platform Engineer")])
    first = mail.get_mail(conn, 1)
    assert first.job_id == job.id

    sync_with(
        conn, paths,
        [
            eml(
                message_id="<m2@ats.example>",
                sender="Recruiter <person@totally-new-domain.example>",
                subject="Re: next steps",
                body="Quick follow up.",
                references="<m1@ats.example>",
            )
        ],
    )
    second = mail.get_mail(conn, 2)
    assert second.job_id == job.id
    assert second.match_method == "thread"


def test_heuristic_threshold_and_ambiguity(conn, paths):
    acme = make_job(conn)
    make_job(conn, company="Globex", title="Data Engineer")

    sync_with(
        conn, paths,
        [eml(subject="Update on your Platform Engineer application at Acme")],
    )
    matched = mail.get_mail(conn, 1)
    assert matched.job_id == acme.id
    assert matched.match_method == "heuristic"
    assert matched.match_score >= 0.75

    # Shared ATS domain with no company or title signal stays unassigned.
    sync_with(
        conn, paths,
        [
            eml(
                message_id="<m9@ats.example>",
                subject="Update on your application",
                body="There is news about a role you applied to.",
            )
        ],
    )
    ambiguous = mail.get_mail(conn, 2)
    assert ambiguous.job_id is None
    assert ambiguous.status == "needs_assignment"
    ranked = mail.candidates(conn, paths, ambiguous)
    assert ranked and all(item["score"] < 0.75 for item in ranked)


def test_manual_assign_advances_state(conn, paths):
    job = make_job(conn)
    sync_with(conn, paths, [eml(subject="News", body="No obvious signals here.")])
    row = mail.get_mail(conn, 1)
    assert row.status == "needs_assignment"

    mail.set_classification(conn, row.id, "confirmation", "owner")
    mail.assign(conn, paths, row.id, job.id, "owner")
    assert mail.get_mail(conn, 1).status == "assigned"
    assert repo.get_job(conn, job.id).status == "acknowledged"


class ClassifyProvider:
    def __init__(self, classification):
        self.classification = classification

    def classify_mail(self, msg, open_jobs):
        return MailClassification(classification=self.classification, confidence=0.9)


@pytest.mark.parametrize(
    "classification,expected_status",
    [("confirmation", "acknowledged"), ("interview", "interview"), ("rejection", "rejected")],
)
def test_classification_advances(conn, paths, classification, expected_status):
    job = make_job(conn)
    sync_with(
        conn, paths,
        [eml(subject="Update on your Platform Engineer application at Acme")],
        provider=ClassifyProvider(classification),
    )
    assert repo.get_job(conn, job.id).status == expected_status


def test_action_required_raises_blocker(conn, paths):
    job = make_job(conn)
    sync_with(
        conn, paths,
        [eml(subject="Complete your Acme Platform Engineer assessment")],
        provider=ClassifyProvider("action_required"),
    )
    open_blockers = blockers.list_open(conn, job.id)
    assert len(open_blockers) == 1
    assert "assessment" in open_blockers[0].question
    assert repo.get_job(conn, job.id).status == "submitted"


def test_illegal_advance_becomes_note(conn, paths):
    job = make_job(conn)
    state.transition(conn, job.id, "rejected", "worker")
    sync_with(
        conn, paths,
        [eml(subject="Interview for Platform Engineer at Acme")],
        provider=ClassifyProvider("interview"),
    )
    assert repo.get_job(conn, job.id).status == "rejected"
    from jobpilot.core import events

    kinds = [event.kind for event in events.timeline(conn, job.id)]
    assert "mail_noted" in kinds


def test_provider_unavailable_leaves_unclassified(conn, paths):
    class Down:
        def classify_mail(self, msg, open_jobs):
            raise ProviderUnavailable("no key")

    make_job(conn)
    result = sync_with(
        conn, paths,
        [eml(subject="Update on your Platform Engineer application at Acme")],
        provider=Down(),
    )
    assert result["classified"] == 0
    assert mail.get_mail(conn, 1).classification is None


def test_bad_classification_rejected(conn, paths):
    sync_with(conn, paths, [eml()])
    with pytest.raises(ValueError):
        mail.set_classification(conn, 1, "spam", "owner")


def test_classification_skips_irrelevant_mail(conn, paths):
    calls = []

    class CountingProvider:
        def classify_mail(self, msg, open_jobs):
            calls.append(msg.subject)
            from jobpilot.providers.schemas import MailClassification

            return MailClassification(classification="other", confidence=0.5)

    newsletter = eml(
        subject="Weekend deals on flights",
        sender="Deals <promo@travel-deals.example>",
        message_id="<promo1@x>",
    )
    application = eml(
        subject="Thank you for applying",
        message_id="<app1@x>",
    )
    sync_with(conn, paths, [newsletter, application], provider=CountingProvider())
    assert calls == ["Thank you for applying"]

    promo = mail.get_mail(conn, 1)
    assert promo.classification is None


def test_likely_relevant_signals(conn, paths):
    make_job(conn, company="Initech Systems")
    sync_with(
        conn, paths,
        [
            eml(subject="Lunch on Sunday?", sender="Sam <sam@family.example>",
                message_id="<p1@x>"),
            eml(subject="Update from Initech Systems",
                sender="News <news@corporate.example>", message_id="<p2@x>"),
        ],
    )
    personal = mail.get_mail(conn, 1)
    company_mail = mail.get_mail(conn, 2)
    assert not mail.likely_relevant(conn, personal, "see you there")
    assert mail.likely_relevant(conn, company_mail, "")


def test_outcome_mail_on_presubmit_job_raises_reconcile_blocker(conn, paths):
    from jobpilot.core import blockers

    job = make_job(conn, req_id="4567890", submitted=False)
    conn.execute("UPDATE job SET status = 'filling' WHERE id = ?", (job.id,))
    conn.commit()

    provider = ClassifyProvider("confirmation")
    sync_with(
        conn, paths,
        [eml(subject="Thank you for applying, ref 4567890", message_id="<c1@x>")],
        provider=provider,
    )
    assert repo.get_job(conn, job.id).status == "filling"
    open_blockers = blockers.list_open(conn, job.id)
    assert len(open_blockers) == 1
    assert "submitted outside jobpilot" in open_blockers[0].question

    # A second outcome message does not stack another blocker.
    sync_with(
        conn, paths,
        [eml(subject="Interview for 4567890", message_id="<c2@x>")],
        provider=ClassifyProvider("interview"),
    )
    assert len(blockers.list_open(conn, job.id)) == 1


def test_mark_submitted_manually_reconciles(conn, paths):
    from jobpilot.core import blockers, resolve

    job = make_job(conn, req_id="4567890", submitted=False)
    conn.execute("UPDATE job SET status = 'filling' WHERE id = ?", (job.id,))
    conn.commit()
    sync_with(
        conn, paths,
        [eml(subject="Thank you for applying, ref 4567890", message_id="<c1@x>")],
        provider=ClassifyProvider("confirmation"),
    )

    reconciled = resolve.mark_submitted_manually(conn, job.id, "owner")
    assert reconciled.status == "acknowledged"
    application = repo.get_application(conn, job.id)
    assert application.submitted_at is not None
    assert application.submitted_by == "owner"
    assert blockers.list_open(conn, job.id) == []


def test_tracked_address_filter_blocks_storage(conn, paths):
    from jobpilot.config import MailConfig

    config = Config(mail=MailConfig(tracked_address="contact@seyitgokce.me"))
    result = sync_with(
        conn, paths,
        [
            eml(message_id="<other@x>", to="Seyit <seyitahmetgokce@icloud.com>"),
            eml(message_id="<mine@x>", to="Seyit <contact@seyitgokce.me>"),
            eml(message_id="<cc@x>", to="team@agency.example",
                cc="contact@seyitgokce.me"),
        ],
        config=config,
    )
    assert result["fetched"] == 3
    assert result["stored"] == 2
    ids = [row.message_id for row in mail.list_unassigned(conn)]
    assert "other@x" not in ids
    assert set(ids) == {"mine@x", "cc@x"}
    stored_files = list(paths.mail_dir.rglob("*.eml"))
    assert len(stored_files) == 2
    assert not any("other" in file.name for file in stored_files)


def test_tracking_window_hides_and_skips_older_mail(conn, paths):
    assert mail.tracking_start(conn) == mail.TRACKING_START_DEFAULT

    calls = []

    class CountingProvider:
        def classify_mail(self, msg, open_jobs):
            calls.append(msg.subject)
            return MailClassification(classification="other", confidence=0.5)

    sync_with(
        conn, paths,
        [
            eml(message_id="<old@x>", subject="Your application from July",
                date="Mon, 20 Jul 2026 10:00:00 +0000"),
            eml(message_id="<new@x>", subject="Your application from August"),
        ],
        provider=CountingProvider(),
    )
    assert calls == ["Your application from August"]
    queue_ids = [row.message_id for row in mail.list_unassigned(conn)]
    assert queue_ids == ["new@x"]

    from jobpilot.core import agenda

    to_classify = agenda.agenda(conn)["mail_to_classify"]
    assert all("July" not in (entry["subject"] or "") for entry in to_classify)

    # The archive keeps the older message even though no surface lists it.
    assert mail.get_mail(conn, 1).message_id == "old@x"


def test_imap_password_prefers_env(monkeypatch):
    from jobpilot.config import Config, MailConfig

    config = Config(mail=MailConfig(imap_user="a@b.c"))
    monkeypatch.setenv("JOBPILOT_IMAP_PASS", "from-env")
    assert mail.imap_password(config) == "from-env"


def test_imap_password_falls_back_to_keychain(monkeypatch):
    import subprocess
    import sys

    from jobpilot.config import Config, MailConfig

    config = Config(mail=MailConfig(imap_user="a@b.c"))
    monkeypatch.delenv("JOBPILOT_IMAP_PASS", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")

    calls = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=15):
        calls["cmd"] = cmd

        class Result:
            returncode = 0
            stdout = "from-keychain\n"

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mail.imap_password(config) == "from-keychain"
    assert mail.KEYCHAIN_SERVICE in calls["cmd"]
    assert "a@b.c" in calls["cmd"]


def test_imap_password_missing_raises(monkeypatch):
    import subprocess

    from jobpilot.config import Config, MailConfig

    config = Config(mail=MailConfig(imap_user="a@b.c"))
    monkeypatch.delenv("JOBPILOT_IMAP_PASS", raising=False)

    def fake_run(cmd, capture_output=True, text=True, timeout=15):
        class Result:
            returncode = 44
            stdout = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as excinfo:
        mail.imap_password(config)
    assert "security add-generic-password" in str(excinfo.value)


def test_a_rejection_for_another_level_does_not_claim_the_job(conn, paths):
    # One employer, two openings. The mail answers the Staff application; the
    # tracked job is the Intermediate one. Company, sender and timing all
    # agree, so only the level separates them.
    job = make_job(conn, company="Achievers", title="Intermediate Software Engineer")
    stored = mail.store_message(
        conn, paths,
        eml(
            message_id="<staff@hire.lever.co>",
            sender="Achievers <no-reply@hire.lever.co>",
            subject="Thanks for your interest in Achievers, Seyit",
            body=(
                "Thank you for submitting your application to Achievers for the "
                "Staff Software Engineer role. After reviewing your application, "
                "we've made the decision to not move forward at this time."
            ),
        ),
        "worker",
    )
    score = mail.score_job(conn, stored, mail.body_text(paths, stored), job)
    assert score < Config().mail.match_threshold, score


def test_the_matching_level_still_scores_a_confident_match(conn, paths):
    job = make_job(conn, company="Achievers", title="Intermediate Software Engineer")
    stored = mail.store_message(
        conn, paths,
        eml(
            message_id="<intermediate@hire.lever.co>",
            sender="Achievers <no-reply@hire.lever.co>",
            subject="Thanks for your interest in Achievers, Seyit",
            body=(
                "Thank you for submitting your application to Achievers for the "
                "Intermediate Software Engineer role."
            ),
        ),
        "worker",
    )
    score = mail.score_job(conn, stored, mail.body_text(paths, stored), job)
    assert score >= Config().mail.match_threshold, score


def test_a_title_with_no_level_word_is_unaffected(conn, paths):
    job = make_job(conn, company="Acme", title="Platform Engineer")
    stored = mail.store_message(conn, paths, eml(), "worker")
    score = mail.score_job(conn, stored, mail.body_text(paths, stored), job)
    assert score >= Config().mail.match_threshold, score
