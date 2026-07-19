"""Cache management for astronomical object resolution results."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import ObjectRecord, IdentifierRecord, PlanetRecord, SavedSearch, UserSummarySnapshot
from app.narrative import generate_summary
from app.resolver import ResolutionResult, resolve_query
from app.catalogs.simbad import normalize_query

logger = logging.getLogger(__name__)

# Per-object cooldown for POST /object/{id}/summary/regenerate. It only prevents
# rapid repeat clicks on the same object; per-client throttling is handled separately.
AI_SUMMARY_COOLDOWN = timedelta(minutes=5)


class CooldownActiveError(Exception):
    """Raised by regenerate_ai_summary() when called again before AI_SUMMARY_COOLDOWN
    has elapsed since the object's last generation. Callers must handle this
    explicitly rather than treating a no-op and a fresh regeneration the same way."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Cooldown active; retry after {retry_after_seconds}s")


async def get_cached(query_text: str) -> ObjectRecord | None:
    """Return a cached object record when the normalized query is still fresh."""
    normalized_query = normalize_query(query_text)
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord)
            # Eagerly load these relationships while the session is still open. Both
            # result.html and ObjectRecord.to_dict() read .identifiers/.planets, but this
            # session closes as soon as this function returns -- without eager loading here,
            # touching either attribute afterward raises DetachedInstanceError (the "Internal
            # Server Error" seen on the /search and /object pages).
            .options(selectinload(ObjectRecord.identifiers), selectinload(ObjectRecord.planets))
            .where(
                ObjectRecord.query_text == (normalized_query or query_text),
                ObjectRecord.expires_at > datetime.now(timezone.utc),
            )
        )
        return result.scalar_one_or_none()


async def store_result(resolution_result: ResolutionResult, *, generate_ai_summary: bool = True) -> ObjectRecord:
    """Persist a resolution result and any associated identifiers or planets to the database."""
    async with SessionLocal() as session:
        # Reuse an existing row when this query or its canonical SIMBAD ID already exists.
        candidate_rows: list[ObjectRecord] = []

        by_query_text = await session.execute(
            select(ObjectRecord).where(ObjectRecord.query_text == resolution_result.query_text)
        )
        row = by_query_text.scalar_one_or_none()
        if row is not None:
            candidate_rows.append(row)

        if resolution_result.main_id:
            by_main_id = await session.execute(
                select(ObjectRecord).where(ObjectRecord.simbad_main_id == resolution_result.main_id)
            )
            row = by_main_id.scalar_one_or_none()
            if row is not None and row not in candidate_rows:
                candidate_rows.append(row)

        # Delete any matching stale rows before inserting the refreshed record.
        for stale_row in candidate_rows:
            await session.delete(stale_row)
        if candidate_rows:
            await session.flush()

        # Serialize the ambiguous candidate list so it can be restored later without re-querying SIMBAD.
        candidates_json = (
            json.dumps(resolution_result.candidates) if resolution_result.candidates else None
        )
        resolved_via_json = (
            json.dumps(resolution_result.resolved_via) if resolution_result.resolved_via else None
        )

        record = ObjectRecord(
            query_text=resolution_result.query_text,
            simbad_main_id=resolution_result.main_id,
            ra_deg=resolution_result.ra,
            dec_deg=resolution_result.dec,
            otype=resolution_result.otype,
            spectral_type=resolution_result.spectral_type,
            resolution_state=resolution_result.state,
            candidates_json=candidates_json,
            resolved_via_json=resolved_via_json,
            resolved_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        )
        session.add(record)
        await session.flush()

        # Unresolved, ambiguous, and failed-lookup requests should expire quickly so
        # later fixes -- or simply a working network connection -- can be picked up
        # without waiting out the full 14-day TTL used for confirmed results.
        if resolution_result.state in ("UNRESOLVED", "AMBIGUOUS", "LOOKUP_FAILED"):
            record.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        # Store each alias reported by SIMBAD as an identifier row.
        for alias in resolution_result.aliases:
            session.add(
                IdentifierRecord(
                    object_id=record.id,
                    catalog="SIMBAD",
                    identifier=alias,
                    matched_exoplanet_archive=alias == resolution_result.matched_alias,
                )
            )

        # Store planet rows when the object has known exoplanets.
        for planet in resolution_result.planets:
            session.add(
                PlanetRecord(
                    object_id=record.id,
                    pl_name=planet.get("pl_name", ""),
                    pl_letter=planet.get("pl_letter"),
                    orbital_period_days=planet.get("orbital_period_days"),
                    planet_radius_earth=planet.get("planet_radius_earth"),
                    discovery_year=planet.get("discovery_year"),
                    discovery_method=planet.get("discovery_method"),
                )
            )

        # Skip AI summaries for unresolved, ambiguous, and failed-lookup results.
        if generate_ai_summary and resolution_result.state not in ("UNRESOLVED", "AMBIGUOUS", "LOOKUP_FAILED"):
            summary_payload = {
                "main_id": resolution_result.main_id,
                "spectral_type": resolution_result.spectral_type,
                "planet_count": len(resolution_result.planets),
                "planets": resolution_result.planets,
            }
            record.ai_summary = await generate_summary(summary_payload)

        commit_started_at = time.perf_counter()
        try:
            await session.commit()
        except IntegrityError:
            logger.info("Database commit stage: failed (IntegrityError) after %.3fs", time.perf_counter() - commit_started_at)
                # Another request won the race to insert this object; reuse that row.
            await session.rollback()
            fallback_query = select(ObjectRecord).options(
                selectinload(ObjectRecord.identifiers), selectinload(ObjectRecord.planets)
            )
            if resolution_result.main_id:
                fallback_query = fallback_query.where(ObjectRecord.simbad_main_id == resolution_result.main_id)
            else:
                fallback_query = fallback_query.where(ObjectRecord.query_text == resolution_result.query_text)
            result = await session.execute(fallback_query)
            winner = result.scalars().first()
            if winner is not None:
                return winner
            # Re-raise if the conflicting row disappeared before we could fetch it.
            raise
        logger.info("Database commit stage: completed in %.3fs", time.perf_counter() - commit_started_at)

        # session.refresh() only reloads column attributes, not relationships, so a plain
        # refresh() here still leaves .identifiers/.planets unloaded and detached once the
        # session closes below. Re-fetch the row with the same eager-loading options used
        # elsewhere in this module so the returned record is safe to read from afterward.
        result = await session.execute(
            select(ObjectRecord)
            .options(selectinload(ObjectRecord.identifiers), selectinload(ObjectRecord.planets))
            .where(ObjectRecord.id == record.id)
        )
        return result.scalar_one()


