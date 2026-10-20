"""Commitment extraction from unstructured messages.

Pipeline per message (cheapest first):
  1. Prefilter: a regex for promise language. ~90% of chat never reaches a model.
  2. Extractor: a cheap model with a forced tool schema (or RuleExtractor offline).
  3. Gates: confidence floor, and the quote must appear verbatim in the message
     (a promise without a receipt is not recorded).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from .config import settings

PREFILTER = re.compile(
    r"\b(i'?ll|i will|we'?ll|we will|will (send|share|do|get|have|fix|ship|follow|update|review)|"
    r"let me|on it|i can have|promise|by (mon|tues?|wed|thur?s?|fri|sat|sun)\w*|"
    r"by (tomorrow|today|tonight|eod|eow|end of|next|the \d)|before (monday|friday|eod|eow))\b",
    re.I,
)


def worth_reading(text: str | None) -> bool:
    return bool(text and PREFILTER.search(text))


@dataclass
class Extracted:
    text: str  # short imperative: "Send SOW to Meera"
    owner: str  # "speaker" or a person's name / handle
    quote: str  # exact words from the message
    due_at: datetime | None = None  # naive UTC
    counterparty: str | None = None
    confidence: float = 1.0


@dataclass
class MessageContext:
    text: str
    speaker: str
    channel: str
    sent_at: datetime  # naive UTC
    thread: list[str] | None = None  # a few prior messages, "Name: text"


class Extractor(Protocol):
    def extract(self, msg: MessageContext) -> list[Extracted]: ...


# --------------------------------------------------------------- dates
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def end_of_local_day(day_local: datetime, tz: ZoneInfo) -> datetime:
    dt = datetime.combine(day_local.date(), time(18, 0), tzinfo=tz)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def parse_due(phrase: str, sent_at_utc: datetime, tz_name: str | None = None) -> datetime | None:
    """Resolve 'by friday' / 'tomorrow' / 'eod' relative to the message time.
    Due = 18:00 local on that day, stored as naive UTC."""
    tz = ZoneInfo(tz_name or settings.timezone)
    local = sent_at_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    p = phrase.lower()
    if re.search(r"\b(today|tonight|eod)\b", p):
        return end_of_local_day(local, tz)
    if "tomorrow" in p:
        return end_of_local_day(local + timedelta(days=1), tz)
    if re.search(r"\b(eow|end of (the )?week)\b", p):
        return end_of_local_day(local + timedelta(days=(4 - local.weekday()) % 7), tz)
    m = re.search(r"\b(next )?(mon|tue|wed|thu|fri|sat|sun)\w*", p)
    if m:
        target = WEEKDAYS.index(m.group(2))
        ahead = (target - local.weekday()) % 7 or 7  # "by friday" said on friday = next friday
        if m.group(1) and target > local.weekday():
            ahead += 7  # "next friday" said on a monday = the friday after this one
        return end_of_local_day(local + timedelta(days=ahead), tz)
    return None


# ------------------------------------------------------- offline extractor
RULE = re.compile(
    r"(?P<quote>\b(?:i'?ll|i will|we'?ll|we will)\s+(?P<what>[^.?!\n]+?)"
    r"(?:\s+(?P<due>by\s+[^.?!,\n]+|today|tomorrow|eod))?)(?=[.?!,\n]|$)",
    re.I,
)


class RuleExtractor:
    """Deterministic, no network. Good for demos, tests and as a fallback."""

    def extract(self, msg: MessageContext) -> list[Extracted]:
        out = []
        for m in RULE.finditer(msg.text):
            what = m.group("what").strip()
            due_phrase = m.group("due") or ""
            due = parse_due(due_phrase, msg.sent_at) if due_phrase else None
            out.append(
                Extracted(
                    text=what[0].upper() + what[1:],
                    owner="speaker",
                    quote=m.group("quote").strip(),
                    due_at=due,
                    confidence=0.8 if due else 0.65,
                )
            )
        return out


# ------------------------------------------------------------ LLM extractor
TOOL = {
    "name": "record_commitments",
    "description": "Record every concrete commitment made in the message. Return an empty list if none.",
    "input_schema": {
        "type": "object",
        "properties": {
            "commitments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Short imperative, e.g. 'Send revised SOW to Meera'"},
                        "owner": {"type": "string", "description": "'speaker' if the speaker committed, else the person's name or @handle"},
                        "counterparty": {"type": ["string", "null"], "description": "Who it is owed to, if stated"},
                        "due_local": {"type": ["string", "null"], "description": "ISO 8601 local date or datetime it is due, resolved against the message time; null if no date"},
                        "quote": {"type": "string", "description": "The exact words from the message that make the commitment, copied verbatim"},
                        "confidence": {"type": "number", "description": "0-1. Below 0.5 for vague intent ('might', 'should try')"},
                    },
                    "required": ["text", "owner", "quote", "confidence"],
                },
            }
        },
        "required": ["commitments"],
    },
}

SYSTEM = """You extract commitments from workplace chat.
A commitment is a concrete promise by a specific person to do a specific thing,
usually for someone else. Not commitments: questions, wishes, past actions,
vague intent ("we should look at this sometime"), or requests to others unless
the other person accepts in the same message.
Copy `quote` verbatim from the message. Resolve relative dates against the
message's local send time. If no date is stated, due_local is null."""


class LLMExtractor:
    def __init__(self, client=None, model: str | None = None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.client = client
        self.model = model or settings.reflex_model

    def extract(self, msg: MessageContext) -> list[Extracted]:
        tz = ZoneInfo(settings.timezone)
        local = msg.sent_at.replace(tzinfo=timezone.utc).astimezone(tz)
        thread = "\n".join(msg.thread or [])
        user = (
            f"Channel: {msg.channel}\nSent: {local:%A %Y-%m-%d %H:%M} ({settings.timezone})\n"
            + (f"Earlier in thread:\n{thread}\n" if thread else "")
            + f"Speaker: {msg.speaker}\nMessage:\n{msg.text}"
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=800,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=[TOOL],
            tool_choice={"type": "tool", "name": "record_commitments"},
            messages=[{"role": "user", "content": user}],
        )
        block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
        if block is None:
            return []
        data = block.input if isinstance(block.input, dict) else json.loads(block.input)
        out = []
        for item in data.get("commitments", []):
            due = None
            if item.get("due_local"):
                try:
                    d = datetime.fromisoformat(item["due_local"])
                    if len(item["due_local"]) <= 10:  # date only -> 18:00 local
                        d = datetime.combine(d.date(), time(18, 0))
                    due = d.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
                except ValueError:
                    due = None
            out.append(
                Extracted(
                    text=item["text"],
                    owner=item.get("owner") or "speaker",
                    quote=item.get("quote", ""),
                    due_at=due,
                    counterparty=item.get("counterparty"),
                    confidence=float(item.get("confidence", 0)),
                )
            )
        return out


def passes_gates(e: Extracted, message_text: str) -> bool:
    if e.confidence < settings.min_commitment_confidence:
        return False
    q = re.sub(r"\s+", " ", e.quote.strip().lower())
    body = re.sub(r"\s+", " ", message_text.lower())
    return bool(q) and q in body  # receipt gate


def default_extractor() -> Extractor:
    return LLMExtractor() if settings.anthropic_api_key else RuleExtractor()
