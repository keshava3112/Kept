"""The commitment ledger: promises as first-class objects with a state machine.

    made ──► linked ──► in_progress ──► done
      │        │            │
      └────────┴────────────┴──► broken (due passed) ──► done (late)
      any open state ──► renegotiated (new due) | dropped
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import Commitment, CommitmentTransition, Edge, Entity, TimelineEvent, utcnow

TRANSITIONS: dict[str, set[str]] = {
    "made": {"linked", "in_progress", "done", "broken", "renegotiated", "dropped"},
    "linked": {"in_progress", "done", "broken", "renegotiated", "dropped"},
    "in_progress": {"done", "broken", "renegotiated", "dropped"},
    "renegotiated": {"linked", "in_progress", "done", "broken", "dropped"},
    "broken": {"done", "renegotiated", "dropped"},
    "done": set(),
    "dropped": set(),
}
OPEN = {"made", "linked", "in_progress", "renegotiated", "broken"}

# Linear workflow state.type -> commitment status
TASK_STATE_MAP = {"started": "in_progress", "completed": "done", "canceled": "dropped"}


class IllegalTransition(ValueError):
    pass


def transition(
    session: Session,
    c: Commitment,
    to: str,
    reason: str,
    *,
    event_id: int | None = None,
    at: datetime | None = None,
) -> bool:
    """Move a commitment. Returns False for a no-op, raises on an illegal move."""
    if c.status == to:
        return False
    if to not in TRANSITIONS.get(c.status, set()):
        raise IllegalTransition(f"{c.status} -> {to}")
    session.add(
        CommitmentTransition(
            commitment_id=c.id,
            from_status=c.status,
            to_status=to,
            reason=reason,
            at=at or utcnow(),
            source_event_id=event_id,
        )
    )
    c.status = to
    c.updated_at = at or utcnow()
    session.flush()
    return True


# ---------------------------------------------------------------- linking
def _task_assignees(session: Session, task_id: int) -> set[int]:
    return set(
        session.scalars(
            select(Edge.dst_id).where(Edge.src_id == task_id, Edge.rel == "assigned_to")
        ).all()
    )


def _task_status(session: Session, task_id: int) -> str | None:
    ev = session.scalars(
        select(TimelineEvent)
        .where(TimelineEvent.entity_id == task_id, TimelineEvent.kind == "status")
        .order_by(TimelineEvent.valid_at.desc(), TimelineEvent.id.desc())
    ).first()
    return ev.value.get("type") if ev else None


STOPWORDS = {"the", "a", "an", "to", "for", "with", "of", "and", "on", "in", "by", "our", "their", "my", "send", "do", "get"}


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]


def coverage(promise: str, task_title: str) -> float:
    """Share of the ticket's content words that the promise mentions (fuzzy per
    word, so 'wireframe' ~ 'wireframes'). Ticket titles are short and specific,
    so asking how much of the title is covered beats symmetric similarity."""
    t, p = _tokens(task_title), _tokens(promise)
    if not t or not p:
        return 0.0
    hit = sum(1 for w in t if any(fuzz.ratio(w, x) >= 85 for x in p))
    return hit / len(t)


def link_score(session: Session, c: Commitment, task: Entity) -> float:
    text_sim = coverage(c.text, task.name)
    if text_sim < 0.5:  # never link on owner/date alone
        return 0.0
    owner = 1.0 if c.owner_id and c.owner_id in _task_assignees(session, task.id) else 0.0
    due = 0.0
    task_due = (task.attrs or {}).get("due")
    if c.due_at and task_due:
        gap = abs((datetime.fromisoformat(task_due) - c.due_at).days)
        due = 1.0 if gap <= 3 else 0.0
    return 0.5 * text_sim + 0.3 * owner + 0.2 * due


def try_link(
    session: Session, c: Commitment, event_id: int | None = None, at: datetime | None = None
) -> Entity | None:
    if c.linked_task_id or c.status not in OPEN:
        return None
    best, best_s = None, 0.0
    for task in session.scalars(select(Entity).where(Entity.kind == "task")):
        if _task_status(session, task.id) in ("completed", "canceled"):
            continue
        s = link_score(session, c, task)
        if s > best_s:
            best, best_s = task, s
    if best and best_s >= settings.link_threshold:
        c.linked_task_id = best.id
        if c.status in ("made", "renegotiated"):
            transition(session, c, "linked", f"matched {best.name!r} (score {best_s:.2f})", event_id=event_id, at=at)
        # Ticket might already be moving: catch up.
        follow_task(session, best, _task_status(session, best.id), event_id=event_id, at=at)
        return best
    return None


def link_open_commitments(session: Session) -> int:
    n = 0
    for c in session.scalars(select(Commitment).where(Commitment.linked_task_id.is_(None))).all():
        if try_link(session, c):
            n += 1
    return n


def follow_task(
    session: Session, task: Entity, state_type: str | None, event_id: int | None = None, at: datetime | None = None
) -> int:
    """A linked ticket moved: move its commitments with it."""
    target = TASK_STATE_MAP.get(state_type or "")
    if not target:
        return 0
    n = 0
    for c in session.scalars(select(Commitment).where(Commitment.linked_task_id == task.id)).all():
        if c.status in OPEN and target in TRANSITIONS[c.status]:
            n += transition(session, c, target, f"ticket {task.name!r} is {state_type}", event_id=event_id, at=at)
    return n


def mark_overdue(session: Session, now: datetime | None = None) -> list[Commitment]:
    now = now or utcnow()
    out = []
    q = select(Commitment).where(Commitment.due_at.is_not(None), Commitment.due_at < now)
    for c in session.scalars(q).all():
        if c.status in OPEN and c.status != "broken":
            transition(session, c, "broken", "due date passed", at=now)
            out.append(c)
    return out


def open_commitments(session: Session, owner_id: int | None = None) -> list[Commitment]:
    q = select(Commitment).where(Commitment.status.in_(OPEN))
    if owner_id:
        q = q.where(Commitment.owner_id == owner_id)
    return list(session.scalars(q.order_by(Commitment.due_at.is_(None), Commitment.due_at)))


def due_soon(session: Session, now: datetime | None = None) -> list[Commitment]:
    now = now or utcnow()
    horizon = now + timedelta(hours=settings.nudge_window_hours)
    return [c for c in open_commitments(session) if c.due_at and c.due_at <= horizon]
