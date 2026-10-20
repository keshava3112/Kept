"""The graph over MCP, so any agent (Claude Code, Cursor, your own) can read it.

  claude mcp add kept -- python -m kept.cli mcp
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from sqlalchemy import or_, select

from . import commitments as cm
from .db import Commitment, CommitmentTransition, Edge, Entity, RawEvent, TimelineEvent, make_session_factory
from .desk import answer as desk_answer
from .desk import find_entities
from .reflexes import describe, fmt_local


def build_server(session_factory=None) -> MCPServer:
    sf = session_factory or make_session_factory()
    server = MCPServer(
        "kept",
        instructions="Company context graph: people, projects, tasks and the promises between them. "
        "Every fact carries a receipt [ev:N]; use get_receipt to see the original message.",
    )

    @server.tool()
    def search(query: str) -> list[dict]:
        """Find people, projects and tasks named in a free-text query."""
        with sf() as s:
            return [{"id": e.id, "kind": e.kind, "name": e.name} for e in find_entities(s, query)]

    @server.tool()
    def get_entity(entity_id: int) -> dict:
        """One card: identifiers, relationships, timeline and commitments for an entity."""
        with sf() as s:
            e = s.get(Entity, entity_id)
            if not e:
                return {"error": "not found"}
            edges = s.scalars(select(Edge).where(or_(Edge.src_id == e.id, Edge.dst_id == e.id))).all()
            tl = s.scalars(
                select(TimelineEvent).where(TimelineEvent.entity_id == e.id).order_by(TimelineEvent.valid_at)
            ).all()
            cs = s.scalars(
                select(Commitment).where(or_(Commitment.owner_id == e.id, Commitment.linked_task_id == e.id))
            ).all()
            return {
                "id": e.id,
                "kind": e.kind,
                "name": e.name,
                "attrs": e.attrs,
                "identifiers": [f"{i.scheme}:{i.value}" for i in e.identifiers],
                "relationships": [
                    f"{s.get(Entity, x.src_id).name} {x.rel} {s.get(Entity, x.dst_id).name}" for x in edges
                ],
                "timeline": [
                    {"at": fmt_local(t.valid_at), "kind": t.kind, "value": t.value, "receipt": t.source_event_id}
                    for t in tl
                ],
                "commitments": [describe(c) for c in cs],
            }

    @server.tool()
    def open_commitments(owner: str | None = None) -> list[str]:
        """Open promises, optionally for one person (by name)."""
        with sf() as s:
            owner_id = None
            if owner:
                hits = [e for e in find_entities(s, owner) if e.kind == "person"]
                if not hits:
                    return []
                owner_id = hits[0].id
            return [describe(c) for c in cm.open_commitments(s, owner_id)]

    @server.tool()
    def commitment_history(commitment_id: int) -> dict:
        """Every state change of one promise, with reasons and receipts."""
        with sf() as s:
            c = s.get(Commitment, commitment_id)
            if not c:
                return {"error": "not found"}
            hist = s.scalars(
                select(CommitmentTransition).where(CommitmentTransition.commitment_id == c.id)
            ).all()
            return {
                "commitment": describe(c),
                "receipt": c.source_event_id,
                "history": [
                    {"at": fmt_local(h.at), "from": h.from_status, "to": h.to_status, "why": h.reason} for h in hist
                ],
            }

    @server.tool()
    def get_receipt(event_id: int) -> dict:
        """The original message or record behind a fact."""
        with sf() as s:
            ev = s.get(RawEvent, event_id)
            if not ev:
                return {"error": "not found"}
            return {
                "source": ev.source,
                "channel": ev.channel,
                "who": (ev.payload or {}).get("user_name") or ev.author,
                "when": fmt_local(ev.occurred_at),
                "text": ev.text,
            }

    @server.tool()
    def ask(question: str) -> str:
        """Ask the reading desk a question; the answer cites receipts."""
        with sf() as s:
            return desk_answer(s, question)

    return server
