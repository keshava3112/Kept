"""A week at a small studio, replayed through the real pipeline.

Runs with no API keys (RuleExtractor + offline desk). Shows: cross-channel
identity merge (Slack profile email == Linear email), the review queue (a
WhatsApp sender with the same name but no shared identifier), promises
extracted from chat, auto-linked to tickets, following ticket status,
going overdue, nudges and the Monday brief.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .projector import process_pending, record_event

IST = ZoneInfo("Asia/Kolkata")
BASE = datetime(2026, 10, 12, 10, 0, tzinfo=IST)  # Monday
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=IST)  # the next Monday, 9am


def utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return utc((BASE + timedelta(days=day)).replace(hour=hh, minute=mm))


PEOPLE = {
    "U1": ("Sana Iyer", "sana@studio.co", "sana"),
    "U2": ("Arjun Mehta", "arjun@studio.co", "arjun"),
    "U3": ("Priya Rao", "priya@studio.co", "priya"),
}

SLACK = [
    (0, 10, 0, "#kelso", "U1", "Kickoff done. I'll send the revised SOW to Meera by Friday."),
    (0, 10, 5, "#kelso", "U2", "Great. I will update the moodboard by Wednesday."),
    (0, 10, 10, "#kelso", "U3", "Can someone check the July invoice status?"),
    (1, 11, 0, "#kelso", "U2", "On it — I'll chase the July invoice with their finance team tomorrow."),
    (1, 15, 0, "#juniper", "U3", "I'll share the homepage wireframes with Juniper by Thursday."),
    (2, 16, 0, "#kelso", "U3", "We should probably look at the analytics setup sometime."),
    (2, 16, 30, "#juniper", "U1", "Thanks Priya, looks good so far."),
    (3, 12, 0, "#kelso", "U1", "Draft SOW is in Drive, reviewing tonight. I'll send the final deck to Kelso by next Wednesday."),
]

LINEAR_USERS = {"U1": "L-sana", "U2": "L-arjun", "U3": "L-priya"}


def issue(iid, key, title, state, stype, who, project, due, day, hh):
    name, email, _ = PEOPLE[who]
    return {
        "id": iid,
        "identifier": key,
        "title": title,
        "url": f"https://linear.app/studio/issue/{key}",
        "dueDate": due,
        "updatedAt": at(day, hh).isoformat() + "Z",
        "state": {"name": state, "type": stype},
        "assignee": {"id": LINEAR_USERS[who], "name": name, "email": email},
        "project": project,
    }


KELSO = {"id": "P-kelso", "name": "Kelso Rebrand"}
JUNIPER = {"id": "P-juniper", "name": "Juniper Site"}

LINEAR = [
    issue("I1", "KEL-1", "Revised SOW for Kelso", "Todo", "unstarted", "U1", KELSO, "2026-10-16", 0, 11),
    issue("I2", "KEL-2", "Update moodboard", "Todo", "unstarted", "U2", KELSO, "2026-10-14", 0, 11),
    issue("I3", "JUN-4", "Homepage wireframes", "In Progress", "started", "U3", JUNIPER, "2026-10-15", 1, 16),
    issue("I1", "KEL-1", "Revised SOW for Kelso", "In Progress", "started", "U1", KELSO, "2026-10-16", 2, 10),
    issue("I2", "KEL-2", "Update moodboard", "Done", "completed", "U2", KELSO, "2026-10-14", 2, 17),
]


def seed(session) -> None:
    rows = []
    for d, h, m, ch, uid, text in SLACK:
        name, email, handle = PEOPLE[uid]
        rows.append((at(d, h, m), dict(
            source="slack", kind="message", external_id=f"{ch}:{d}{h}{m}", text=text, channel=ch, author=uid,
            payload={"user_name": name, "user_email": email, "user_handle": handle},
        )))
    for i in LINEAR:
        rows.append((datetime.fromisoformat(i["updatedAt"][:-1]), dict(
            source="linear", kind="task", external_id=f"{i['id']}@{i['updatedAt']}", text=i["title"],
            author=i["assignee"]["id"], payload=i,
        )))
    # Same human, new channel: email from Sana's work address (merges via identifier).
    rows.append((at(3, 18), dict(
        source="email", kind="message", external_id="msg-881", channel="email:kelso",
        author="sana@studio.co", text="Meera — SOW draft attached, final by Friday as promised.",
        payload={"user_name": "Sana Iyer", "user_email": "sana@studio.co"},
    )))
    # Same name, no shared identifier: must NOT auto-merge, goes to review.
    rows.append((at(4, 9), dict(
        source="whatsapp", kind="message", external_id="wa-17", channel="Kelso · team",
        author="+919800000021", text="Reached the client office, meeting at 10.",
        payload={"user_name": "Arjun Mehta"},
    )))
    for when, kw in sorted(rows, key=lambda r: r[0]):
        record_event(session, occurred_at=when, **kw)
    session.commit()


def run(session, extractor) -> None:
    seed(session)
    process_pending(session, extractor)
