<div align="center">

# Kept

**Every promise your team makes, kept.**

Kept is an open-source Slack bot that catches the commitments people make in chat,<br>
links each one to the ticket that fulfils it, and nudges the owner before it slips.

[![CI](https://github.com/keshava3112/kept/actions/workflows/ci.yml/badge.svg)](https://github.com/keshava3112/kept/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![Postgres or SQLite](https://img.shields.io/badge/db-SQLite%20%7C%20Postgres-336791)
![MCP](https://img.shields.io/badge/MCP-server-black)

<img src="docs/assets/demo.gif" alt="Animation: a promise made in Slack is caught by Kept, linked to a Linear ticket, goes overdue, triggers a nudge, and is marked kept after one reply" width="860">

</div>

---

Most work that slips was never a ticket. It was a sentence in a thread:
*"I'll send the revised SOW by Friday."* Everyone saw it, nobody filed it, and
on Monday nobody remembers who owed what.

Kept reads the channels you invite it to and turns those sentences into tracked
commitments: who promised, what, to whom, by when, plus **the exact words as
a receipt**. When a ticket exists for the work, Kept links the promise to it and
follows the ticket to done. When nothing exists, Kept still tracks the promise
and nudges the owner before it's due. Every Monday it posts who kept their word.

No new app to learn and nothing to file. You keep talking the way you already do.

### What your team sees

Every Monday, a brief in the channel of your choice. During the week, a DM
to whoever owes something, before it's late rather than after.

<p align="center"><img src="docs/assets/hero.svg" alt="Kept posting a weekly brief of overdue promises in Slack, and nudging an owner by DM" width="820"></p>

## Try it in 30 seconds

No Slack workspace or API keys needed. The demo replays a week at a small
studio through the real pipeline:

```bash
git clone https://github.com/keshava3112/kept && cd kept
pip install -e ".[dev]"
python -m kept.cli demo
```

You'll see the Monday brief, the DM nudges, the identity-review queue and a
grounded answer to *"where does Kelso stand?"*. Run `pytest` to see the
32-test suite.

## How it works

```mermaid
flowchart LR
    S[Slack] --> L[(Event log<br/>append-only)]
    T[Linear] --> L
    L --> P[Projector]
    P --> G["Spine<br/>people · projects · tasks"]
    P --> TL["Timelines<br/>status · assignee"]
    P --> C["Commitments<br/>state machine + receipts"]
    C --> N[Nudges · Monday brief]
    G & TL & C --> D["Reading desk<br/>@kept questions"]
    G & TL & C --> M[MCP server]
```

1. **Everything is logged first.** Every Slack message and every *version* of a
   Linear issue lands in an append-only event log. Everything else is a
   projection of that log, so `kept replay` rebuilds the whole state after you
   improve a prompt or a rule. There are no migrations of derived data.
2. **Cheap things stay cheap.** A regex prefilter skips most chat for free. A
   small model extracts promises from what's left. A frontier model only runs
   when someone asks the bot a question.
3. **No receipt, no record.** A promise is stored only if its quote appears
   verbatim in the message. The model can propose; it can't invent.
4. **Tools are trusted for what they already know.** Tasks, projects and
   assignees come straight from Linear as facts. The model is never asked to
   re-derive them from chat.
5. **Identity merges are careful.** Your Slack handle, Linear account and email
   merge automatically when they share an identifier. Two people who merely
   share a name go to a human review queue, because one wrong merge poisons
   every answer about both.

## Commands

DM the bot or @mention it in a thread:

| Command | What it does |
|---|---|
| `mine` / `open` | Your open promises / everyone's |
| `done 12` / `drop 12` | Close a promise |
| `push 12 to friday` | Renegotiate the date (it stays on the record) |
| `brief` | The weekly brief, right now |
| `merges` / `merge 3 yes` | Settle "is this the same person?" |
| anything else | Answered from the graph, citing the source messages |

## Run it for your team

1. **Create the Slack app.** Go to [api.slack.com/apps](https://api.slack.com/apps),
   choose *Create New App* → *From a manifest*, and paste
   [`slack-manifest.yml`](slack-manifest.yml). Generate an app-level token with
   `connections:write`, then install the app to your workspace.
2. **Configure.** Run `cp .env.example .env` and fill in `SLACK_BOT_TOKEN`,
   `SLACK_APP_TOKEN`, `ANTHROPIC_API_KEY`, `LINEAR_API_KEY` and
   `BRIEF_CHANNEL`.
3. **Run.** `python -m kept.cli run`, then invite `@kept` to the channels it
   should read.

It uses Slack's Socket Mode, so it runs from a laptop or a small VM with no
public URL. A background scheduler syncs Linear every 10 minutes, sends nudges
hourly during working hours, and posts the brief on Monday mornings.

<details>
<summary><b>Postgres, models and other options</b></summary>

- **Postgres:** `docker compose up -d`, set
  `DATABASE_URL=postgresql+psycopg://kept:kept@localhost:5432/kept`, then
  `pip install -e ".[postgres]"`. SQLite is fine for a team of about 20.
- **Models:** `REFLEX_MODEL` handles extraction and `DESK_MODEL` answers
  questions. Without `ANTHROPIC_API_KEY`, Kept falls back to a deterministic
  rule extractor, and the desk returns the raw context it found.
- **Timezone:** `KEPT_TZ` (default `Asia/Kolkata`) decides what "by Friday"
  means and when the brief goes out.
- **Thresholds:** merge, link and nudge thresholds are environment variables;
  see [`kept/config.py`](kept/config.py).

</details>

## Use the graph from Claude Code (MCP)

```bash
claude mcp add kept -- python -m kept.cli mcp
```

This exposes `search`, `get_entity`, `open_commitments`, `commitment_history`,
`get_receipt` and `ask`. Your coding agent can then answer questions like
"what did we promise Juniper, and is the ticket moving?" from your team's real context.

## Roadmap

Kept is an early MVP. These are the next pieces, and good places to contribute:

- [ ] Email ingest (inbound-parse webhook): the schema already supports it
- [ ] WhatsApp via the official Cloud API
- [ ] Meeting transcripts as a source, so promises made on calls are caught too
- [ ] Renegotiation from chat: *"can we push the SOW to Monday?"* → proposed `push`
- [ ] Permission-aware answers in group channels
- [ ] Jira, ClickUp and GitHub Issues alongside Linear
- [ ] Write-back: comment on the linked ticket, or create one for unlinked promises
- [ ] Multi-workspace (hosted) mode

See [CONTRIBUTING.md](CONTRIBUTING.md) to get set up. Issues labelled
`good first issue` are scoped to an afternoon.

## Project layout

```
kept/
  db.py           schema: event log, spine, timelines, commitments
  projector.py    events → projections, replay
  extract.py      prefilter, rule and LLM extractors, date parsing, receipt gate
  resolve.py      identity resolution, dedupe by who's involved, merges
  commitments.py  state machine, ticket linking, overdue detection
  reflexes.py     nudges, weekly brief, promise-keeping stats
  commands.py     done / push / drop / mine / merges
  desk.py         graph-walk retrieval and grounded answers
  mcp_server.py   the graph over MCP
  ingest/         Slack (Socket Mode) and Linear (GraphQL)
scripts/          make_demo_gif.py renders the README animation
tests/            rules, state machine, linking, replay, Linear sync, MCP
```

## Acknowledgements

The context-graph design (trust structured sources, keep identity separate from
time, resolve duplicates by who's involved) builds on Canvas Labs' note
[*The future of AI runs on a context graph*](https://canvasx.ai/research/context-graphs-for-real-organizations).

## License

[MIT](LICENSE). If Kept catches a promise your team would have dropped, a ⭐ helps others find it.
