"""kept — command line.

  python -m kept.cli demo               # a seeded week, no keys needed
  python -m kept.cli run                # Slack bot + scheduler (Linear sync, nudges, Monday brief)
  python -m kept.cli sync-linear
  python -m kept.cli nudge [--dry]
  python -m kept.cli brief
  python -m kept.cli ask "where does Kelso stand?"
  python -m kept.cli open | merges
  python -m kept.cli replay             # rebuild every projection from the event log
  python -m kept.cli mcp                # serve the graph over MCP (stdio)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import settings
from .db import make_session_factory


def _extractor():
    from .extract import default_extractor

    return default_extractor()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kept", description="Kept: every promise your team makes, kept.")
    ap.add_argument("cmd", choices=["demo", "run", "sync-linear", "nudge", "brief", "ask", "open", "merges", "replay", "mcp"])
    ap.add_argument("text", nargs="*")
    ap.add_argument("--db", help="database URL (default from DATABASE_URL)")
    ap.add_argument("--dry", action="store_true", help="print instead of sending to Slack")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.cmd == "run" else logging.WARNING)

    if args.cmd == "demo":
        return _demo()

    sf = make_session_factory(args.db)

    if args.cmd == "mcp":
        from .mcp_server import build_server

        build_server(sf).run("stdio")
        return 0

    if args.cmd == "run":
        return _run(sf)

    with sf() as s:
        if args.cmd == "sync-linear":
            from .ingest.linear_sync import sync_linear
            from .projector import process_pending

            n = sync_linear(s)
            m = process_pending(s, _extractor())
            print(f"pulled {n} issue versions, projected {m} events")
        elif args.cmd == "nudge":
            from .reflexes import ConsoleNotifier, SlackNotifier, run_nudges

            notifier = ConsoleNotifier() if args.dry or not settings.slack_bot_token else _slack_notifier()
            print(f"nudged {len(run_nudges(s, notifier))}")
        elif args.cmd == "brief":
            from .reflexes import weekly_brief

            print(weekly_brief(s))
        elif args.cmd == "ask":
            from .desk import answer

            print(answer(s, " ".join(args.text)))
        elif args.cmd in ("open", "merges"):
            from .commands import handle_command

            print(handle_command(s, args.cmd, None))
        elif args.cmd == "replay":
            from .projector import replay

            print(f"replayed {replay(s, _extractor())} events")
    return 0


def _slack_notifier():
    from slack_sdk import WebClient

    from .reflexes import SlackNotifier

    return SlackNotifier(WebClient(token=settings.slack_bot_token))


def _demo() -> int:
    from . import demo
    from .commands import handle_command
    from .desk import answer
    from .extract import RuleExtractor
    from .reflexes import ConsoleNotifier, run_nudges, weekly_brief

    path = "demo.db"
    if os.path.exists(path):
        os.remove(path)
    sf = make_session_factory(f"sqlite:///{path}")
    now = demo.utc(demo.NOW)
    with sf() as s:
        demo.run(s, RuleExtractor())
        bar = "─" * 70
        print(f"{bar}\nWEEKLY BRIEF (posted to #ops, Monday 9am)\n{bar}")
        print(weekly_brief(s, now))
        print(f"\n{bar}\nNUDGES (DMs)\n{bar}", end="")
        run_nudges(s, ConsoleNotifier(), now)
        print(f"\n{bar}\nIDENTITY REVIEW\n{bar}")
        print(handle_command(s, "merges", None, now))
        print(f"\n{bar}\nASK: where does Kelso stand?\n{bar}")
        print(answer(s, "where does Kelso stand?"))
    print(f"\n(demo database written to {path}; try: python -m kept.cli --db sqlite:///{path} open)")
    return 0


def _run(sf) -> int:
    import threading

    from apscheduler.schedulers.background import BackgroundScheduler

    from .ingest.slack_app import DB_LOCK, run_slack
    from .reflexes import run_nudges, weekly_brief

    notifier = _slack_notifier()
    sched = BackgroundScheduler(timezone=settings.timezone)

    def linear_job():
        if not settings.linear_api_key:
            return
        from .ingest.linear_sync import sync_linear
        from .projector import process_pending

        with DB_LOCK, sf() as s:
            sync_linear(s)
            process_pending(s, _extractor())

    def nudge_job():
        with DB_LOCK, sf() as s:
            run_nudges(s, notifier)

    def brief_job():
        if settings.brief_channel:
            with DB_LOCK, sf() as s:
                notifier.post(settings.brief_channel, weekly_brief(s))

    sched.add_job(linear_job, "interval", minutes=10, id="linear", max_instances=1)
    sched.add_job(nudge_job, "cron", minute=7, hour="9-20", day_of_week="mon-sat", id="nudges")
    sched.add_job(brief_job, "cron", day_of_week="mon", hour=8, minute=53, id="brief")
    sched.start()
    threading.Thread(target=linear_job, daemon=True).start()
    run_slack(sf)  # blocks
    return 0


if __name__ == "__main__":
    sys.exit(main())
