"""Lane 2: the reading desk. Rare, expensive, grounded.

Retrieval is a graph walk, not a vector search: find the entities named in the
question, take their one-hop neighbours, and collect their commitments,
current state and recent messages. Every line carries a receipt [ev:N] that
points at a raw event, so "says who?" always has an answer.
"""
from __future__ import annotations

from rapidfuzz import fuzz
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import settings
from .db import Commitment, CommitmentTransition, Edge, Entity, RawEvent, TimelineEvent
from .reflexes import fmt_local
from .resolve import norm_name

STOP = {"the", "a", "an", "what", "who", "is", "are", "on", "for", "with", "we", "our", "my", "to", "of", "and"}


def _keywords(question: str) -> set[str]:
    return {w for w in norm_name(question).split() if w not in STOP and len(w) > 2}


def find_entities(session: Session, question: str, limit: int = 8) -> list[Entity]:
    q = norm_name(question)
    words = _keywords(question)
    scored = []
    for e in session.scalars(select(Entity)):
        name = norm_name(e.name)
        if not name:
            continue
        hits = len(words & set(name.split()))
        full = fuzz.partial_ratio(name, q) / 100 if len(name) >= 5 else 0
        score = max(full, 0.85 + 0.05 * hits if hits else 0)
        if score >= 0.85:
            scored.append((score, e.kind == "person", e))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [e for _, _, e in scored[:limit]]


def neighbours(session: Session, ids: set[int]) -> set[int]:
    out = set(ids)
    for e in session.scalars(select(Edge).where(or_(Edge.src_id.in_(ids), Edge.dst_id.in_(ids)))):
        out.update((e.src_id, e.dst_id))
    return out


def gather_context(session: Session, question: str, max_messages: int = 15) -> tuple[list[Entity], str]:
    seeds = find_entities(session, question)
    if not seeds:
        return [], ""
    ids = neighbours(session, {e.id for e in seeds})
    ents = {e.id: e for e in session.scalars(select(Entity).where(Entity.id.in_(ids)))}
    lines: list[str] = ["## Entities"]
    for e in ents.values():
        status = session.scalars(
            select(TimelineEvent)
            .where(TimelineEvent.entity_id == e.id, TimelineEvent.kind == "status")
            .order_by(TimelineEvent.valid_at.desc())
        ).first()
        extra = f" · status: {status.value.get('name')} [ev:{status.source_event_id}]" if status else ""
        key = (e.attrs or {}).get("key")
        lines.append(f"- ({e.kind}) {e.name}{f' [{key}]' if key else ''}{extra} [ev:{e.source_event_id}]")
    edges = session.scalars(select(Edge).where(Edge.src_id.in_(ids), Edge.dst_id.in_(ids))).all()
    if edges:
        lines.append("## Relationships")
        lines += [f"- {ents[x.src_id].name} {x.rel} {ents[x.dst_id].name} [ev:{x.source_event_id}]" for x in edges]

    cs = session.scalars(
        select(Commitment).where(or_(Commitment.owner_id.in_(ids), Commitment.linked_task_id.in_(ids)))
    ).all()
    names = sorted(_keywords(question) & {w for e in seeds for w in norm_name(e.name).split()})
    for c in session.scalars(select(Commitment)):
        if c not in cs and any(n and n in norm_name(c.text + " " + (c.quote or "")) for n in names):
            cs.append(c)
    if cs:
        lines.append("## Commitments")
        for c in cs:
            hist = session.scalars(select(CommitmentTransition).where(CommitmentTransition.commitment_id == c.id)).all()
            trail = " → ".join(f"{h.to_status} ({h.reason})" for h in hist)
            lines.append(
                f"- #{c.id} {c.text} · owner {c.owner.name if c.owner else 'none'} · {c.status} · due {fmt_local(c.due_at)}"
                f" · said: “{c.quote}” [ev:{c.source_event_id}]" + (f" · history: {trail}" if trail else "")
            )

    msgs = []
    for ev in session.scalars(select(RawEvent).where(RawEvent.kind == "message").order_by(RawEvent.id.desc())):
        if any(n and n in norm_name(ev.text or "") for n in names) or (ev.channel and any(n in norm_name(ev.channel) for n in names if n)):
            who = (ev.payload or {}).get("user_name") or ev.author
            msgs.append(f"- {fmt_local(ev.occurred_at)} {ev.channel} {who}: {ev.text} [ev:{ev.id}]")
        if len(msgs) >= max_messages:
            break
    if msgs:
        lines.append("## Recent messages (newest first)")
        lines += msgs
    return seeds, "\n".join(lines)


SYSTEM = """You are the team's project manager, answering in a chat thread.
Answer ONLY from the context provided. Cite receipts like [ev:12] after each claim.
If the context does not contain the answer, say so plainly and say what you'd need.
Lead with the answer in one or two sentences, then at most a few bullets. No preamble."""


def answer(session: Session, question: str, client=None) -> str:
    seeds, ctx = gather_context(session, question)
    if not ctx:
        return "I don't have anything on that yet. Which client, project or person do you mean?"
    if client is None and not settings.anthropic_api_key:
        return f"(offline mode — no model key; here is what I found)\n{ctx}"
    if client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.desk_model,
        max_tokens=900,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"<context>\n{ctx}\n</context>\n\nQuestion: {question}"}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
