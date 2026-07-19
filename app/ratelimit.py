"""Decision D: real rate limiting on Gemini-quota-spending actions.

This is deliberately a *different* layer from Decision B's AI_SUMMARY_COOLDOWN in
app/cache.py, not a replacement for it (the original handoff doc suggested D would
"replace" B; on inspection that's not quite right -- they guard against different
things and both remain useful):

- Decision B (per-object cooldown): stops rapid double-clicking Generate/Regenerate
  on the *same* object. Global cache, so it does nothing to stop someone clicking
  Generate across many *different* objects.
- Decision D (this module, per-client limit): stops a single client -- one logged-in
  user, or one anonymous browser session -- from spending Gemini quota across many
  *different* objects in a short window. This is the gap Decision B's own docstring
  already flagged as out of scope for it.

Both checks run on every Generate/Regenerate request; either can reject it.

Storage: an event-log table (RateLimitEvent) rather than a counter column, so the
same schema handles both "user" and "session" subjects uniformly and a sliding
window can be computed by counting rows, not by resetting a counter on a timer
(which would need a background job this small app has no infrastructure for).

The limit chosen below (20 requests/hour/client) is a starting placeholder, not a
measured figure -- there's no traffic data yet to size it against, since hosting is
still postponed. It's defined as a single constant specifically so it's easy to
revisit once real usage patterns exist.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import RateLimitEvent

AI_SUMMARY_RATE_LIMIT = 20
AI_SUMMARY_RATE_LIMIT_WINDOW = timedelta(hours=1)


class RateLimitExceededError(Exception):
    """Raised when a subject (user or session) has exceeded the allowed number of
    Gemini-quota-spending requests within the current window."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limit exceeded; retry after {retry_after_seconds}s")


async def check_and_record(
    subject_type: str,
    subject_id: str,
    *,
    limit: int | None = None,
    window: timedelta | None = None,
) -> None:
    """Record one request for (subject_type, subject_id), raising
    RateLimitExceededError instead if that would exceed `limit` within `window`.

    limit/window default to the module-level constants above, looked up at call
    time (not bound as early parameter defaults) so they can be overridden -- e.g.
    by tests, or a future admin/config override -- without needing every caller to
    pass them through explicitly.

    The check and the record happen in the same call so callers can't accidentally
    check without recording (which would make the limit unenforceable) or record
    without checking (which would make it pointless).
    """
    if limit is None:
        limit = AI_SUMMARY_RATE_LIMIT
    if window is None:
        window = AI_SUMMARY_RATE_LIMIT_WINDOW

    now = datetime.now(timezone.utc)
    window_start = now - window

    async with SessionLocal() as session:
        count_result = await session.execute(
            select(func.count()).where(
                RateLimitEvent.subject_type == subject_type,
                RateLimitEvent.subject_id == subject_id,
                RateLimitEvent.created_at > window_start,
            )
        )
        count = count_result.scalar_one()

        if count >= limit:
            oldest_result = await session.execute(
                select(RateLimitEvent.created_at)
                .where(
                    RateLimitEvent.subject_type == subject_type,
                    RateLimitEvent.subject_id == subject_id,
                    RateLimitEvent.created_at > window_start,
                )
                .order_by(RateLimitEvent.created_at.asc())
                .limit(1)
            )
            oldest = oldest_result.scalar_one_or_none()
            if oldest is not None:
                if oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=timezone.utc)
                retry_after = (oldest + window) - now
                retry_after_seconds = max(1, int(retry_after.total_seconds()))
            else:
                retry_after_seconds = int(window.total_seconds())
            raise RateLimitExceededError(retry_after_seconds=retry_after_seconds)

        session.add(RateLimitEvent(subject_type=subject_type, subject_id=subject_id, created_at=now))
        await session.commit()
