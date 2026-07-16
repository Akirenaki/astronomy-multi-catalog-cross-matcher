"""Helpers for normalizing and resolving SIMBAD identifiers."""

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SimbadLookupError(Exception):
    """Raised when a SIMBAD request could not be completed -- a transport failure
    (timeout, connection refused, DNS failure), a bad HTTP status, or a response body
    that couldn't be parsed. This is deliberately distinct from resolve_identity()
    returning None, which means "SIMBAD was reached and genuinely has no match for
    this query." Collapsing both cases into None previously made a firewalled network
    indistinguishable from a nonexistent star -- see resolver.py's handling of this
    exception for how the two are now reported separately as LOOKUP_FAILED vs
    UNRESOLVED."""


def normalize_query(query_text: str) -> str:
    """Clean up whitespace/casing and canonicalize catalog-prefixed identifiers.

    SIMBAD's `ident.id` stores the catalog prefix as part of the identifier
    itself (e.g. "HD 217014", "HIP 113357") — both "HD 217014" and "HD217014"
    are accepted by SIMBAD's own identifier lookup, but the prefix must stay.
    Earlier versions of this function stripped the prefix entirely, which
    turned a valid identifier like "HD 217014" into a bare "217014" that
    SIMBAD can't resolve. The fix here is to normalize casing on the prefix
    and guarantee exactly one space between prefix and number, never to
    remove the prefix.
    """
    cleaned = re.sub(r"\s+", " ", (query_text or "").strip())
    if not cleaned:
        return ""

    match = re.match(r"^(HD|HIP|GJ|TYC)\s*(\S.*)$", cleaned, flags=re.IGNORECASE)
    if match:
        prefix, rest = match.groups()
        cleaned = f"{prefix.upper()} {rest.strip()}"

    return cleaned.strip()


async def resolve_identity(query_text: str) -> dict | list[dict] | None:
    """Query SIMBAD for an object identity and return either one candidate or a list of candidates.

    Returns None only when SIMBAD was successfully reached and genuinely has no match
    for this query. Raises SimbadLookupError when the request itself couldn't be
    completed (timeout, transport error, bad status, unparseable response) -- the
    caller should not treat that case as "no match found"."""
    normalized = normalize_query(query_text)
    if not normalized:
        return None

    escaped = normalized.replace("'", "''")
    # Build an ADQL query that asks SIMBAD for the object metadata and aliases matching the supplied identifier.
    query = (
        "SELECT TOP 10 basic.main_id, basic.ra, basic.dec, basic.otype, basic.sp_type, ids.ids "
        "FROM basic JOIN ident ON basic.oid = ident.oidref JOIN ids ON basic.oid = ids.oidref "
        f"WHERE ident.id = '{escaped}'"
    )

    payload = {
        "request": "doQuery",
        "lang": "adql",
        "format": "json",
        "query": query,
    }

    try:
        # A flat `timeout=20` applies that same 20s budget to connect, read, write, AND
        # pool-acquisition independently -- so a host that's genuinely unreachable
        # (firewalled, DNS issue, dead route) still takes up to 20s to fail, which is
        # indistinguishable from "the server is just slow" while debugging. Splitting
        # these lets a connection failure surface in ~5s while still giving a legitimately
        # slow-but-reachable SIMBAD response the full 60s to complete.
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=15.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                "https://simbad.cds.unistra.fr/simbad/sim-tap/sync",
                data=payload,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            json_data = response.json()
    except httpx.TimeoutException as exc:
        # A timeout means SIMBAD was never actually reached -- this is a network/
        # environment problem (firewalled host, dead route, DNS issue), not evidence
        # that the object doesn't exist. Raise rather than returning None so the
        # caller can report this honestly instead of as an "unresolved" star.
        logger.warning("SIMBAD lookup timed out for query_text=%r", normalized)
        raise SimbadLookupError(f"SIMBAD lookup timed out for {normalized!r}") from exc
    except httpx.HTTPError as exc:
        # Any other HTTP transport or status-layer issue (connection refused, DNS
        # failure, a 4xx/5xx from raise_for_status()) is likewise a service-reachability
        # problem, not a genuine "no match" -- log it and raise the same way.
        logger.warning("SIMBAD lookup failed for query_text=%r", normalized, exc_info=True)
        raise SimbadLookupError(f"SIMBAD lookup failed for {normalized!r}") from exc
    except Exception as exc:
        # A response that can't be parsed at all is also a service-side problem
        # (e.g. SIMBAD changed its response shape) rather than a real "not found".
        logger.warning("SIMBAD lookup failed for query_text=%r", normalized, exc_info=True)
        raise SimbadLookupError(f"SIMBAD lookup failed for {normalized!r}") from exc

    rows: list[dict[str, Any]] = []
    if isinstance(json_data, dict):
        metadata = json_data.get("metadata")
        data = json_data.get("data")
        if isinstance(metadata, list) and isinstance(data, list):
            # SIMBAD's TAP service follows the standard IVOA TAP JSON envelope (the same
            # shape used by e.g. the Gaia archive): {"metadata": [{"name": ...}, ...],
            # "data": [[v1, v2, ...], ...]}. Each row is a POSITIONAL array, not a dict
            # keyed by column name -- the column order is only given once, in "metadata".
            # The previous code assumed every row was already a dict (`row.get("ids")`,
            # `isinstance(row, dict)`), which is the shape the *Exoplanet Archive* returns,
            # not SIMBAD. Against a real response every row failed the `isinstance(row, dict)`
            # check, so `rows` was always empty and resolve_identity() always returned None --
            # every query would have come back UNRESOLVED against the live API despite every
            # resolver/cache test passing, because those tests mock resolve_identity() itself
            # rather than exercising this parsing code.
            column_names = [
                col.get("name") if isinstance(col, dict) else None for col in metadata
            ]
            for raw_row in data:
                if isinstance(raw_row, dict):
                    rows.append(raw_row)
                elif isinstance(raw_row, (list, tuple)):
                    rows.append(
                        {
                            name: value
                            for name, value in zip(column_names, raw_row)
                            if name is not None
                        }
                    )
        elif isinstance(json_data.get("data"), list):
            rows = [row for row in json_data["data"] if isinstance(row, dict)]
        elif isinstance(json_data.get("results"), list):
            rows = [row for row in json_data["results"] if isinstance(row, dict)]
    elif isinstance(json_data, list):
        rows = [row for row in json_data if isinstance(row, dict)]

    if not rows:
        return None

    # Normalize each SIMBAD row into a consistent structure that the rest of the app can use.
    candidates: list[dict[str, Any]] = []
    for row in rows:
        alias_field = row.get("ids") or row.get("ids.ids") or row.get("ids_ids") or row.get("idsids") or ""
        aliases = [item.strip() for item in str(alias_field).split("|") if item.strip()]
        candidate = {
            "main_id": row.get("main_id") or row.get("mainID") or row.get("MAIN_ID"),
            "ra": row.get("ra"),
            "dec": row.get("dec"),
            "otype": row.get("otype"),
            "sp_type": row.get("sp_type") or row.get("spType"),
            "aliases": aliases,
        }
        candidates.append(candidate)

    if len(candidates) == 1:
        return candidates[0]
    return candidates
