"""Owner-approved growth of the fact base.

Tailoring may only say what the fact base says. When a posting asks for
something the owner may well have but never wrote down, the session does not
paraphrase around the gap and does not invent: it proposes the missing fact
and the owner decides, per proposal, between

- master: add it to facts.yaml, usable by every future resume,
- job: add it to this job's resume only (data/jobs/<id>/facts_extra.yaml),
- skip: leave it out.

add_fact() is the only way a fact gets written, and it requires the owner's
decision in their own words; that text is kept on the event timeline. When
the owner is not in the conversation, ask_gaps() parks every proposal for a
job as one fact_gap blocker, and resolve_gaps() applies the answer later.
"""

from __future__ import annotations

import sqlite3

from pydantic import ValidationError

from ..config import DataPaths
from ..providers.schemas import Fact
from ..util import from_json
from . import blockers, events, repo, resume
from .models import Blocker

DECISIONS = ("master", "job", "skip")


def _owner_text(owner_answer: str) -> str:
    text = (owner_answer or "").strip()
    if not text:
        raise ValueError(
            "owner_answer is required: ask the owner first and pass their "
            "decision in their own words"
        )
    return text


def _parse_fact(fact: dict) -> Fact:
    try:
        return Fact.model_validate(fact)
    except ValidationError as error:
        raise ValueError(f"invalid fact: {error}") from error


def add_fact(
    conn: sqlite3.Connection,
    paths: DataPaths,
    job_id: int,
    fact: dict,
    scope: str,
    owner_answer: str,
    actor: str = "claude",
) -> dict:
    """Write one fact the owner agreed to. scope is "master" (facts.yaml,
    all future resumes) or "job" (this job's resume only)."""
    answer = _owner_text(owner_answer)
    if scope not in resume.FACT_SCOPES:
        raise ValueError(f"scope must be one of {resume.FACT_SCOPES}, got {scope!r}")
    repo.get_job(conn, job_id)
    parsed = _parse_fact(fact)
    resume.add_fact(paths, parsed, scope, job_id)
    events.append(
        conn,
        actor=actor,
        kind="fact_added",
        job_id=job_id,
        payload={
            "scope": scope,
            "fact": parsed.model_dump(exclude_defaults=True),
            "owner_answer": answer,
        },
    )
    conn.commit()
    return {"fact": parsed.model_dump(exclude_defaults=True), "scope": scope}


def ask_gaps(
    conn: sqlite3.Connection,
    paths: DataPaths,
    job_id: int,
    proposals: list[dict],
    actor: str = "claude",
) -> Blocker:
    """Park every proposed fact for one job as a single fact_gap blocker.
    Each proposal is {"fact": {...}, "posting_ask": "...",
    "suggested_scope": "master"|"job"}. Facts are checked against the fact
    base now (ids, parents) so the owner only ever sees writable proposals."""
    if not proposals:
        raise ValueError("no proposals given")
    base = resume.load_facts(paths, job_id)
    known_ids = {fact.id for fact in base.facts}
    cleaned = []
    for index, proposal in enumerate(proposals):
        fact = _parse_fact(proposal.get("fact") or {})
        if fact.id in known_ids:
            raise ValueError(f"proposal {index}: fact id {fact.id!r} already exists")
        if fact.parent is not None and fact.parent not in known_ids:
            raise ValueError(
                f"proposal {index}: parent {fact.parent!r} is not in the fact base"
            )
        scope = proposal.get("suggested_scope") or "job"
        if scope not in resume.FACT_SCOPES:
            raise ValueError(f"proposal {index}: suggested_scope {scope!r}")
        known_ids.add(fact.id)
        cleaned.append(
            {
                "index": index,
                "fact": fact.model_dump(exclude_defaults=True),
                "posting_ask": proposal.get("posting_ask") or "",
                "suggested_scope": scope,
            }
        )
    lines = [
        f"{item['index'] + 1}. {item['fact'].get('text') or item['fact']['id']}"
        f" (suggested: {item['suggested_scope']})"
        for item in cleaned
    ]
    question = (
        "The posting asks for things your fact base does not mention. For each, "
        "choose master (add to the fact base), job (this resume only), or skip, "
        "and correct the wording if needed:\n" + "\n".join(lines)
    )
    return blockers.raise_blocker(
        conn,
        job_id,
        "fact_gap",
        actor,
        question=question,
        field_spec={"proposals": cleaned, "decisions": list(DECISIONS)},
    )


def resolve_gaps(
    conn: sqlite3.Connection,
    paths: DataPaths,
    blocker_id: int,
    decisions: list[dict],
    owner_answer: str,
    actor: str = "claude",
) -> dict:
    """Apply the owner's answer to a fact_gap blocker. decisions is a list
    of {"index": n, "decision": "master"|"job"|"skip", "text": optional
    corrected text}. Every proposal needs a decision."""
    answer = _owner_text(owner_answer)
    blocker = blockers.get_blocker(conn, blocker_id)
    if blocker.kind != "fact_gap":
        raise ValueError(f"blocker {blocker_id} is a {blocker.kind} blocker")
    if blocker.status not in ("open", "answered"):
        raise ValueError(f"blocker {blocker_id} is {blocker.status}")
    spec = from_json(blocker.field_spec) or {}
    proposals = {item["index"]: item for item in spec.get("proposals", [])}
    by_index = {}
    for decision in decisions:
        index = decision.get("index")
        if index not in proposals:
            raise ValueError(f"no proposal {index!r} on blocker {blocker_id}")
        if decision.get("decision") not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        by_index[index] = decision
    missing = sorted(set(proposals) - set(by_index))
    if missing:
        raise ValueError(f"proposals {missing} have no decision yet")

    added, skipped = [], []
    for index in sorted(proposals):
        decision = by_index[index]
        if decision["decision"] == "skip":
            skipped.append(index)
            continue
        fact = dict(proposals[index]["fact"])
        if decision.get("text"):
            fact["text"] = decision["text"]
        added.append(
            add_fact(
                conn, paths, blocker.job_id, fact, decision["decision"],
                answer, actor,
            )
        )
    blockers.set_status(conn, blocker_id, "resolved", actor)
    return {"added": added, "skipped": skipped}
