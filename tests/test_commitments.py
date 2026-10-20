import pytest
from sqlalchemy import select

from kept import commitments as cm
from kept.db import Commitment, CommitmentTransition, Entity
from kept.reflexes import reliability


def by_text(s, fragment):
    return next(c for c in s.scalars(select(Commitment)) if fragment in c.text)


def test_promises_extracted_with_receipts(seeded):
    cs = seeded.scalars(select(Commitment)).all()
    assert len(cs) == 5  # "we should probably look at..." is not a promise
    for c in cs:
        assert c.quote and c.source_event_id


def test_autolink_and_follow_ticket(seeded):
    mood = by_text(seeded, "moodboard")
    assert mood.linked_task.attrs["key"] == "KEL-2"
    assert mood.status == "done"  # followed the ticket to Done
    sow = by_text(seeded, "SOW")
    assert sow.linked_task.attrs["key"] == "KEL-1"
    assert sow.status == "in_progress"


def test_owner_overlap_cannot_prop_up_weak_text(seeded):
    deck = by_text(seeded, "final deck")  # same owner + project word as KEL-1, different work
    assert deck.linked_task_id is None


def test_unlinked_promise_still_tracked(seeded):
    inv = by_text(seeded, "invoice")
    assert inv.linked_task_id is None and inv.status == "made"


def test_overdue_marks_broken(seeded, now):
    broken = cm.mark_overdue(seeded, now)
    assert {c.text.split()[0] for c in broken} == {"Chase", "Share", "Send"}
    assert by_text(seeded, "final deck").status == "made"  # not due yet


def test_illegal_transition(seeded):
    mood = by_text(seeded, "moodboard")
    with pytest.raises(cm.IllegalTransition):
        cm.transition(seeded, mood, "in_progress", "nope")


def test_transitions_use_event_time(seeded, now):
    mood = by_text(seeded, "moodboard")
    done = seeded.scalars(
        select(CommitmentTransition).where(
            CommitmentTransition.commitment_id == mood.id, CommitmentTransition.to_status == "done"
        )
    ).one()
    assert done.at <= mood.due_at  # done Wed 5pm, due Wed 6pm: on time
    rel = reliability(seeded, now.replace(day=1))
    assert rel["Arjun Mehta"] == {"on_time": 1, "late": 0, "dropped": 0}


def test_coverage():
    assert cm.coverage("Share the homepage wireframes with Juniper", "Homepage wireframe") == 1.0
    assert cm.coverage("Send the final deck to Kelso", "Revised SOW for Kelso") < 0.5
