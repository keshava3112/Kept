import asyncio

from sqlalchemy import func, select

from kept.commands import handle_command
from kept.db import Commitment, CommitmentTransition, Entity, RawEvent
from kept.desk import answer, gather_context
from kept.extract import RuleExtractor
from kept.projector import record_event, replay
from kept.reflexes import ConsoleNotifier, run_nudges, weekly_brief


def snapshot(s):
    return (
        sorted((e.kind, e.name) for e in s.scalars(select(Entity))),
        sorted((c.text, c.status, c.linked_task_id is not None) for c in s.scalars(select(Commitment))),
        s.scalar(select(func.count()).select_from(CommitmentTransition)),
    )


def test_replay_is_deterministic(seeded):
    before = snapshot(seeded)
    replay(seeded, RuleExtractor())
    assert snapshot(seeded) == before


def test_event_log_is_idempotent(seeded):
    n = seeded.scalar(select(func.count()).select_from(RawEvent))
    ev = seeded.get(RawEvent, 1)
    assert record_event(seeded, source=ev.source, kind=ev.kind, external_id=ev.external_id, occurred_at=ev.occurred_at) is None
    assert seeded.scalar(select(func.count()).select_from(RawEvent)) == n


def test_email_sender_merges_into_slack_person(seeded):
    sanas = seeded.scalars(select(Entity).where(Entity.name == "Sana Iyer")).all()
    assert len(sanas) == 1


def test_nudges_respect_cooldown(seeded, now):
    n = ConsoleNotifier()
    first = run_nudges(seeded, n, now)
    assert len(first) == 3
    assert run_nudges(seeded, n, now) == []


def test_brief(seeded, now):
    b = weekly_brief(seeded, now)
    assert "*Overdue* (3)" in b and "Due in the next 7 days* (1)" in b
    assert "Arjun Mehta: 1/1 on time" in b


def test_commands_flow(seeded, now):
    s = seeded
    sana = s.scalars(select(Entity).where(Entity.name == "Sana Iyer")).one()
    assert "SOW" in handle_command(s, "mine", sana, now)
    c = next(c for c in s.scalars(select(Commitment)) if "SOW" in c.text)
    assert "now due" in handle_command(s, f"push {c.id} to wednesday", sana, now)
    assert s.get(Commitment, c.id).status == "renegotiated"
    assert "done" in handle_command(s, f"done {c.id}", sana, now)
    assert "already" in handle_command(s, f"done {c.id}", sana, now)
    assert handle_command(s, "where does kelso stand?", sana, now) is None  # falls through to desk


def test_merge_command(seeded, now):
    assert "Arjun Mehta ⟷ Arjun Mehta" in handle_command(seeded, "merges", None, now)
    assert handle_command(seeded, "merge 1 yes", None, now) == "Merged."
    assert len(seeded.scalars(select(Entity).where(Entity.name == "Arjun Mehta")).all()) == 1


def test_desk_context_has_receipts(seeded):
    seeds, ctx = gather_context(seeded, "where does Kelso stand?")
    assert any(e.name == "Kelso Rebrand" for e in seeds)
    assert "[ev:" in ctx and "Revised SOW for Kelso" in ctx and "invoice" in ctx


def test_desk_uses_model_with_context(seeded):
    from types import SimpleNamespace

    class C:
        def __init__(self):
            self.messages = self
            self.req = None

        def create(self, **kw):
            self.req = kw
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="SOW is late [ev:1]")])

    c = C()
    assert answer(seeded, "where does Kelso stand?", client=c) == "SOW is late [ev:1]"
    assert "<context>" in c.req["messages"][0]["content"]


def test_desk_unknown(seeded):
    assert "don't have anything" in answer(seeded, "how is the zebra migration going")


def test_mcp_server_tools(sf):
    from kept import demo
    from kept.mcp_server import build_server

    with sf() as s:
        demo.run(s, RuleExtractor())
    server = build_server(sf)
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert {"search", "get_entity", "open_commitments", "commitment_history", "get_receipt", "ask"} <= tools
    out = asyncio.run(server.call_tool("open_commitments", {"owner": "Sana"}))
    assert "SOW" in str(out)
