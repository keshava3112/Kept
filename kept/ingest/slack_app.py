"""Slack surface (Socket Mode: no public URL needed).

Bot scopes: app_mentions:read, channels:history, groups:history, im:history,
chat:write, im:write, users:read, users:read.email, channels:read, groups:read
Events: message.channels, message.groups, message.im, app_mention

Every channel message the bot can see goes into the event log and through the
projector. @mentions and DMs are answered: commands first, reading desk second.
It only ever replies in the thread it was asked in.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from functools import lru_cache

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from ..commands import handle_command
from ..config import settings
from ..desk import answer
from ..extract import default_extractor
from ..projector import process_pending, record_event
from ..reflexes import slack_id_for

log = logging.getLogger(__name__)
DB_LOCK = threading.Lock()  # SQLite-friendly; harmless on Postgres


def build_app(session_factory, extractor=None) -> App:
    app = App(token=settings.slack_bot_token)
    extractor = extractor or default_extractor()

    @lru_cache(maxsize=2048)
    def profile(user_id: str) -> tuple[str, str | None, str | None]:
        u = app.client.users_info(user=user_id)["user"]
        p = u.get("profile", {})
        return (p.get("real_name") or u.get("real_name") or u.get("name"), p.get("email"), u.get("name"))

    @lru_cache(maxsize=1024)
    def channel_name(channel_id: str) -> str:
        try:
            c = app.client.conversations_info(channel=channel_id)["channel"]
            return "DM" if c.get("is_im") else f"#{c.get('name', channel_id)}"
        except Exception:
            return channel_id

    def ingest(event: dict) -> None:
        name, email, handle = profile(event["user"])
        with DB_LOCK, session_factory() as s:
            record_event(
                s,
                source="slack",
                kind="message",
                external_id=f"{event['channel']}:{event['ts']}",
                occurred_at=datetime.fromtimestamp(float(event["ts"]), tz=timezone.utc).replace(tzinfo=None),
                text=event.get("text", ""),
                channel=channel_name(event["channel"]),
                author=event["user"],
                payload={
                    "user_name": name,
                    "user_email": email,
                    "user_handle": handle,
                    "thread_ts": event.get("thread_ts"),
                    "channel_id": event["channel"],
                },
            )
            s.commit()
            process_pending(s, extractor)

    def reply_to(text: str, user: str) -> str:
        with DB_LOCK, session_factory() as s:
            asker = slack_id_for(s, user)
            out = handle_command(s, text, asker)
            if out is None:
                out = answer(s, text)
            return out

    @app.event("message")
    def on_message(event, say):
        if event.get("subtype") or event.get("bot_id") or "user" not in event:
            return
        if event.get("channel_type") == "im":
            ingest(event)  # so the bot knows who you are
            say(text=reply_to(event.get("text", ""), event["user"]))
            return
        ingest(event)

    @app.event("app_mention")
    def on_mention(event, say):
        text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip()
        say(text=reply_to(text, event["user"]), thread_ts=event.get("thread_ts") or event["ts"])

    return app


def run_slack(session_factory) -> None:
    if not (settings.slack_bot_token and settings.slack_app_token):
        raise RuntimeError("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN")
    app = build_app(session_factory)
    SocketModeHandler(app, settings.slack_app_token).start()
