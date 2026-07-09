import re
from typing import Any

import httpx


def normalize_query(query_text: str) -> str:
    """Clean up user input by collapsing whitespace and removing common catalog prefixes."""
    cleaned = re.sub(r"\s+", " ", (query_text or "").strip())
    if not cleaned:
        return ""
    # Strip prefixes such as HD, HIP, GJ, and TYC so equivalent names resolve consistently.
    cleaned = re.sub(r"^(HD|HIP|GJ|TYC)\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


async def resolve_identity(query_text: str) -> dict | list[dict] | None:
    """Query SIMBAD for an object identity and return either one candidate or a list of candidates."""
    normalized = normalize_query(query_text)
    if not normalized:
        return None

    # Build an ADQL query that asks SIMBAD for the object metadata and aliases matching the supplied identifier.
    query = (
        "SELECT TOP 10 basic.main_id, basic.ra, basic.dec, basic.otype, basic.sp_type, ids.ids "
        "FROM basic JOIN ident ON basic.oid = ident.oidref JOIN ids ON basic.oid = ids.oidref "
        f"WHERE ident.id = '{normalized}'"
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
        # Any network or formatting issue should be treated as "no identity found" for this request.
        return None

    rows: list[dict[str, Any]] = []
    if isinstance(json_data, dict):
        if isinstance(json_data.get("data"), list):
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
