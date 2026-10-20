"""Entity resolution.

Rules (each one is tested in tests/test_resolve.py):

R1  An identifier that already exists wins. No scoring.
R2  Identifiers asserted together by one structured record (e.g. a Slack
    profile that lists an email) belong to the same entity.
R3  A name alone never auto-merges two people. Name similarity is capped so it
    cannot cross the auto-merge threshold without independent evidence.
R4  Reordered names are not trusted ("Nair Meera" != "Meera Nair"): we use
    plain ratio, never token-sort.
R3b Shared email domain or shared channels only count once names already
    look alike: colleagues share both.
R5  The middle band goes to a human review queue (MergeCandidate), never to an
    automatic merge.
R6  Projects are de-duplicated by *cast* (who works around them), not by
    title similarity.
"""
from __future__ import annotations

import re
from itertools import combinations

from rapidfuzz import fuzz
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import settings
from .db import (
    Commitment,
    Edge,
    Entity,
    Identifier,
    MergeCandidate,
    TimelineEvent,
)

NAME_GATE = 0.8  # below this, other signals are ignored
NAME_WEIGHT = 0.6  # R3: max contribution of name similarity
DOMAIN_WEIGHT = 0.3
CAST_WEIGHT = 0.1
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "proton.me"}


def norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", name or "")).strip().lower()


def _domain(ids: dict[str, str]) -> str | None:
    email = ids.get("email")
    if email and "@" in email:
        d = email.split("@", 1)[1].lower()
        return None if d in FREE_MAIL else d
    return None


def _entity_domains(e: Entity) -> set[str]:
    out = set()
    for i in e.identifiers:
        if i.scheme == "email":
            d = _domain({"email": i.value})
            if d:
                out.add(d)
    return out


def _channels(session: Session, entity_id: int) -> set[str]:
    rows = session.scalars(
        select(TimelineEvent).where(
            TimelineEvent.entity_id == entity_id, TimelineEvent.kind == "spoke_in"
        )
    )
    return {r.value.get("channel") for r in rows}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def person_score(
    session: Session,
    candidate: Entity,
    display_name: str,
    ids: dict[str, str],
    channels: set[str],
) -> tuple[float, str]:
    name_sim = fuzz.ratio(norm_name(display_name), norm_name(candidate.name)) / 100  # R4
    dom = _domain(ids)
    same_domain = 1.0 if dom and dom in _entity_domains(candidate) else 0.0
    cast = jaccard(channels, _channels(session, candidate.id))
    reason = f"name={name_sim:.2f} domain={same_domain:.0f} shared_channels={cast:.2f}"
    if name_sim < NAME_GATE:
        # Colleagues share a domain and channels; that is not evidence of identity.
        return 0.0, reason
    score = NAME_WEIGHT * name_sim + DOMAIN_WEIGHT * same_domain + CAST_WEIGHT * cast
    return score, reason


def find_by_identifier(session: Session, scheme: str, value: str) -> Entity | None:
    ident = session.scalar(
        select(Identifier).where(Identifier.scheme == scheme, Identifier.value == value)
    )
    return ident.entity if ident else None


def attach_identifiers(session: Session, entity: Entity, ids: dict[str, str]) -> None:
    for scheme, value in ids.items():
        if value and not find_by_identifier(session, scheme, value):
            session.add(Identifier(entity_id=entity.id, scheme=scheme, value=value))
    session.flush()


def resolve_person(
    session: Session,
    ids: dict[str, str],
    display_name: str,
    *,
    vouched_by: str,
    event_id: int | None = None,
    channels: set[str] | None = None,
) -> Entity:
    ids = {k: v for k, v in ids.items() if v}
    # R1 / R2
    for scheme, value in ids.items():
        hit = find_by_identifier(session, scheme, value)
        if hit:
            attach_identifiers(session, hit, ids)
            if display_name and hit.name.startswith(("unknown", "+")):
                hit.name = display_name
            return hit

    channels = channels or set()
    best, best_score, best_reason = None, 0.0, ""
    for cand in session.scalars(select(Entity).where(Entity.kind == "person")):
        s, r = person_score(session, cand, display_name, ids, channels)
        if s > best_score:
            best, best_score, best_reason = cand, s, r

    if best and best_score >= settings.auto_merge_threshold:
        attach_identifiers(session, best, ids)
        return best

    person = Entity(
        kind="person",
        name=display_name or next(iter(ids.values()), "unknown"),
        vouched_by=vouched_by,
        source_event_id=event_id,
    )
    session.add(person)
    session.flush()
    attach_identifiers(session, person, ids)

    if best and best_score >= settings.review_merge_threshold:  # R5
        _queue(session, best.id, person.id, best_score, best_reason)
    return person


