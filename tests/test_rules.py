import pytest

from jobpilot.core import rules


def test_owner_rule_is_active_immediately(conn):
    rule = rules.add_owner_rule(
        conn, "banned_word", "tailoring", "never use the word synergy",
        pattern=r"\bsynergy\b", enforcement="hard",
    )
    assert rule.status == "active"
    assert rule.source == "owner"
    assert [r.id for r in rules.active_rules(conn, "tailoring")] == [rule.id]


def test_proposal_needs_owner_acceptance(conn):
    proposal = rules.propose_rule(
        conn, "style", "answers", "keep answers under three sentences"
    )
    assert proposal.status == "proposed"
    assert rules.active_rules(conn, "answers") == []

    with pytest.raises(rules.RulePermissionError):
        rules.set_status(conn, proposal.id, "active", "claude")

    accepted = rules.set_status(conn, proposal.id, "active", "owner")
    assert accepted.status == "active"
    assert [r.id for r in rules.active_rules(conn, "answers")] == [accepted.id]


def test_owner_rules_locked_to_owner(conn):
    rule = rules.add_owner_rule(conn, "always", "global", "always attach the PDF")
    with pytest.raises(rules.RulePermissionError):
        rules.set_status(conn, rule.id, "retired", "claude")
    with pytest.raises(rules.RulePermissionError):
        rules.update_text(conn, rule.id, "claude", text="changed")
    retired = rules.set_status(conn, rule.id, "retired", "owner")
    assert retired.status == "retired"


def test_claude_may_edit_its_open_proposal(conn):
    proposal = rules.propose_rule(conn, "style", "tailoring", "draft wording")
    updated = rules.update_text(conn, proposal.id, "claude", text="better wording")
    assert updated.text == "better wording"

    rules.set_status(conn, proposal.id, "active", "owner")
    with pytest.raises(rules.RulePermissionError):
        rules.update_text(conn, proposal.id, "claude", text="sneaky edit")


def test_scope_filtering(conn):
    global_rule = rules.add_owner_rule(conn, "never", "global", "never answer EEO silently")
    tailoring_rule = rules.add_owner_rule(
        conn, "banned_word", "tailoring", "no rockstar", pattern=r"\brockstar\b",
        enforcement="hard",
    )
    mail_rule = rules.add_owner_rule(conn, "style", "mail", "plain text replies")

    tailoring_ids = {r.id for r in rules.active_rules(conn, "tailoring")}
    assert tailoring_ids == {global_rule.id, tailoring_rule.id}
    assert mail_rule.id not in tailoring_ids

    global_only = {r.id for r in rules.active_rules(conn)}
    assert global_only == {global_rule.id}


def test_hard_rules_require_pattern(conn):
    with pytest.raises(ValueError):
        rules.add_owner_rule(conn, "banned_word", "global", "no synergy", enforcement="hard")


def test_invalid_pattern_rejected(conn):
    with pytest.raises(Exception):
        rules.add_owner_rule(
            conn, "banned_word", "global", "broken", pattern="(", enforcement="hard"
        )


def test_hard_violations(conn):
    rules.add_owner_rule(
        conn, "banned_word", "tailoring", "never use the word synergy",
        pattern=r"\bsynergy\b", enforcement="hard",
    )
    rules.add_owner_rule(conn, "style", "tailoring", "prefer active voice")

    active = rules.active_rules(conn, "tailoring")
    hits = rules.hard_violations("Drove cross-team Synergy initiatives", active)
    assert len(hits) == 1
    assert hits[0].match.lower() == "synergy"
    assert rules.hard_violations("Led the platform migration", active) == []
