from __future__ import annotations

from datetime import datetime, timedelta, timezone


def effective_expiry(*, last_activity_at: datetime, retention_days: int) -> datetime:
    """Return the UTC expiry boundary used by a cleanup worker."""
    if not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650")
    if last_activity_at.tzinfo is None:
        last_activity_at = last_activity_at.replace(tzinfo=timezone.utc)
    return last_activity_at.astimezone(timezone.utc) + timedelta(days=retention_days)


def should_cleanup(*, expires_at: datetime | None, cleanup_enabled: bool, now: datetime | None = None) -> bool:
    if not cleanup_enabled or expires_at is None:
        return False
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return expires_at.astimezone(timezone.utc) <= current
