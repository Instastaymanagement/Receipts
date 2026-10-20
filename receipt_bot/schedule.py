"""9 pm Toronto gate.

GitHub cron runs in UTC and Toronto moves between UTC-4 and UTC-5, so the
workflow fires at both 01:00 and 02:00 UTC. Only the one that lands on 9 pm
local time that day does any work.
"""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import RUN_HOUR_LOCAL, TIMEZONE


def should_run(event_name: str, cron: str, now_utc: datetime | None = None) -> bool:
    if event_name != "schedule":
        return True  # manual "Run workflow" always runs
    m = re.match(r"^\s*\d+\s+(\d+)\s", cron or "")
    if not m:
        return True
    cron_hour = int(m.group(1))
    now_utc = now_utc or datetime.now(timezone.utc)
    # GitHub can start a scheduled job late; anchor to the hour it was scheduled for.
    scheduled = now_utc.replace(hour=cron_hour, minute=0, second=0, microsecond=0)
    return scheduled.astimezone(ZoneInfo(TIMEZONE)).hour == RUN_HOUR_LOCAL
