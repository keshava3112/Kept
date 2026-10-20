"""Event log -> projections. Deterministic given (log, extractor version).

`replay()` wipes every projection and rebuilds it from raw_events, which is
how a new extraction prompt or rule ships: replay, diff, done.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from . import commitments as cm
from .db import (
    PROJECTION_TABLES,
    Commitment,
    Edge,
    Entity,
    Identifier,
    RawEvent,
    TimelineEvent,
)
from .extract import Extractor, MessageContext, passes_gates, worth_reading
from .resolve import find_by_identifier, norm_name, resolve_person

log = logging.getLogger(__name__)
MESSAGE_SOURCES = {"slack", "email", "whatsapp"}


# ------------------------------------------------------------- ingestion
def record_event(
    session: Session,
    *,
    source: str,
    kind: str,
    external_id: str,
    occurred_at: datetime,
    text: str | None = None,
    channel: str | None = None,
    author: str | None = None,
    payload: dict | None = None,
) -> RawEvent | None:
    """Append to the log. Idempotent: the same (source, external_id) is stored once."""
    exists = session.scalar(
        select(RawEvent.id).where(RawEvent.source == source, RawEvent.external_id == external_id)
    )
    if exists:
        return None
    ev = RawEvent(
        source=source,
        kind=kind,
        external_id=external_id,
        occurred_at=occurred_at,
        text=text,
        channel=channel,
        author=author,
        payload=payload or {},
    )
    session.add(ev)
    session.flush()
    return ev


# ------------------------------------------------------------ projection
def _once_timeline(session: Session, entity_id: int, kind: str, value: dict, ev: RawEvent) -> None:
    rows = session.scalars(
        select(TimelineEvent).where(TimelineEvent.entity_id == entity_id, TimelineEvent.kind == kind)
    )
    if not any(r.value == value for r in rows):
        session.add(
            TimelineEvent(entity_id=entity_id, kind=kind, value=value, valid_at=ev.occurred_at, source_event_id=ev.id)
        )


def _thread_context(session: Session, ev: RawEvent, n: int = 3) -> list[str]:
    prior = session.scalars(
        select(RawEvent)
        .where(RawEvent.channel == ev.channel, RawEvent.kind == "message", RawEvent.id < ev.id)
        .order_by(RawEvent.id.desc())
        .limit(n)
    ).all()
    return [f"{(p.payload or {}).get('user_name') or p.author}: {p.text}" for p in reversed(prior)]


def resolve_owner(session: Session, owner: str, speaker: Entity, source: str) -> Entity | None:
    if not owner or owner.lower() in {"speaker", "me", "i"}:
        return speaker
    handle = owner.lstrip("@")
    hit = find_by_identifier(session, source, handle) or find_by_identifier(session, f"{source}_handle", handle)
    if hit:
        return hit
    # Only an exact normalised-name match: a near miss is not an owner (R3).
    target = norm_name(owner)
    people = session.scalars(select(Entity).where(Entity.kind == "person")).all()
    exact = [p for p in people if norm_name(p.name) == target]
    if len(exact) == 1:
        return exact[0]
    first = [p for p in people if norm_name(p.name).split(" ")[0] == target]
    return first[0] if len(first) == 1 else None


def project_message(session: Session, ev: RawEvent, extractor: Extractor) -> list[Commitment]:
    p = ev.payload or {}
    ids = {ev.source: ev.author, "email": p.get("user_email"), f"{ev.source}_handle": p.get("user_handle")}
    speaker = resolve_person(
        session,
        ids,
        p.get("user_name") or ev.author or "unknown",
        vouched_by=ev.source,
        event_id=ev.id,
        channels={ev.channel} if ev.channel else None,
    )
    if ev.channel:
        _once_timeline(session, speaker.id, "spoke_in", {"channel": ev.channel}, ev)

    if not worth_reading(ev.text):  # Lane 1 prefilter: no model call
        return []

    msg = MessageContext(
        text=ev.text or "",
        speaker=speaker.name,
        channel=ev.channel or "",
        sent_at=ev.occurred_at,
        thread=_thread_context(session, ev),
    )
    created = []
    for x in extractor.extract(msg):
        if not passes_gates(x, msg.text):
            log.info("gate rejected %r (conf=%.2f)", x.quote, x.confidence)
            continue
        owner = resolve_owner(session, x.owner, speaker, ev.source)
        c = Commitment(
            text=x.text,
            owner_id=owner.id if owner else None,
            counterparty=x.counterparty,
            channel=ev.channel,
            due_at=x.due_at,
            confidence=x.confidence,
            quote=x.quote,
            source_event_id=ev.id,
            created_at=ev.occurred_at,
            updated_at=ev.occurred_at,
        )
        session.add(c)
        session.flush()
        cm.try_link(session, c, event_id=ev.id, at=ev.occurred_at)
        created.append(c)
    return created


def _entity_by_ident(session: Session, scheme: str, value: str, kind: str, name: str, ev: RawEvent) -> Entity:
    e = find_by_identifier(session, scheme, value)
    if e:
        if e.name != name:
            e.name = name
        return e
    e = Entity(kind=kind, name=name, vouched_by=ev.source, source_event_id=ev.id)
    session.add(e)
    session.flush()
    session.add(Identifier(entity_id=e.id, scheme=scheme, value=value))
    session.flush()
    return e


def project_task(session: Session, ev: RawEvent) -> Entity:
    """A structured source vouches for the task, its project and its assignee:
    these are facts, no model involved (principle 1)."""
    p = ev.payload
    task = _entity_by_ident(session, f"{ev.source}", p["id"], "task", p["title"], ev)
    task.attrs = {
        **(task.attrs or {}),
        "key": p.get("identifier"),
        "url": p.get("url"),
        "due": p.get("dueDate"),
    }

    if proj := p.get("project"):
        project = _entity_by_ident(session, f"{ev.source}_project", proj["id"], "project", proj["name"], ev)
        if not session.scalar(select(Edge).where(Edge.src_id == task.id, Edge.rel == "part_of", Edge.dst_id == project.id)):
            session.add(Edge(src_id=task.id, rel="part_of", dst_id=project.id, source_event_id=ev.id))

    assignee = p.get("assignee")
    current = set(session.scalars(select(Edge.dst_id).where(Edge.src_id == task.id, Edge.rel == "assigned_to")).all())
    if assignee:
        person = resolve_person(
            session,
            {f"{ev.source}_user": assignee["id"], "email": assignee.get("email")},
            assignee.get("name", ""),
            vouched_by=ev.source,
            event_id=ev.id,
        )
        if current != {person.id}:
            session.execute(delete(Edge).where(Edge.src_id == task.id, Edge.rel == "assigned_to"))
            session.add(Edge(src_id=task.id, rel="assigned_to", dst_id=person.id, source_event_id=ev.id))
            session.add(TimelineEvent(entity_id=task.id, kind="assignee", value={"person_id": person.id}, valid_at=ev.occurred_at, source_event_id=ev.id))
    elif current:
        session.execute(delete(Edge).where(Edge.src_id == task.id, Edge.rel == "assigned_to"))

    state = p.get("state") or {}
    last = session.scalars(
        select(TimelineEvent)
        .where(TimelineEvent.entity_id == task.id, TimelineEvent.kind == "status")
        .order_by(TimelineEvent.valid_at.desc(), TimelineEvent.id.desc())
    ).first()
    if not last or last.value.get("name") != state.get("name"):
        session.add(TimelineEvent(entity_id=task.id, kind="status", value=state, valid_at=ev.occurred_at, source_event_id=ev.id))
    session.flush()

    cm.follow_task(session, task, state.get("type"), event_id=ev.id, at=ev.occurred_at)
    # A new ticket may be the one an earlier promise was about.
    for c in session.scalars(select(Commitment).where(Commitment.linked_task_id.is_(None))).all():
        cm.try_link(session, c, event_id=ev.id, at=ev.occurred_at)
    return task


def project_event(session: Session, ev: RawEvent, extractor: Extractor) -> None:
    if ev.kind == "message" and ev.source in MESSAGE_SOURCES:
        project_message(session, ev, extractor)
    elif ev.kind == "task":
        project_task(session, ev)
    ev.processed = True


def process_pending(session: Session, extractor: Extractor) -> int:
    n = 0
    for ev in session.scalars(select(RawEvent).where(RawEvent.processed.is_(False)).order_by(RawEvent.id)).all():
        try:
            project_event(session, ev, extractor)
            session.commit()
            n += 1
        except Exception:  # one bad event must not stall the log
            session.rollback()
            log.exception("failed to project event %s", ev.id)
    return n


def replay(session: Session, extractor: Extractor) -> int:
    """Drop all projections, rebuild from the log."""
    for table in PROJECTION_TABLES:
        session.execute(delete(table))
    session.execute(update(RawEvent).values(processed=False))
    session.commit()
    return process_pending(session, extractor)
