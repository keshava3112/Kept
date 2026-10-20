"""Commands people type to the bot (DM or @mention). Deterministic, no model.

  done 12            mark kept
  drop 12            mark dropped
  push 12 to friday  renegotiate the date
  mine               my open promises
  open               everyone's open promises
  brief              the weekly brief, now
  merges             pending identity reviews
  merge 3 yes|no     resolve one
  help
Anything else falls through to the reading desk (returns None here).
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import commitments as cm
from .db import Commitment, Entity, MergeCandidate, utcnow
from .extract import parse_due
from .reflexes import describe, fmt_local, weekly_brief
from .resolve import merge_entities

HELP = __doc__.split("\n", 1)[1].rsplit("Anything else", 1)[0]


def _get(session: Session, cid: str) -> Commitment | None:
    return session.get(Commitment, int(cid))


def handle_command(session: Session, text: str, asker: Entity | None, now: datetime | None = None) -> str | None:
    now = now or utcnow()
    t = text.strip()
    low = t.lower()

    if low in {"help", "?"}:
        return HELP

    if m := re.fullmatch(r"(done|drop|kept)\s+#?(\d+)", low):
        c = _get(session, m.group(2))
        if not c:
            return f"No promise #{m.group(2)}."
        to = "dropped" if m.group(1) == "drop" else "done"
        if c.status == to:
            return f"#{c.id} is already {c.status}."
        try:
            cm.transition(session, c, to, f"marked {to} by {asker.name if asker else 'someone'}", at=now)
        except cm.IllegalTransition:
            return f"#{c.id} is already {c.status}."
        session.commit()
        return f"#{c.id} → {to}. {'Nice.' if to == 'done' else ''}".strip()

    if m := re.fullmatch(r"push\s+#?(\d+)\s+(?:to\s+)?(.+)", low):
        c = _get(session, m.group(1))
        if not c:
            return f"No promise #{m.group(1)}."
        due = parse_due(m.group(2), now)
        if not due:
            return "I couldn't read that date. Try `push 12 to friday` or `push 12 to tomorrow`."
        try:
            cm.transition(session, c, "renegotiated", f"pushed to {fmt_local(due)}", at=now)
        except cm.IllegalTransition:
            return f"#{c.id} is {c.status}; can't push it."
        c.due_at = due
        c.last_nudged_at = None
        session.commit()
        return f"#{c.id} now due {fmt_local(due)}. Want me to tell {c.counterparty or 'them'}? (I won't unless you ask.)"

    if low == "mine":
        if not asker:
            return "I don't know who you are yet. Say something in a channel I'm in first."
        items = cm.open_commitments(session, asker.id)
        return "\n".join(f"• {describe(c)}" for c in items) or "Nothing open. 🎉"

    if low == "open":
        items = cm.open_commitments(session)
        return "\n".join(f"• {describe(c)}" for c in items) or "No open promises."

    if low == "brief":
        return weekly_brief(session, now)

    if low == "merges":
        rows = session.scalars(select(MergeCandidate).where(MergeCandidate.status == "pending")).all()
        if not rows:
            return "No identity reviews pending."
        lines = []
        for r in rows:
            a, b = session.get(Entity, r.a_id), session.get(Entity, r.b_id)
            lines.append(f"• [{r.id}] {a.name} ⟷ {b.name} ({r.reason}) — `merge {r.id} yes|no`")
        return "Same person/thing?\n" + "\n".join(lines)

    if m := re.fullmatch(r"merge\s+(\d+)\s+(yes|no|y|n)", low):
        r = session.get(MergeCandidate, int(m.group(1)))
        if not r or r.status != "pending":
            return "That review isn't pending."
        if m.group(2).startswith("y"):
            keep, drop = sorted((r.a_id, r.b_id))  # keep the older entity
            merge_entities(session, keep, drop)
            session.commit()
            return "Merged."
        r.status = "rejected"
        session.commit()
        return "Kept separate. I won't ask about that pair again."

    return None
