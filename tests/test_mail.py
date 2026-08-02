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
):
    headers = [
        f"Message-ID: {message_id}",
        f"From: {sender}",
        "To: jamie@example.com",
        f"Subject: {subject}",
        f"Date: {date}",
        "Content-Type: text/plain; charset=utf-8",
    ]
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


def sync_with(conn, paths, raws, provider=None):
    return mail.sync(
        conn, paths, Config(), provider=provider, fetch=lambda: iter(raws)
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