async def get_or_resolve(query_text: str, *, generate_ai_summary: bool = True) -> ObjectRecord:
    """Serve a cached result when possible, otherwise resolve the query and store it.

    generate_ai_summary is forwarded to store_result() for a brand-new resolution; it
    has no effect on a cache hit, since get_cached() returns whatever was already
    persisted (summary present or not) without touching Gemini either way.
    """
    cached = await get_cached(query_text)
    if cached is not None:
        return cached

    result = await resolve_query(query_text)
    return await store_result(result, generate_ai_summary=generate_ai_summary)


async def ensure_ai_summary(simbad_main_id: str) -> str:
    """Generate (if not already cached) and persist the AI narrative for an
    already-resolved object, then return it.

    This is called lazily by result.html's client-side JS, via GET
    /object/{id}/summary, strictly *after* the main result page has already rendered
    with the scientific data -- mirroring the "results first, AI overview second"
    pattern search engines use, so a slow Gemini call (a live query for a
    heavily-catalogued star was observed taking ~42s for this call alone) never
    blocks the page the user is actually waiting on.

    Raises LookupError if no object exists for this SIMBAD main ID.
    """
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord)
            .options(selectinload(ObjectRecord.planets))
            .where(ObjectRecord.simbad_main_id == simbad_main_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise LookupError(f"No object found for simbad_main_id={simbad_main_id!r}")

        if record.ai_summary:
            # Already generated -- either by a previous call to this same function, or
            # by store_result() directly for any caller still using the blocking
            # default. Returning the cached value means repeated polls/page views
            # never re-spend Gemini quota on the same object.
            return record.ai_summary

        if record.resolution_state not in ("RESOLVED", "PARTIAL"):
            # Mirrors store_result()'s own skip condition: AMBIGUOUS/UNRESOLVED/
            # LOOKUP_FAILED have no confirmed structured data to summarize.
            return "No summary available."

        summary_payload = {
            "state": record.resolution_state,
            "main_id": record.simbad_main_id,
            "spectral_type": record.spectral_type,
            "planet_count": len(record.planets),
            "planets": [planet.to_dict() for planet in record.planets],
        }
        summary = await generate_summary(summary_payload)

        record.ai_summary = summary
        # Only set on an actual generation, not on the cache-hit early return above --
        # this timestamp is what regenerate_ai_summary()'s cooldown is measured from.
        record.ai_summary_generated_at = datetime.now(timezone.utc)
        await session.commit()
        return summary


async def regenerate_ai_summary(simbad_main_id: str) -> str:
    """Force-regenerate the AI narrative for an already-resolved object, subject to
    AI_SUMMARY_COOLDOWN.

    Unlike ensure_ai_summary(), this does NOT short-circuit on an existing
    ai_summary -- it always regenerates and overwrites it (only the latest summary
    is ever kept; no new row/table), unless the cooldown is still active, in which
    case it raises CooldownActiveError rather than silently no-op-ing or silently
    regenerating anyway, so the caller can tell the two outcomes apart.

    Raises LookupError if no object exists for this SIMBAD main ID.
    Raises CooldownActiveError if called again within AI_SUMMARY_COOLDOWN of the
    object's last generation.
    """
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord)
            .options(selectinload(ObjectRecord.planets))
            .where(ObjectRecord.simbad_main_id == simbad_main_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise LookupError(f"No object found for simbad_main_id={simbad_main_id!r}")

        if record.resolution_state not in ("RESOLVED", "PARTIAL"):
            # Mirrors ensure_ai_summary()'s own skip rule: AMBIGUOUS/UNRESOLVED/
            # LOOKUP_FAILED have no confirmed structured data to summarize, so
            # there's nothing meaningful to regenerate.
            return "No summary available."

        if record.ai_summary_generated_at is not None:
            generated_at = record.ai_summary_generated_at
            # SQLite's DateTime column round-trips aware datetimes as naive ones,
            # so a value written with datetime.now(timezone.utc) can come back
            # tzinfo=None. Re-attach UTC before subtracting from an aware "now" --
            # otherwise this comparison raises TypeError (or worse, silently
            # compares wall-clock time across a real timezone mismatch).
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=timezone.utc)
            elapsed = datetime.now(timezone.utc) - generated_at
            if elapsed < AI_SUMMARY_COOLDOWN:
                retry_after = AI_SUMMARY_COOLDOWN - elapsed
                raise CooldownActiveError(retry_after_seconds=max(1, int(retry_after.total_seconds())))

        summary_payload = {
            "state": record.resolution_state,
            "main_id": record.simbad_main_id,
            "spectral_type": record.spectral_type,
            "planet_count": len(record.planets),
            "planets": [planet.to_dict() for planet in record.planets],
        }
        summary = await generate_summary(summary_payload)

        record.ai_summary = summary
        record.ai_summary_generated_at = datetime.now(timezone.utc)
        await session.commit()
        return summary


