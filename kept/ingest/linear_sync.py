"""Pull Linear issues changed since the last sync into the event log.

Each issue *version* is one raw event (external_id = id@updatedAt), so status
history is preserved in the log even though Linear only shows the latest.
"""
from __future__ import annotations

from datetime import datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import RawEvent
from ..projector import record_event

QUERY = """
query Issues($since: DateTimeOrDuration!, $after: String) {
  issues(filter: {updatedAt: {gt: $since}}, first: 100, after: $after, orderBy: updatedAt) {
    nodes {
      id identifier title url dueDate updatedAt
      state { name type }
      assignee { id name email }
      project { id name }
    }
    pageInfo { hasNextPage endCursor }
  }
}"""


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)


def sync_linear(session: Session, api_key: str | None = None, http: httpx.Client | None = None) -> int:
    api_key = api_key or settings.linear_api_key
    if not api_key:
        raise RuntimeError("LINEAR_API_KEY is not set")
    last = session.scalar(select(func.max(RawEvent.occurred_at)).where(RawEvent.source == "linear"))
    since = last.isoformat() + "Z" if last else "-P90D"  # first run: last 90 days
    http = http or httpx.Client(timeout=30)
    n, after = 0, None
    while True:
        r = http.post(
            "https://api.linear.app/graphql",
            json={"query": QUERY, "variables": {"since": since, "after": after}},
            headers={"Authorization": api_key},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(body["errors"])
        page = body["data"]["issues"]
        for issue in page["nodes"]:
            ev = record_event(
                session,
                source="linear",
                kind="task",
                external_id=f"{issue['id']}@{issue['updatedAt']}",
                occurred_at=_parse(issue["updatedAt"]),
                text=issue["title"],
                author=(issue.get("assignee") or {}).get("id"),
                payload=issue,
            )
            n += ev is not None
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    session.commit()
    return n
