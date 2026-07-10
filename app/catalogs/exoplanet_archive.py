import httpx


async def find_planets(alias_list: list[str]) -> tuple[list[dict], str | None]:
    """Search the NASA Exoplanet Archive for planets associated with each alias in order."""
    if not alias_list:
        return [], None

    # Try each alias until one returns planet data, then stop.
    for alias in alias_list:
        # Escape embedded single quotes the same way simbad.py does, so aliases like
        # "O'Donnell's Star" don't silently break the query and get misread as "no planets found".
        escaped_alias = alias.replace("'", "''")
        query = (
            "SELECT top 20 pl_name, pl_letter, pl_orbper, pl_rade, disc_year, discoverymethod, hostname "
            f"FROM pscomppars WHERE hostname = '{escaped_alias}'"
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
                    "https://exoplanetarchive.ipac.caltech.edu/TAP/sync",
                    data=payload,
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                # The Exoplanet Archive's TAP format=json response is a bare top-level JSON
                # array of row objects (e.g. [{"pl_name": "...", ...}, ...]) -- NOT wrapped in
                # a {"data": [...]} envelope the way some other TAP services are. The previous
                # `.get("data", [])` call assumed a dict and raised AttributeError on every real
                # response (a list has no .get), which this broad except silently swallowed --
                # so find_planets() always returned no planets, no matter what. RESOLVED could
                # never actually trigger against the live API.
                body = response.json()
                rows = body if isinstance(body, list) else []
        except Exception:
            # If one alias fails or has no planet rows, continue to the next alias.
            continue

        if rows:
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
            return planets, alias

    return [], None