async def get_object_by_simbad_id(simbad_main_id: str) -> ObjectRecord | None:
    """Look up a stored object by its SIMBAD main identifier, honoring the same TTL as /search
    so a bookmarked profile URL can't serve arbitrarily stale data forever."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord)
            .options(selectinload(ObjectRecord.identifiers), selectinload(ObjectRecord.planets))
            .where(
                ObjectRecord.simbad_main_id == simbad_main_id,
                ObjectRecord.expires_at > datetime.now(timezone.utc),
            )
        )
        return result.scalar_one_or_none()


async def list_recent_objects(limit: int = 10) -> list[ObjectRecord]:
    """Return the newest object records, ordered from most recent to oldest."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord).order_by(ObjectRecord.resolved_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def get_object_id_by_simbad_id(simbad_main_id: str) -> int | None:
    """Look up an ObjectRecord's primary key by SIMBAD id, ignoring TTL expiry.

    Favoriting/snapshotting an object shouldn't fail just because its cached
    scientific data is due for a refresh -- expires_at governs re-resolution
    freshness (see get_cached), not whether the row is allowed to exist. Mirrors
    the TTL-agnostic lookups already used by ensure_ai_summary/regenerate_ai_summary.
    """
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord.id).where(ObjectRecord.simbad_main_id == simbad_main_id)
        )
        return result.scalar_one_or_none()


