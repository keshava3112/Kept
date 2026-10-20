"""Runtime configuration, read from environment (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass

try:  # optional
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///kept.db")
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    # Lane 1 (reflexes: cheap, constant) and Lane 2 (reading desk: rare, best)
    reflex_model: str = os.getenv("REFLEX_MODEL", "claude-haiku-5-5")
    desk_model: str = os.getenv("DESK_MODEL", "claude-sonnet-5-5")

    slack_bot_token: str | None = os.getenv("SLACK_BOT_TOKEN")
    slack_app_token: str | None = os.getenv("SLACK_APP_TOKEN")  # socket mode
    brief_channel: str | None = os.getenv("BRIEF_CHANNEL")  # e.g. C0123 (#ops)

    linear_api_key: str | None = os.getenv("LINEAR_API_KEY")

    timezone: str = os.getenv("KEPT_TZ", "Asia/Kolkata")

    # Gates and thresholds
    min_commitment_confidence: float = _f("MIN_COMMITMENT_CONFIDENCE", 0.6)
    auto_merge_threshold: float = _f("AUTO_MERGE_THRESHOLD", 0.92)
    review_merge_threshold: float = _f("REVIEW_MERGE_THRESHOLD", 0.55)
    link_threshold: float = _f("LINK_THRESHOLD", 0.55)
    nudge_window_hours: float = _f("NUDGE_WINDOW_HOURS", 24)
    nudge_cooldown_hours: float = _f("NUDGE_COOLDOWN_HOURS", 20)


settings = Settings()
