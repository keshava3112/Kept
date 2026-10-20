"""Lane 1 reflexes: fire on time and triggers, never on prompts. No model calls."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import commitments as cm
from .config import settings
from .db import Commitment, CommitmentTransition, Entity, Identifier, MergeCandidate, utcnow

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def dm(self, person: Entity | None, text: str, fallback_channel: str | None = None) -> None: ...
    def post(self, channel: str, text: str) -> None: ...


class ConsoleNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def dm(self, person, text, fallback_channel=None):
        to = f"@{person.name}" if person else (fallback_channel or "?")
        self.sent.append((to, text))
        print(f"\n→ {to}\n{text}")

    def post(self, channel, text):
        self.sent.append((channel, text))
        print(f"\n→ {channel}\n{text}")


class SlackNotifier:
    def __init__(self, client):
        self.client = client

    def _slack_id(self, person: Entity | None) -> str | None:
        if not person:
            return None
        return next((i.value for i in person.identifiers if i.scheme == "slack"), None)

    def dm(self, person, text, fallback_channel=None):
        uid = self._slack_id(person)
        if uid:
            ch = self.client.conversations_open(users=uid)["channel"]["id"]
            self.client.chat_postMessage(channel=ch, text=text)
        elif fallback_channel:
            self.client.chat_postMessage(channel=fallback_channel, text=text)

    def post(self, channel, text):
        self.client.chat_postMessage(channel=channel, text=text)


def fmt_local(dt: datetime | None) -> str:
    if not dt:
        return "no date"
    local = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone))
    hour = local.strftime("%I").lstrip("0")  # portable (no %-I on Windows)
    return local.strftime(f"%a %d %b, {hour}:%M %p")


def describe(c: Commitment) -> str:
    owner = c.owner.name if c.owner else "unowned"
    ticket = f" · ticket {c.linked_task.attrs.get('key') or c.linked_task.name}" if c.linked_task else ""
    where = f" in {c.channel}" if c.channel else ""
    return f"#{c.id} {c.text} — {owner}, due {fmt_local(c.due_at)}{ticket}\n      “{c.quote}”{where}"


# ------------------------------------------------------------------ nudges
def run_nudges(session: Session, notifier: Notifier, now: datetime | None = None) -> list[Commitment]:
    now = now or utcnow()
    cm.mark_overdue(session, now)
    cooldown = timedelta(hours=settings.nudge_cooldown_hours)
    nudged = []
    candidates = {c.id: c for c in cm.due_soon(session, now)}
    for c in cm.open_commitments(session):
        if c.status == "broken":
            candidates[c.id] = c
    for c in candidates.values():
        if c.last_nudged_at and now - c.last_nudged_at < cooldown:
            continue
        late = c.status == "broken"
        head = "⏰ Overdue" if late else "Heads up — due soon"
        text = (
            f"{head}: you said “{c.quote}”"
            + (f" in {c.channel}" if c.channel else "")
            + f".\nDue {fmt_local(c.due_at)}."
            + f"\nReply `done {c.id}`, `push {c.id} to <day>`, or `drop {c.id}`."
        )
        notifier.dm(c.owner, text, fallback_channel=c.channel)
        c.last_nudged_at = now
        nudged.append(c)
    session.commit()
    return nudged


# ------------------------------------------------------------- reliability
def reliability(session: Session, since: datetime) -> dict[str, dict]:
    """Per owner: closed in window, kept on time, kept late, dropped."""
    stats: dict[str, dict] = defaultdict(lambda: {"on_time": 0, "late": 0, "dropped": 0})
    rows = session.scalars(
        select(CommitmentTransition).where(
            CommitmentTransition.at >= since, CommitmentTransition.to_status.in_(("done", "dropped"))
        )
    ).all()
    for t in rows:
        c = session.get(Commitment, t.commitment_id)
        name = c.owner.name if c.owner else "unowned"
        if t.to_status == "dropped":
            stats[name]["dropped"] += 1
        elif t.from_status == "broken" or (c.due_at and t.at > c.due_at):
            stats[name]["late"] += 1
        else:
            stats[name]["on_time"] += 1
    return dict(stats)


# ------------------------------------------------------------------- brief
def weekly_brief(session: Session, now: datetime | None = None) -> str:
    now = now or utcnow()
    cm.mark_overdue(session, now)
    week_end = now + timedelta(days=7)
    open_ = cm.open_commitments(session)
    overdue = [c for c in open_ if c.status == "broken"]
    this_week = [c for c in open_ if c.status != "broken" and c.due_at and c.due_at <= week_end]
    undated = [c for c in open_ if not c.due_at]
    unowned = [c for c in open_ if not c.owner_id]
    done = session.scalars(
        select(CommitmentTransition).where(
            CommitmentTransition.to_status == "done", CommitmentTransition.at >= now - timedelta(days=7)
        )
    ).all()
    pending_merges = session.scalars(select(MergeCandidate).where(MergeCandidate.status == "pending")).all()

    def section(title: str, items: list[Commitment]) -> str:
        if not items:
            return ""
        return f"\n*{title}* ({len(items)})\n" + "\n".join(f"  • {describe(c)}" for c in items) + "\n"

    out = [f"*Weekly brief* — {fmt_local(now)}"]
    out.append(section("Overdue", overdue))
    out.append(section("Due in the next 7 days", this_week))
    out.append(section("Promised, no date", undated))
    out.append(section("Nobody owns these", [c for c in unowned if c not in overdue + this_week + undated]))
    if done:
        out.append(f"\n*Kept last week*: {len(done)}")
    rel = reliability(session, now - timedelta(days=30))
    if rel:
        lines = []
        for name, s in sorted(rel.items()):
            total = s["on_time"] + s["late"] + s["dropped"]
            lines.append(f"  • {name}: {s['on_time']}/{total} on time, {s['late']} late, {s['dropped']} dropped")
        out.append("\n*Promise-keeping, last 30 days*\n" + "\n".join(lines))
    if pending_merges:
        out.append(f"\n*Identity review*: {len(pending_merges)} possible duplicate{'' if len(pending_merges) == 1 else 's'} — reply `merges` to review.")
    if not (overdue or this_week or undated):
        out.append("\nNo open promises. Quiet week.")
    return "\n".join(x for x in out if x)


def slack_id_for(session: Session, slack_user: str) -> Entity | None:
    ident = session.scalar(select(Identifier).where(Identifier.scheme == "slack", Identifier.value == slack_user))
    return ident.entity if ident else None
