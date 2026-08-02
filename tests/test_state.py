from itertools import product

import pytest

from conftest import force_status
from jobpilot.core import events, repo, settings, state

# Expected legality, written out independently of the implementation table.
LEGAL = {
    ("captured", "tailored"),
    ("captured", "manual_required"),
    ("tailored", "filling"),
    ("tailored", "manual_required"),
    ("filling", "filled"),
    ("filling", "manual_required"),
    ("filled", "submitting"),
    ("filled", "filling"),
    ("filled", "manual_required"),
    ("submitting", "submitted"),
    ("submitting", "filled"),
    ("submitted", "acknowledged"),
    ("submitted", "in_process"),
    ("submitted", "interview"),
    ("submitted", "rejected"),
    ("submitted", "offer"),
    ("acknowledged", "in_process"),
    ("acknowledged", "interview"),
    ("acknowledged", "rejected"),
    ("acknowledged", "offer"),
    ("in_process", "interview"),
    ("in_process", "rejected"),
    ("in_process", "offer"),
    ("interview", "in_process"),
    ("interview", "rejected"),
    ("interview", "offer"),
    ("manual_required", "submitted_manually"),
    ("submitted_manually", "acknowledged"),
    ("submitted_manually", "in_process"),
    ("submitted_manually", "interview"),
    ("submitted_manually", "rejected"),
    ("submitted_manually", "offer"),
}

ALL_STATES = sorted(state.STATES)
TERMINAL = {"rejected", "offer", "abandoned"}


def expected_legal(frm, to):
    if to == "abandoned":
        return frm not in TERMINAL
    return (frm, to) in LEGAL


def make_job(conn, status="captured", gates="off"):
    job = repo.create_job(conn, "https://example.com/posting", "claude")
    repo.set_job_gates(
        conn, job.id, "owner", resume_glance_gate=gates, submit_gate=gates
    )
    if status != "captured":
        force_status(conn, job.id, status)
    return repo.get_job(conn, job.id)


def test_state_sets_are_consistent():
    assert state.TERMINAL <= state.STATES
    for targets in state._EDGES.values():
        assert targets <= state.STATES


def test_exhaustive_transition_matrix(conn):
    for frm, to in product(ALL_STATES, ALL_STATES):
        job = make_job(conn, status=frm)
        if expected_legal(frm, to):
            moved = state.transition(conn, job.id, to, "claude")
            assert moved.status == to, f"{frm} -> {to} should succeed"
        else:
            with pytest.raises(state.IllegalTransition):
                state.transition(conn, job.id, to, "claude")
            assert repo.get_job(conn, job.id).status == frm, (
                f"failed {frm} -> {to} must not change status"
            )


def test_unknown_status_rejected():
    with pytest.raises(ValueError):
        state.is_legal("captured", "nonsense")
    with pytest.raises(ValueError):
        state.is_legal("nonsense", "captured")


def test_transition_appends_event(conn):
    job = make_job(conn)
    state.transition(conn, job.id, "tailored", "claude", payload={"note": "done"})
    kinds = [(e.kind, e.payload) for e in events.timeline(conn, job.id)]
    assert any(
        kind == "status_changed" and '"from": "captured"' in payload and '"to": "tailored"' in payload
        for kind, payload in kinds
    )


def test_illegal_transition_appends_no_event(conn):
    job = make_job(conn)
    before = len(events.timeline(conn, job.id))
    with pytest.raises(state.IllegalTransition):
        state.transition(conn, job.id, "submitted", "claude")
    assert len(events.timeline(conn, job.id)) == before


class TestGates:
    def test_resume_gate_blocks_without_approval(self, conn):
        job = make_job(conn, status="tailored", gates="inherit")
        with pytest.raises(state.GateNotSatisfied):
            state.transition(conn, job.id, "filling", "claude")
        assert repo.get_job(conn, job.id).status == "tailored"

    def test_resume_gate_passes_after_approval(self, conn):
        job = make_job(conn, status="tailored", gates="inherit")
        state.approve_resume(conn, job.id, "owner")
        assert state.transition(conn, job.id, "filling", "claude").status == "filling"

    def test_per_job_override_off_skips_gate(self, conn):
        job = make_job(conn, status="tailored", gates="off")
        assert state.transition(conn, job.id, "filling", "claude").status == "filling"

    def test_global_off_inherited(self, conn):
        settings.set(conn, "gates.resume_glance", "off", "owner")
        job = make_job(conn, status="tailored", gates="inherit")
        assert state.transition(conn, job.id, "filling", "claude").status == "filling"

    def test_per_job_on_beats_global_off(self, conn):
        settings.set(conn, "gates.resume_glance", "off", "owner")
        job = make_job(conn, status="tailored", gates="on")
        with pytest.raises(state.GateNotSatisfied):
            state.transition(conn, job.id, "filling", "claude")

    def test_submit_gate_blocks_without_approval(self, conn):
        job = make_job(conn, status="filled", gates="inherit")
        with pytest.raises(state.GateNotSatisfied):
            state.transition(conn, job.id, "submitting", "claude")

    def test_submit_gate_passes_after_approval(self, conn):
        job = make_job(conn, status="filled", gates="inherit")
        state.approve_submit(conn, job.id, "owner")
        assert (
            state.transition(conn, job.id, "submitting", "claude").status == "submitting"
        )

    def test_resume_approval_does_not_satisfy_submit_gate(self, conn):
        job = make_job(conn, status="filled", gates="inherit")
        state.approve_resume(conn, job.id, "owner")
        with pytest.raises(state.GateNotSatisfied):
            state.transition(conn, job.id, "submitting", "claude")

    def test_missing_gate_setting_defaults_on(self, conn):
        conn.execute("DELETE FROM setting WHERE key = 'gates.resume_glance'")
        conn.commit()
        job = make_job(conn, status="tailored", gates="inherit")
        with pytest.raises(state.GateNotSatisfied):
            state.transition(conn, job.id, "filling", "claude")
