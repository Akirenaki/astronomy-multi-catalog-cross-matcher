from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.database import SessionLocal
from app.models import ObjectRecord, IdentifierRecord, PlanetRecord
from app.narrative import generate_summary
from app.resolver import ResolutionResult, resolve_query
from app.catalogs.simbad import normalize_query


async def get_cached(query_text: str) -> ObjectRecord | None:
    """Return a cached object record when the normalized query is still fresh."""
    normalized_query = normalize_query(query_text)
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord).where(
                ObjectRecord.query_text == (normalized_query or query_text),
                ObjectRecord.expires_at > datetime.now(timezone.utc),
            )
        )
        return result.scalar_one_or_none()


async def store_result(resolution_result: ResolutionResult, *, generate_ai_summary: bool = True) -> ObjectRecord:
    """Persist a resolution result and any associated identifiers or planets to the database."""
    async with SessionLocal() as session:
        # Reuse the same row for a repeated query by removing any previous record first.
        existing = await session.execute(
            select(ObjectRecord).where(ObjectRecord.query_text == resolution_result.query_text)
        )
        record = existing.scalar_one_or_none()

        if record is not None:
            session.delete(record)
            await session.flush()

        # Serialize the ambiguous candidate list so it can be restored later without re-querying SIMBAD.
        candidates_json = (
            json.dumps(resolution_result.candidates) if resolution_result.candidates else None
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
            resolved_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        )
        session.add(record)
        await session.flush()

        # Unresolved and ambiguous requests should expire quickly so later fixes can be picked up.
        if resolution_result.state in ("UNRESOLVED", "AMBIGUOUS"):
            record.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        # Remove any old child rows before inserting the new ones for this object.
        await session.execute(
            delete(IdentifierRecord).where(IdentifierRecord.object_id == record.id)
        )
        await session.execute(
            delete(PlanetRecord).where(PlanetRecord.object_id == record.id)
        )
        await session.flush()

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

        # AI summaries are skipped for unresolved/ambiguous results because they do not represent a confirmed object.
        if generate_ai_summary and resolution_result.state not in ("UNRESOLVED", "AMBIGUOUS"):
            summary_payload = {
                "state": resolution_result.state,
                "main_id": resolution_result.main_id,
                "spectral_type": resolution_result.spectral_type,
                "planet_count": len(resolution_result.planets),
                "planets": resolution_result.planets,
            }
            record.ai_summary = await generate_summary(summary_payload)

        await session.commit()
        await session.refresh(record)
        return record


async def get_or_resolve(query_text: str) -> ObjectRecord:
    """Serve a cached result when possible, otherwise resolve the query and store it."""
    cached = await get_cached(query_text)
    if cached is not None:
        return cached

    result = await resolve_query(query_text)
    return await store_result(result)


async def get_object_by_simbad_id(simbad_main_id: str) -> ObjectRecord | None:
    """Look up a stored object by its SIMBAD main identifier."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord).where(ObjectRecord.simbad_main_id == simbad_main_id)
        )
        return result.scalar_one_or_none()


async def list_recent_objects(limit: int = 10) -> list[ObjectRecord]:
    """Return the newest object records, ordered from most recently resolved to oldest."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord).order_by(ObjectRecord.resolved_at.desc()).limit(limit)
        )
        return list(result.scalars().all())
