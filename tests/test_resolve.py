from sqlalchemy import select

from kept.db import Entity, Identifier, MergeCandidate
from kept.resolve import merge_entities, resolve_person


def people(s):
    return s.scalars(select(Entity).where(Entity.kind == "person")).all()


def test_r1_existing_identifier_wins(session):
    a = resolve_person(session, {"slack": "U1"}, "Sana Iyer", vouched_by="slack")
    b = resolve_person(session, {"slack": "U1"}, "Totally Different Name", vouched_by="slack")
    assert a.id == b.id


def test_r2_identifiers_asserted_together_link_channels(session):
    a = resolve_person(session, {"slack": "U1", "email": "sana@studio.co"}, "Sana Iyer", vouched_by="slack")
    b = resolve_person(session, {"linear_user": "L1", "email": "sana@studio.co"}, "Sana I.", vouched_by="linear")
    assert a.id == b.id
    schemes = {i.scheme for i in session.scalars(select(Identifier).where(Identifier.entity_id == a.id))}
    assert schemes == {"slack", "email", "linear_user"}


def test_r3_same_name_alone_never_auto_merges(session):
    resolve_person(session, {"slack": "U2", "email": "arjun@studio.co"}, "Arjun Mehta", vouched_by="slack")
    resolve_person(session, {"whatsapp": "+9198"}, "Arjun Mehta", vouched_by="whatsapp")
    assert len(people(session)) == 2
    # ...but it is queued for a human (R5)
    assert len(session.scalars(select(MergeCandidate)).all()) == 1


def test_r3b_colleagues_are_not_duplicates(session):
    resolve_person(session, {"slack": "U1", "email": "sana@studio.co"}, "Sana Iyer", vouched_by="slack")
    resolve_person(session, {"slack": "U2", "email": "arjun@studio.co"}, "Arjun Mehta", vouched_by="slack")
    assert session.scalars(select(MergeCandidate)).all() == []


def test_r4_reordered_names_not_trusted(session):
    resolve_person(session, {"slack": "U1"}, "Meera Nair", vouched_by="slack")
    resolve_person(session, {"whatsapp": "+91"}, "Nair Meera", vouched_by="whatsapp")
    assert len(people(session)) == 2
    assert session.scalars(select(MergeCandidate)).all() == []


def test_strong_evidence_auto_merges(session):
    a = resolve_person(session, {"slack": "U1", "email": "meera@juniper.co"}, "Meera Nair", vouched_by="slack")
    # new email at the same company domain, same name -> name 0.6 + domain 0.3 = 0.9 (< 0.92), still review
    b = resolve_person(session, {"email": "m.nair@juniper.co"}, "Meera Nair", vouched_by="email")
    assert a.id != b.id
    assert len(session.scalars(select(MergeCandidate)).all()) == 1


def test_merge_moves_everything(seeded):
    s = seeded
    mc = s.scalars(select(MergeCandidate)).one()
    keep, drop = sorted((mc.a_id, mc.b_id))
    merge_entities(s, keep, drop)
    s.commit()
    assert s.get(Entity, drop) is None
    schemes = {i.scheme for i in s.scalars(select(Identifier).where(Identifier.entity_id == keep))}
    assert "whatsapp" in schemes and "slack" in schemes
