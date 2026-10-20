# Contributing to Kept

Thanks for helping. Kept is small on purpose, so the bar is: easy to read,
tested, and honest about what it knows.

## Setup

```bash
git clone https://github.com/keshava3112/kept && cd kept
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                       # 32 tests, a few seconds
python -m kept.cli demo      # the whole pipeline on fixture data
```

To run the suite against Postgres:

```bash
docker compose up -d
pip install -e ".[postgres]"
KEPT_TEST_DB=postgresql+psycopg://kept:kept@localhost:5432/kept pytest
```

## How the code fits together

Everything starts as a row in `raw_events` (the append-only log).
`projector.py` turns events into the spine (entities, identifiers, edges),
timelines and commitments. All of those are rebuildable with `kept replay`,
so **never write derived state anywhere except through the projector.**

## Ground rules

- **Every derived row keeps its receipt** (`source_event_id`). If a feature
  can't say where a fact came from, it isn't ready.
- **The model proposes, gates decide.** New extraction logic goes through a
  gate (confidence, verbatim quote, or a structured source vouching for it).
- **Rules come with tests.** Each rule in `resolve.py` and `commitments.py` has
  a test named after it. If you fix a bug, add the test that would have caught it.
- **Keep Lane 1 free of model calls.** Nudges, briefs and commands are
  deterministic. Spend the model only where text is genuinely ambiguous.

## Adding a source (email, WhatsApp, Jira, …)

1. Write an ingester in `kept/ingest/` that calls `record_event(...)`, with a
   stable `external_id` so re-ingestion is a no-op.
2. Messages use `kind="message"`. Put `user_name`, `user_email` and
   `user_handle` in `payload` so identity resolution can work.
3. Structured records use `kind="task"`, or a new kind with its own
   `project_*` function in `projector.py`.
4. Add a fixture-driven test like `tests/test_linear_sync.py`.

## Pull requests

- Keep them focused, and describe the behaviour change, not just the diff.
- `pytest` must pass, and `python -m kept.cli demo` must still run.
- Use a draft PR to discuss an approach before building it out.
