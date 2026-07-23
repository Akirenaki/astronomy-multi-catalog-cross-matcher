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


async def check_limit(
    subject_type: str,
    subject_id: str,
    *,
    limit: int | None = None,
    window: timedelta | None = None,
) -> None:
    """Raise RateLimitExceededError if (subject_type, subject_id) has already hit
    `limit` requests within `window`; otherwise return without writing anything.

    This is the read-only half of what used to be check_and_record(). It exists
    so a caller can check *before* attempting a Gemini call and only record the
    attempt afterwards -- see record_usage() -- rather than unconditionally
    writing a rate_limit_events row for every attempt regardless of whether
    Gemini actually returned a summary.

    limit/window default to the module-level constants above, looked up at call
    time (not bound as early parameter defaults) so they can be overridden -- e.g.
    by tests, or a future admin/config override -- without needing every caller to
    pass them through explicitly.
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


async def record_usage(subject_type: str, subject_id: str) -> None:
    """Write one rate_limit_events row for (subject_type, subject_id).

    Callers must only call this *after* a Gemini generation call has actually
    succeeded -- see the module docstring and app.main's summary routes, which
    call check_limit() before attempting generation and record_usage() only once
    generate_summary() has returned without raising.
    """
    async with SessionLocal() as session:
        session.add(
            RateLimitEvent(subject_type=subject_type, subject_id=subject_id, created_at=datetime.now(timezone.utc))
        )
        await session.commit()


async def check_and_record(
    subject_type: str,
    subject_id: str,
    *,
    limit: int | None = None,
    window: timedelta | None = None,
) -> None:
    """Check and record in one call. Kept for callers (and existing tests) that
    want the original all-or-nothing behaviour -- e.g. a generic per-request
    limiter that isn't gated on a downstream call succeeding. app.main's AI
    summary routes no longer use this directly; they use check_limit() /
    record_usage() separately so a failed Gemini call doesn't consume quota.

    NOTE: because the check and the write are two separate awaited calls, this
    (like check_limit()+record_usage() called back to back) is not fully immune
    to a race between two concurrent requests from the same subject both passing
    the check before either has recorded -- the existing implementation had the
    same property. This is judged an acceptable trade-off for a soft per-client
    throttle, not a hard security boundary.
    """
    await check_limit(subject_type, subject_id, limit=limit, window=window)
    await record_usage(subject_type, subject_id)
