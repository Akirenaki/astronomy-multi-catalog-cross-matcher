"""Core cross-catalog resolution logic for astronomical object queries."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.catalogs.exoplanet_archive import find_planets
from app.catalogs.simbad import normalize_query, resolve_identity

# SIMBAD prefixes its main_id/aliases with a type-classifier token for many object
# categories -- most commonly "*" (star), "**" (double/multiple star), and "V*"
# (variable star), e.g. "* 51 Peg" or "V* RR Lyr". External catalogs like the NASA
# Exoplanet Archive don't use this convention: pscomppars.hostname for 51 Pegasi is
# the plain string "51 Peg", not "* 51 Peg". An exact-string match against SIMBAD's
# own main_id/aliases therefore misses well-known, unambiguous cross-matches -- this
# is what made a textbook case like HD 217014 / 51 Peg (host to 51 Peg b, one of the
# first confirmed exoplanets) come back PARTIAL instead of RESOLVED against the live
# API in testing.
_SIMBAD_TYPE_PREFIX_RE = re.compile(r"^(\*\*|V\*|\*)\s+")


def _without_simbad_type_prefix(name: str) -> str | None:
    """Return `name` with a leading SIMBAD type-classifier token removed, or None if
    there was no such prefix to strip."""
    stripped = _SIMBAD_TYPE_PREFIX_RE.sub("", name)
    return stripped if stripped != name else None


@dataclass
class ResolutionResult:
    """Structured container for the outcome of a query resolution attempt."""
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
    """Resolve a user query by consulting SIMBAD and optionally the exoplanet archive."""
    # Normalize the text first so the same object is treated consistently across lookups.
    normalized_query = normalize_query(query_text)
    # NOTE: this is a single awaited call, not real concurrency -- the Exoplanet Archive
    # lookup below depends on SIMBAD's alias output, so it genuinely cannot start until
    # this finishes (this is also documented in the context summary as a deliberate
    # deviation from the checklist's asyncio.gather suggestion). Wrapping a lone awaited
    # coroutine in create_task() just to immediately await it adds a task-scheduling
    # detour with no benefit, so call it directly instead.
    simbad_result = await resolve_identity(normalized_query or query_text)

    if simbad_result is None:
        return ResolutionResult(query_text=normalized_query or query_text, state="UNRESOLVED")

    if isinstance(simbad_result, list):
        return ResolutionResult(
            query_text=normalized_query or query_text,
            state="AMBIGUOUS",
            candidates=simbad_result,
        )

    aliases = list(simbad_result.get("aliases", []))
    # Per spec section 3, cross-matching should try the canonical SIMBAD main_id first,
    # then fall back to its aliases in the order SIMBAD returned them. ids.ids usually
    # already includes the main identifier, but nothing guarantees it's present or that
    # it's first in the list, so make the canonical-name-first order explicit here rather
    # than relying on SIMBAD's incidental ordering.
    main_id = simbad_result.get("main_id")
    match_candidates = list(aliases)
    if main_id and main_id not in match_candidates:
        match_candidates.insert(0, main_id)
    elif main_id in match_candidates:
        match_candidates.remove(main_id)
        match_candidates.insert(0, main_id)

    # For each identifier, also try the type-prefix-stripped form immediately after
    # it, so "* 51 Peg" and "51 Peg" are both attempted before moving on to the next
    # distinct identifier. Order matters here: a false-positive risk from stripping
    # (e.g. two different catalog objects that happen to share a bare name once their
    # prefixes are removed) is astronomically unlikely within one star's own alias set,
    # since these are all aliases SIMBAD already asserts refer to the same object.
    expanded_candidates: list[str] = []
    seen: set[str] = set()
    for candidate in match_candidates:
        for variant in (candidate, _without_simbad_type_prefix(candidate)):
            if variant and variant not in seen:
                expanded_candidates.append(variant)
                seen.add(variant)
    match_candidates = expanded_candidates

    # Ask the exoplanet archive for planets only when there is something to check.
    planets, matched_alias = await find_planets(match_candidates) if match_candidates else ([], None)

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
