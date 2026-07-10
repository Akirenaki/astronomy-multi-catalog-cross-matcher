"""Unit tests for find_planets() against a *realistic* mocked HTTP response shape.

The regression this guards against: the NASA Exoplanet Archive's TAP `format=json`
response is a bare top-level JSON array of row objects, e.g.:

    [{"pl_name": "51 Peg b", "pl_letter": "b", ...}, ...]

NOT a dict wrapped in a `{"data": [...]}` envelope. An earlier version of this module
called `response.json().get("data", [])`, which raised AttributeError on every real
response (lists have no .get) -- silently swallowed by the broad except block, so
find_planets() always returned no planets no matter what the API actually said.

test_resolver.py deliberately does NOT catch this class of bug, because it monkeypatches
find_planets() itself rather than exercising the JSON-parsing code. These tests mock
httpx one layer lower, at the response object, so the real parsing logic runs.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.catalogs.exoplanet_archive import find_planets


def _fake_client(json_body):
    """Build a mock httpx.AsyncClient whose .post() returns a response with the given
    already-decoded JSON body, mirroring the real TAP endpoint's shape."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=json_body)

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.post = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_find_planets_parses_realistic_bare_array_response():
    """The core regression: a bare top-level array (the real API shape) must be
    parsed successfully, not silently treated as zero rows."""
    payload = [
        {
            "pl_name": "51 Peg b",
            "pl_letter": "b",
            "pl_orbper": 4.230785,
            "pl_rade": 1.9,
            "disc_year": 1995,
            "discoverymethod": "Radial Velocity",
            "hostname": "HD 217014",
        }
    ]
    with patch("httpx.AsyncClient", return_value=_fake_client(payload)):
        planets, matched_alias = await find_planets(["HD 217014"])

    assert matched_alias == "HD 217014"
    assert len(planets) == 1
    assert planets[0]["pl_name"] == "51 Peg b"
    assert planets[0]["orbital_period_days"] == 4.230785
    assert planets[0]["discovery_year"] == 1995


@pytest.mark.asyncio
async def test_find_planets_returns_empty_for_genuinely_empty_array():
    """A star with no catalogued planets returns a real empty array from the API --
    confirm that still cleanly produces ([], None), not an exception."""
    with patch("httpx.AsyncClient", return_value=_fake_client([])):
        planets, matched_alias = await find_planets(["Barnard's Star"])

    assert planets == []
    assert matched_alias is None


@pytest.mark.asyncio
async def test_find_planets_tries_next_alias_after_empty_result():
    """First alias has no planets, second one does -- confirm the loop advances
    correctly and reports which alias actually matched."""
    empty_client = _fake_client([])
    hit_payload = [{"pl_name": "Test b", "pl_letter": "b", "hostname": "Alias Two"}]
    hit_client = _fake_client(hit_payload)

    calls = {"n": 0}

    def client_factory(*args, **kwargs):
        calls["n"] += 1
        return empty_client if calls["n"] == 1 else hit_client

    with patch("httpx.AsyncClient", side_effect=client_factory):
        planets, matched_alias = await find_planets(["Alias One", "Alias Two"])

    assert matched_alias == "Alias Two"
    assert planets[0]["pl_name"] == "Test b"
