import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


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
    """Query SIMBAD for an object identity and return either one candidate or a list of candidates."""
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
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://simbad.cds.unistra.fr/simbad/sim-tap/sync",
                data=payload,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            json_data = response.json()
    except Exception:
        # Any network or formatting issue is treated as "no identity found" for this
        # request -- but log it first. Without this, a genuine "SIMBAD has never heard
        # of this name" and a transient network/rate-limit failure (SIMBAD blocks
        # bursts above ~5-10 req/sec) both surface identically as UNRESOLVED, which
        # makes a real gap in the resolver indistinguishable from a self-inflicted
        # rate limit during a quick round of manual testing.
        logger.warning("SIMBAD lookup failed for query_text=%r", normalized, exc_info=True)
        return None

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
