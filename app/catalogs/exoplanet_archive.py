import httpx


async def find_planets(alias_list: list[str]) -> tuple[list[dict], str | None]:
    if not alias_list:
        return [], None

    for alias in alias_list:
        query = (
            "SELECT top 20 pl_name, pl_letter, orbital_period, planet_radius, discoveryyear, discoverymethod, hostname "
            f"FROM pscomppars WHERE hostname = '{alias}'"
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
                rows = response.json().get("data", [])
        except Exception:
            continue

        if rows:
            planets: list[dict] = []
            for row in rows:
                planets.append(
                    {
                        "pl_name": row.get("pl_name") or row.get("PL_NAME"),
                        "pl_letter": row.get("pl_letter") or row.get("PL_LETTER"),
                        "orbital_period_days": row.get("orbital_period") or row.get("ORBITAL_PERIOD"),
                        "planet_radius_earth": row.get("planet_radius") or row.get("PLANET_RADIUS"),
                        "discovery_year": row.get("discoveryyear") or row.get("DISCOVERYYEAR"),
                        "discovery_method": row.get("discoverymethod") or row.get("DISCOVERYMETHOD"),
                    }
                )
            return planets, alias

    return [], None
