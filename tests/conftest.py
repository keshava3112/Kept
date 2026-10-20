import os

import pytest

from kept import demo
from kept.db import Base, make_engine, make_session_factory
from kept.extract import RuleExtractor

# Run the suite against Postgres too:
#   KEPT_TEST_DB=postgresql+psycopg://kept:kept@localhost:5432/kept pytest
TEST_DB = os.getenv("KEPT_TEST_DB")


@pytest.fixture
def sf(tmp_path):
    if TEST_DB:
        engine = make_engine(TEST_DB)
        Base.metadata.drop_all(engine)
        engine.dispose()
        return make_session_factory(TEST_DB)
    return make_session_factory(f"sqlite:///{tmp_path / 't.db'}")


@pytest.fixture
def session(sf):
    with sf() as s:
        yield s


@pytest.fixture
def seeded(session):
    demo.run(session, RuleExtractor())
    return session


@pytest.fixture
def now():
    return demo.utc(demo.NOW)
