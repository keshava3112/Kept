from datetime import datetime
from types import SimpleNamespace

from kept.extract import (
    Extracted,
    LLMExtractor,
    MessageContext,
    RuleExtractor,
    parse_due,
    passes_gates,
    worth_reading,
)

# Tue 13 Oct 2026, 11:00 IST == 05:30 UTC
TUE = datetime(2026, 10, 13, 5, 30)


def ist(d: datetime) -> str:
    from zoneinfo import ZoneInfo
    from datetime import timezone

    return d.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%a %d %H:%M")


def test_parse_due_relative_days():
    assert ist(parse_due("by friday", TUE)) == "Fri 16 18:00"
    assert ist(parse_due("tomorrow", TUE)) == "Wed 14 18:00"
    assert ist(parse_due("eod", TUE)) == "Tue 13 18:00"
    assert ist(parse_due("by tuesday", TUE)) == "Tue 20 18:00"  # same weekday = next week
    assert ist(parse_due("by next friday", TUE)) == "Fri 23 18:00"  # friday after this one
    assert ist(parse_due("by next monday", TUE)) == "Mon 19 18:00"  # already next week
    assert parse_due("at some point", TUE) is None


def test_prefilter():
    assert worth_reading("I'll send it by Friday")
    assert worth_reading("on it")
    assert not worth_reading("lol nice")
    assert not worth_reading("Can someone check the invoice?")


def test_rule_extractor():
    msg = MessageContext("Kickoff done. I'll send the revised SOW to Meera by Friday.", "Sana", "#kelso", TUE)
    [x] = RuleExtractor().extract(msg)
    assert x.text == "Send the revised SOW to Meera"
    assert ist(x.due_at) == "Fri 16 18:00"


def test_gates_require_verbatim_quote_and_confidence():
    body = "I'll send the SOW by Friday"
    assert passes_gates(Extracted("Send SOW", "speaker", "I'll send the SOW", confidence=0.9), body)
    assert not passes_gates(Extracted("Send SOW", "speaker", "I will send the SOW", confidence=0.9), body)
    assert not passes_gates(Extracted("Send SOW", "speaker", "I'll send the SOW", confidence=0.3), body)


class FakeClient:
    """Stands in for anthropic.Anthropic; records the request."""

    def __init__(self, tool_input):
        self.tool_input = tool_input
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=self.tool_input)])


def test_llm_extractor_parses_tool_output_and_dates():
    client = FakeClient({"commitments": [
        {"text": "Send SOW to Meera", "owner": "speaker", "quote": "I'll send the SOW", "due_local": "2026-10-16", "confidence": 0.9},
        {"text": "Look at analytics", "owner": "speaker", "quote": "maybe look", "due_local": None, "confidence": 0.2},
    ]})
    out = LLMExtractor(client=client, model="m").extract(MessageContext("I'll send the SOW, maybe look", "Sana", "#k", TUE))
    assert len(out) == 2
    assert ist(out[0].due_at) == "Fri 16 18:00"
    req = client.calls[0]
    assert req["tool_choice"] == {"type": "tool", "name": "record_commitments"}
    assert "Tuesday 2026-10-13 11:00" in req["messages"][0]["content"]
