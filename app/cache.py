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
from app.models import ObjectRecord, IdentifierRecord, PlanetRecord
from app.narrative import generate_summary
from app.resolver import ResolutionResult, resolve_query
from app.catalogs.simbad import normalize_query

logger = logging.getLogger(__name__)


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
        # Reuse the same row for a repeated query by finding any previous record first. We look
        # up by BOTH query_text and simbad_main_id (when known) because those are two different
        # search strings that can legitimately resolve to the same canonical object ("51 Peg" vs
        # "HD 217014") -- and simbad_main_id is UNIQUE, so failing to catch that case here is what
        # caused inserts to crash with IntegrityError on the second alias search for a given star.
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

        # Delete every matching old row (there will almost always be zero or one, but two distinct
        # rows are possible if this star was previously cached under a different query_text).
        # NOTE: session.delete() on an AsyncSession returns a coroutine and must be awaited --
        # omitting the await here previously left stale rows in place and caused
        # "IntegrityError: UNIQUE constraint failed: objects.simbad_main_id" on re-resolution.
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

        # AI summaries are skipped for unresolved/ambiguous/failed-lookup results because
        # they do not represent a confirmed object -- and for LOOKUP_FAILED specifically,
        # there is no structured data yet for the narrative layer to safely describe.
        if generate_ai_summary and resolution_result.state not in ("UNRESOLVED", "AMBIGUOUS", "LOOKUP_FAILED"):
            summary_payload = {
                "state": resolution_result.state,
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
            # The candidate_rows lookup above is a check-then-act sequence: two concurrent
            # get_or_resolve() calls for the same brand-new star (e.g. a double-click, two
            # browser tabs, or two workers) can both find zero existing rows and both reach
            # this commit, since neither has committed yet when the other runs its SELECT.
            # The second commit then violates the UNIQUE constraint on simbad_main_id. Rather
            # than surfacing that as a 500 to the user, treat it the same as a cache hit:
            # roll back this session's failed insert and return the row the other request
            # just committed.
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
            # Extremely unlikely (the conflicting row would have to disappear between the
            # failed commit and this re-query), but re-raise rather than returning None from
            # a function whose return type promises an ObjectRecord.
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


async def get_or_resolve(query_text: str) -> ObjectRecord:
    """Serve a cached result when possible, otherwise resolve the query and store it."""
    cached = await get_cached(query_text)
    if cached is not None:
        return cached

    result = await resolve_query(query_text)
    return await store_result(result)


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
    """Return the newest object records, ordered from most recently resolved to oldest."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord).order_by(ObjectRecord.resolved_at.desc()).limit(limit)
        )
        return list(result.scalars().all())
