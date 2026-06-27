"""Shared datetime parsing and user-facing display helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_DISPLAY_TIMEZONE = "America/New_York"


def parse_utc_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp to UTC. Returns None when value is empty or invalid."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def format_user_datetime(
    value: Any,
    preferred_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
) -> str:
    """Format a stored UTC timestamp for display in the user's timezone."""
    if value is None or str(value).strip() == "":
        return "Not recorded"

    parsed = parse_utc_datetime(value)
    if parsed is None:
        return "Not recorded"

    tz_name = str(preferred_timezone or DEFAULT_DISPLAY_TIMEZONE).strip() or DEFAULT_DISPLAY_TIMEZONE
    try:
        user_tz = ZoneInfo(tz_name)
    except Exception:
        user_tz = ZoneInfo(DEFAULT_DISPLAY_TIMEZONE)
        tz_name = DEFAULT_DISPLAY_TIMEZONE

    local_dt = parsed.astimezone(user_tz)
    return local_dt.strftime("%b %d, %Y, %I:%M %p %Z").replace(", 0", ", ", 1)