async def add_favorite(user_id: int, object_id: int) -> SavedSearch:
    """Favorite an object for a user. Idempotent: favoriting an already-favorited
    object returns the existing row rather than raising, since re-clicking an
    already-active Favorite button is a normal UI interaction, not an error."""
    async with SessionLocal() as session:
        existing = await session.execute(
            select(SavedSearch).where(SavedSearch.user_id == user_id, SavedSearch.object_id == object_id)
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return row

        row = SavedSearch(user_id=user_id, object_id=object_id)
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent double-click race, same pattern as store_result()'s
            # candidate_rows handling: someone else's favorite for this exact pair
            # committed first. Treat it as success rather than a 500.
            await session.rollback()
            existing = await session.execute(
                select(SavedSearch).where(SavedSearch.user_id == user_id, SavedSearch.object_id == object_id)
            )
            return existing.scalar_one()
        await session.refresh(row)
        return row


async def remove_favorite(user_id: int, object_id: int) -> bool:
    """Unfavorite an object for a user. Returns True if a row was removed, False if
    it wasn't favorited in the first place (also not an error -- same idempotent
    reasoning as add_favorite)."""
    async with SessionLocal() as session:
        existing = await session.execute(
            select(SavedSearch).where(SavedSearch.user_id == user_id, SavedSearch.object_id == object_id)
        )
        row = existing.scalar_one_or_none()
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


async def is_favorited(user_id: int, object_id: int) -> bool:
    """Whether this user has already favorited this object -- drives whether
    result.html renders a Favorite or Unfavorite button."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(SavedSearch.id).where(SavedSearch.user_id == user_id, SavedSearch.object_id == object_id)
        )
        return result.scalar_one_or_none() is not None


async def list_favorites(user_id: int) -> list[dict]:
    """Return this user's favorited objects, newest favorite first, each paired with
    their personal summary snapshot if one exists (falling back to the shared
    canonical ai_summary otherwise -- see GET /account/saved's docstring in
    app/main.py for when that fallback applies)."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(SavedSearch)
            .options(
                selectinload(SavedSearch.object).selectinload(ObjectRecord.identifiers),
                selectinload(SavedSearch.object).selectinload(ObjectRecord.planets),
            )
            .where(SavedSearch.user_id == user_id)
            .order_by(SavedSearch.created_at.desc())
        )
        saved_rows = list(result.scalars().all())

        object_ids = [row.object_id for row in saved_rows]
        snapshots_by_object_id: dict[int, str] = {}
        if object_ids:
            snapshot_result = await session.execute(
                select(UserSummarySnapshot).where(
                    UserSummarySnapshot.user_id == user_id,
                    UserSummarySnapshot.object_id.in_(object_ids),
                )
            )
            for snapshot in snapshot_result.scalars().all():
                snapshots_by_object_id[snapshot.object_id] = snapshot.summary_text

        return [
            {
                "object": row.object,
                "favorited_at": row.created_at,
                "personal_summary": snapshots_by_object_id.get(row.object_id),
                "used_fallback_summary": row.object_id not in snapshots_by_object_id,
            }
            for row in saved_rows
        ]


async def save_user_summary_snapshot(user_id: int, object_id: int, summary_text: str) -> None:
    """Persist a logged-in user's personal copy of an AI summary they just
    generated/regenerated. Upserts: each user has at most one snapshot per object
    (their most recent own generation), enforced by the uq_user_summary_snapshot
    constraint -- a second Generate/Regenerate by the *same* user intentionally
    replaces their own snapshot; only *other* users' later actions are prevented
    from doing so.
    """
    async with SessionLocal() as session:
        existing = await session.execute(
            select(UserSummarySnapshot).where(
                UserSummarySnapshot.user_id == user_id, UserSummarySnapshot.object_id == object_id
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            row.summary_text = summary_text
            row.created_at = datetime.now(timezone.utc)
        else:
            session.add(
                UserSummarySnapshot(user_id=user_id, object_id=object_id, summary_text=summary_text)
            )
        await session.commit()
