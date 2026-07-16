"""Helpers for querying the NASA Exoplanet Archive."""

import logging
import time

import httpx

logger = logging.getLogger(__name__)

# Keeps each IN(...) clause well under typical TAP query-length limits. A star's own
# SIMBAD alias set is expected to be well under this in the vast majority of cases
# (even heavily-catalogued objects like Betelgeuse or Proxima Centauri), but chunking
# defensively avoids building one arbitrarily long query string for the rare outlier.
_BATCH_SIZE = 40

_EXOPLANET_ARCHIVE_URL = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"


async def find_planets(alias_list: list[str]) -> tuple[list[dict], str | None]:
    """Search the NASA Exoplanet Archive for planets associated with any alias.

    Tries every alias in one batched `hostname IN (...)` query per chunk, rather than
    one request per alias.

    Regression this fixes: the archive's `hostname` field is an exact-string match, so
    a star with many SIMBAD aliases (Betelgeuse and Proxima Centauri both carry 100+
    cross-catalog identifiers) previously required one full HTTP round trip per alias,
    tried strictly in sequence, before giving up -- easily 100+ sequential requests for
    a star with no exoplanet-archive match at all (e.g. Betelgeuse, which has none).
    That multiplied into multi-minute searches that timed out the browser/dev-proxy
    even though the app itself was still working underneath. Batching collapses that
    into (usually) exactly one request.
    """
    if not alias_list:
        return [], None

    for chunk_start in range(0, len(alias_list), _BATCH_SIZE):
        chunk = alias_list[chunk_start : chunk_start + _BATCH_SIZE]
        rows_by_hostname = await _query_hostnames(chunk)
        if rows_by_hostname is None:
            # This chunk's request failed outright (see _query_hostnames' logging) --
            # move on to the next chunk rather than aborting the whole search, matching
            # the old per-alias loop's fault tolerance (one bad request didn't used to
            # sink the entire lookup either).
            continue

        # Preserve the same priority order the old sequential loop had: the first
        # alias in this chunk with any matching rows wins, even if a later alias in
        # the same chunk also matched. In practice this should be at most one alias,
        # since every alias here is asserted by SIMBAD to refer to the same object.
        for alias in chunk:
            rows = rows_by_hostname.get(alias)
            if rows:
                return _rows_to_planets(rows), alias

    return [], None


async def _query_hostnames(aliases: list[str]) -> dict[str, list[dict]] | None:
    """Run one batched ADQL query for a chunk of aliases.

    Returns rows grouped by the exact `hostname` string that matched, or None if the
    request itself failed (transport error, bad status, or an unparseable body).
    """
    # Escape embedded single quotes the same way simbad.py does, so aliases like
    # "O'Donnell's Star" don't silently break the query.
    escaped = [alias.replace("'", "''") for alias in aliases]
    in_clause = ", ".join(f"'{value}'" for value in escaped)
    # No TOP limit here (unlike the old per-alias "TOP 20"): a single chunk can now
    # legitimately return rows for one star with several planets, and there's no
    # reliable a-priori bound on how many that could be across up to _BATCH_SIZE
    # candidate hostnames in the same request.
    query = (
        "SELECT pl_name, pl_letter, pl_orbper, pl_rade, disc_year, discoverymethod, hostname "
        f"FROM pscomppars WHERE hostname IN ({in_clause})"
    )

    payload = {
        "request": "doQuery",
        "lang": "adql",
        "format": "json",
        "query": query,
    }

    started_at = time.perf_counter()
    try:
        # See the matching comment in catalogs/simbad.py -- splitting connect from
        # read lets an unreachable host fail fast instead of always taking 20s.
        timeout = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                _EXOPLANET_ARCHIVE_URL,
                data=payload,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            # The Exoplanet Archive's TAP format=json response is a bare top-level JSON
            # array of row objects, not wrapped in a {"data": [...]} envelope -- see the
            # regression note in the test suite for the bug this shape assumption fixed.
            body = response.json()
            rows = body if isinstance(body, list) else []
    except Exception:
        # A transient network error or a rate-limit response (429) on one chunk is
        # logged, not silently swallowed, so it's distinguishable from that chunk's
        # stars genuinely having no planets.
        logger.warning("Exoplanet Archive batched lookup failed for %d aliases", len(aliases), exc_info=True)
        return None
    finally:
        logger.info(
            "Exoplanet Archive stage: queried %d aliases in %.3fs",
            len(aliases),
            time.perf_counter() - started_at,
        )

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        hostname = row.get("hostname") or row.get("HOSTNAME")
        if hostname:
            grouped.setdefault(hostname, []).append(row)
    return grouped


def _rows_to_planets(rows: list[dict]) -> list[dict]:
    """Convert raw Exoplanet Archive rows into this app's planet dict shape."""
    planets: list[dict] = []
    for row in rows:
        planets.append(
            {
                "pl_name": row.get("pl_name") or row.get("PL_NAME"),
                "pl_letter": row.get("pl_letter") or row.get("PL_LETTER"),
                "orbital_period_days": row.get("pl_orbper") or row.get("PL_ORBPER"),
                "planet_radius_earth": row.get("pl_rade") or row.get("PL_RADE"),
                "discovery_year": row.get("disc_year") or row.get("DISC_YEAR"),
                "discovery_method": row.get("discoverymethod") or row.get("DISCOVERYMETHOD"),
            }
        )
    return planets
