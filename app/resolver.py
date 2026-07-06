from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.catalogs.exoplanet_archive import find_planets
from app.catalogs.simbad import normalize_query, resolve_identity


@dataclass
class ResolutionResult:
    query_text: str
    state: str
    main_id: str | None = None
    ra: float | None = None
    dec: float | None = None
    otype: str | None = None
    spectral_type: str | None = None
    aliases: list[str] = field(default_factory=list)
    planets: list[dict[str, Any]] = field(default_factory=list)
    matched_alias: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    resolved_via: list[str] = field(default_factory=list)


async def resolve_query(query_text: str) -> ResolutionResult:
    normalized_query = normalize_query(query_text)
    simbad_task = asyncio.create_task(resolve_identity(normalized_query or query_text))
    simbad_result = await simbad_task

    if simbad_result is None:
        return ResolutionResult(query_text=normalized_query or query_text, state="UNRESOLVED")

    if isinstance(simbad_result, list):
        return ResolutionResult(
            query_text=normalized_query or query_text,
            state="AMBIGUOUS",
            candidates=simbad_result,
        )

    aliases = list(simbad_result.get("aliases", []))
    planets_task = asyncio.create_task(find_planets(aliases)) if aliases else None
    planets, matched_alias = await planets_task if planets_task else ([], None)

    if planets:
        return ResolutionResult(
            query_text=normalized_query or query_text,
            state="RESOLVED",
            main_id=simbad_result.get("main_id"),
            ra=simbad_result.get("ra"),
            dec=simbad_result.get("dec"),
            otype=simbad_result.get("otype"),
            spectral_type=simbad_result.get("sp_type"),
            aliases=aliases,
            planets=planets,
            matched_alias=matched_alias,
            resolved_via=[normalized_query or query_text, simbad_result.get("main_id") or "", matched_alias or ""],
        )

    return ResolutionResult(
        query_text=normalized_query or query_text,
        state="PARTIAL",
        main_id=simbad_result.get("main_id"),
        ra=simbad_result.get("ra"),
        dec=simbad_result.get("dec"),
        otype=simbad_result.get("otype"),
        spectral_type=simbad_result.get("sp_type"),
        aliases=aliases,
        planets=planets,
        matched_alias=matched_alias,
        resolved_via=[normalized_query or query_text, simbad_result.get("main_id") or ""],
    )
