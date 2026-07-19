"""Per-client rate limiting for Gemini summary generation.

This complements the per-object cooldown in app.cache: the cooldown blocks rapid
repeat clicks on one object, while this module blocks one client from generating
summaries across many different objects in a short window. The implementation uses
an event log so the sliding window can be counted directly.
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