def _queue(session: Session, a: int, b: int, score: float, reason: str) -> None:
    a, b = sorted((a, b))
    exists = session.scalar(
        select(MergeCandidate).where(MergeCandidate.a_id == a, MergeCandidate.b_id == b)
    )
    if not exists:
        session.add(MergeCandidate(a_id=a, b_id=b, score=round(score, 3), reason=reason))
        session.flush()


# ------------------------------------------------------------------ R6
def project_cast(session: Session, project_id: int) -> set[int]:
    """People around a project: owners of its tasks + people who spoke in its channels."""
    task_ids = session.scalars(
        select(Edge.src_id).where(Edge.rel == "part_of", Edge.dst_id == project_id)
    ).all()
    cast = set(
        session.scalars(
            select(Edge.dst_id).where(Edge.rel == "assigned_to", Edge.src_id.in_(task_ids))
        ).all()
    ) if task_ids else set()
    project = session.get(Entity, project_id)
    chans = set((project.attrs or {}).get("channels", []))
    if chans:
        for ev in session.scalars(select(TimelineEvent).where(TimelineEvent.kind == "spoke_in")):
            if ev.value.get("channel") in chans:
                cast.add(ev.entity_id)
    return cast


def suggest_project_merges(session: Session, min_cast: int = 2, threshold: float = 0.6) -> int:
    projects = session.scalars(select(Entity).where(Entity.kind == "project")).all()
    casts = {p.id: project_cast(session, p.id) for p in projects}
    n = 0
    for a, b in combinations(projects, 2):
        ca, cb = casts[a.id], casts[b.id]
        if len(ca) < min_cast or len(cb) < min_cast:
            continue
        j = jaccard(ca, cb)
        if j >= threshold:
            _queue(session, a.id, b.id, j, f"same cast: jaccard={j:.2f} over {len(ca | cb)} people")
            n += 1
    return n


def merge_entities(session: Session, keep_id: int, drop_id: int) -> None:
    """Repoint everything from drop -> keep. Used after a human approves."""
    session.execute(update(Identifier).where(Identifier.entity_id == drop_id).values(entity_id=keep_id))
    session.execute(update(TimelineEvent).where(TimelineEvent.entity_id == drop_id).values(entity_id=keep_id))
    session.execute(update(Commitment).where(Commitment.owner_id == drop_id).values(owner_id=keep_id))
    session.execute(update(Commitment).where(Commitment.linked_task_id == drop_id).values(linked_task_id=keep_id))
    for col in (Edge.src_id, Edge.dst_id):
        for e in session.scalars(select(Edge).where(col == drop_id)).all():
            src = keep_id if e.src_id == drop_id else e.src_id
            dst = keep_id if e.dst_id == drop_id else e.dst_id
            dup = session.scalar(select(Edge).where(Edge.src_id == src, Edge.rel == e.rel, Edge.dst_id == dst))
            if dup or src == dst:
                session.delete(e)
            else:
                e.src_id, e.dst_id = src, dst
    # Candidates pointing at the dropped entity are settled; remove them so the
    # foreign key stays valid on Postgres.
    for mc in session.scalars(
        select(MergeCandidate).where((MergeCandidate.a_id == drop_id) | (MergeCandidate.b_id == drop_id))
    ).all():
        session.delete(mc)
    session.flush()
    session.expire_all()  # bulk updates above bypass the identity map
    session.delete(session.get(Entity, drop_id))
    session.flush()
