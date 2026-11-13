"""Plugin-owned scheduler: no private AstrBot cron handlers or orphan cron rows."""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import SlotConfig


def scheduled_time(slot: SlotConfig, now: datetime) -> datetime:
    h, m = slot.hour_minute
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def is_due(slot: SlotConfig, now: datetime, catchup_minutes: int) -> bool:
    """Window=0 still executes in the scheduled minute; never replays old dates."""
    delta = (now - scheduled_time(slot, now)).total_seconds()
    return 0 <= delta < max(60, catchup_minutes * 60)


def next_time(slot: SlotConfig, now: datetime) -> datetime:
    value = scheduled_time(slot, now)
    if value <= now:
        value += timedelta(days=1)
    return value
