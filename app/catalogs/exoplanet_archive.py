import httpx


async def find_planets(alias_list: list[str]) -> tuple[list[dict], str | None]:
    if not alias_list:
        return [], None

    for alias in alias_list:
        query = (
            "SELECT top 20 pl_name, pl_letter, pl_orbper, pl_rade, disc_year, discoverymethod, hostname "
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
                        "orbital_period_days": row.get("pl_orbper") or row.get("PL_ORBPER"),
                        "planet_radius_earth": row.get("pl_rade") or row.get("PL_RADE"),
                        "discovery_year": row.get("disc_year") or row.get("DISC_YEAR"),
                        "discovery_method": row.get("discoverymethod") or row.get("DISCOVERYMETHOD"),
                    }
                )
            return planets, alias

    return [], None
