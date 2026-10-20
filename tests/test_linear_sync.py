import json

import httpx
from sqlalchemy import func, select

from kept.db import Entity, RawEvent
from kept.extract import RuleExtractor
from kept.ingest.linear_sync import sync_linear
from kept.projector import process_pending


def node(i, state, stype, updated):
    return {
        "id": f"I{i}", "identifier": f"KEL-{i}", "title": f"Task {i}", "url": "u", "dueDate": None,
        "updatedAt": updated, "state": {"name": state, "type": stype},
        "assignee": {"id": "L1", "name": "Sana Iyer", "email": "sana@studio.co"},
        "project": {"id": "P1", "name": "Kelso Rebrand"},
    }


def test_sync_paginates_and_is_idempotent(session):
    pages = [
        {"nodes": [node(1, "Todo", "unstarted", "2026-10-12T05:00:00.000Z")], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}},
        {"nodes": [node(2, "Done", "completed", "2026-10-13T05:00:00.000Z")], "pageInfo": {"hasNextPage": False, "endCursor": None}},
    ]
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body["variables"])
        assert req.headers["Authorization"] == "key"
        page = pages[1] if body["variables"]["after"] == "c1" else pages[0]
        return httpx.Response(200, json={"data": {"issues": page}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert sync_linear(session, api_key="key", http=http) == 2
    assert seen[0]["since"] == "-P90D"
    process_pending(session, RuleExtractor())
    assert session.scalar(select(func.count()).select_from(Entity).where(Entity.kind == "task")) == 2

    seen.clear()
    assert sync_linear(session, api_key="key", http=http) == 0  # same versions: nothing new
    assert seen[0]["since"].startswith("2026-10-13T05:00:00")
    assert session.scalar(select(func.count()).select_from(RawEvent)) == 2
