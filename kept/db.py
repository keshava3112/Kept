"""Schema.

Two kinds of tables:

* ``raw_events`` is the append-only source of truth. Nothing else is
  authoritative: every other table is a *projection* that can be wiped and
  rebuilt by replaying the log (``kept replay``).
* Projections: the **spine** (entities, identifiers, edges: identity, rarely
  changes), **timelines** (state over time, hung off spine nodes), and the
  **commitment ledger** (promises with a state machine and receipts).

Every derived row carries ``source_event_id``: the receipt.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # store naive UTC


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- event log
class RawEvent(Base):
    """Append-only. One row per message / record version seen from a source."""

    __tablename__ = "raw_events"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # slack | linear | email | whatsapp
    kind: Mapped[str] = mapped_column(String(32))  # message | task
    external_id: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str | None] = mapped_column(String(255))
    author: Mapped[str | None] = mapped_column(String(255))  # source-native id
    text: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)


# -------------------------------------------------------------------- spine
class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # person | org | project | task
    name: Mapped[str] = mapped_column(String(512))
    attrs: Mapped[dict] = mapped_column(JSON, default=dict)
    # Vouching gate: which structured source asserted this exists.
    vouched_by: Mapped[str | None] = mapped_column(String(64))
    source_event_id: Mapped[int | None] = mapped_column(ForeignKey("raw_events.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    identifiers: Mapped[list["Identifier"]] = relationship(back_populates="entity")


class Identifier(Base):
    """A source-native handle that points at one entity (slack:U123, email:x@y)."""

    __tablename__ = "identifiers"
    __table_args__ = (UniqueConstraint("scheme", "value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"))
    scheme: Mapped[str] = mapped_column(String(32))
    value: Mapped[str] = mapped_column(String(255))

    entity: Mapped[Entity] = relationship(back_populates="identifiers")


class Edge(Base):
    __tablename__ = "edges"
    __table_args__ = (UniqueConstraint("src_id", "rel", "dst_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    src_id: Mapped[int] = mapped_column(ForeignKey("entities.id"))
    rel: Mapped[str] = mapped_column(String(64))  # owns | works_at | part_of | assigned_to
    dst_id: Mapped[int] = mapped_column(ForeignKey("entities.id"))
    source_event_id: Mapped[int | None] = mapped_column(ForeignKey("raw_events.id"))


class MergeCandidate(Base):
    """Review queue: maybe-the-same entities. Never auto-merged below threshold."""

    __tablename__ = "merge_candidates"
    __table_args__ = (UniqueConstraint("a_id", "b_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    a_id: Mapped[int] = mapped_column(ForeignKey("entities.id"))
    b_id: Mapped[int] = mapped_column(ForeignKey("entities.id"))
    score: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|merged|rejected


# ---------------------------------------------------------------- timelines
class TimelineEvent(Base):
    """State, not identity. Bitemporal: when it was true vs when we saw it."""

    __tablename__ = "timeline_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"))
    kind: Mapped[str] = mapped_column(String(64))  # status | assignee | spoke_in | ...
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    valid_at: Mapped[datetime] = mapped_column(DateTime)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source_event_id: Mapped[int | None] = mapped_column(ForeignKey("raw_events.id"))


# ------------------------------------------------------- commitment ledger
class Commitment(Base):
    __tablename__ = "commitments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("entities.id"))
    counterparty: Mapped[str | None] = mapped_column(String(255))
    channel: Mapped[str | None] = mapped_column(String(255))
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="made")
    linked_task_id: Mapped[int | None] = mapped_column(ForeignKey("entities.id"))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    quote: Mapped[str | None] = mapped_column(Text)  # exact words: the receipt
    source_event_id: Mapped[int] = mapped_column(ForeignKey("raw_events.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_nudged_at: Mapped[datetime | None] = mapped_column(DateTime)

    owner: Mapped[Entity | None] = relationship(foreign_keys=[owner_id])
    linked_task: Mapped[Entity | None] = relationship(foreign_keys=[linked_task_id])


class CommitmentTransition(Base):
    __tablename__ = "commitment_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    commitment_id: Mapped[int] = mapped_column(ForeignKey("commitments.id"))
    from_status: Mapped[str] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(512))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source_event_id: Mapped[int | None] = mapped_column(ForeignKey("raw_events.id"))


PROJECTION_TABLES = [
    CommitmentTransition,
    Commitment,
    TimelineEvent,
    MergeCandidate,
    Edge,
    Identifier,
    Entity,
]


def make_engine(url: str | None = None):
    url = url or settings.database_url
    kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    return create_engine(url, **kw)


def make_session_factory(url: str | None = None):
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
