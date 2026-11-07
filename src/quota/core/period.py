from datetime import datetime, timezone

def get_current_period(now: datetime | None = None) -> str:
    """
    Returns the UTC billing period in 'YYYY-MM' format.
    A billing period is the UTC calendar month (DESIGN.md Section 4.4).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.strftime("%Y-%m")

def get_next_reset_time(now: datetime | None = None) -> datetime:
    """
    Returns the start of the next billing period (00:00:00 UTC on the 1st of next month).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    if now.month == 12:
        return datetime(now.year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    else:
        return datetime(now.year, now.month + 1, 1, 0, 0, 0, tzinfo=timezone.utc)
