import pytest

from app.resolver import resolve_query


@pytest.mark.asyncio
async def test_resolver_returns_unresolved_when_simbad_has_no_match(monkeypatch):
    async def fake_simbad(query_text: str):
        return None

    async def fake_planets(alias_list):
        return [], None

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("not a star")

    assert result.state == "UNRESOLVED"
    assert result.planets == []
    assert result.aliases == []


@pytest.mark.asyncio
async def test_resolver_returns_partial_when_simbad_matches_but_no_planets(monkeypatch):
    async def fake_simbad(query_text: str):
        return {
            "main_id": "51 Peg",
            "ra": 10.0,
            "dec": 20.0,
            "otype": "Star",
            "sp_type": "G2V",
            "aliases": ["HD 217014"],
        }

    async def fake_planets(alias_list):
        return [], None

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("51 Peg")

    assert result.state == "PARTIAL"
    assert result.main_id == "51 Peg"
    assert result.planets == []


@pytest.mark.asyncio
async def test_resolver_returns_resolved_when_planet_match_found(monkeypatch):
    async def fake_simbad(query_text: str):
        return {
            "main_id": "51 Peg",
            "ra": 10.0,
            "dec": 20.0,
            "otype": "Star",
            "sp_type": "G2V",
            "aliases": ["HD 217014"],
        }

    async def fake_planets(alias_list):
        return [{"pl_name": "51 Peg b", "pl_letter": "b"}], "HD 217014"

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("51 Peg")

    assert result.state == "RESOLVED"
    assert result.planets[0]["pl_name"] == "51 Peg b"
    assert result.matched_alias == "HD 217014"


@pytest.mark.asyncio
async def test_resolver_returns_ambiguous_for_multiple_simbad_candidates(monkeypatch):
    async def fake_simbad(query_text: str):
        return [
            {
                "main_id": "51 Peg",
                "ra": 10.0,
                "dec": 20.0,
                "otype": "Star",
                "sp_type": "G2V",
                "aliases": ["HD 217014"],
            },
            {
                "main_id": "51 Peg B",
                "ra": 11.0,
                "dec": 21.0,
                "otype": "Star",
                "sp_type": "M4V",
                "aliases": ["HD 217014 B"],
            },
        ]

    async def fake_planets(alias_list):
        return [], None

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("51 Peg")

    assert result.state == "AMBIGUOUS"
    assert len(result.candidates) == 2
