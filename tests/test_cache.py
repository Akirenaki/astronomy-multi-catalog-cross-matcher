import os

# Use an isolated on-disk test database so these tests never touch the app's
# real astronomy.db, and so the engine binds to this URL before app.database
# is imported anywhere else in the test session.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app import cache as cache_mod
from app.database import init_db


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    """Create the test tables before each test and cleanly tear down the fixture afterward."""
    await init_db()
    yield


@pytest.mark.asyncio
async def test_ambiguous_result_persists_candidates(monkeypatch):
    """The core bug this test guards against: AMBIGUOUS results used to be
    stored with no candidate data at all, so the disambiguation list the
    spec requires was silently dropped before it ever reached the page."""

    fake_candidates = [
        {"main_id": "51 Peg", "ra": 344.36, "dec": 20.77, "otype": "Star", "sp_type": "G2V", "aliases": ["HD 217014"]},
        {"main_id": "51 Peg B", "ra": 344.37, "dec": 20.78, "otype": "Star", "sp_type": "M4V", "aliases": ["HD 217014 B"]},
    ]

    monkeypatch.setattr(
        "app.resolver.resolve_identity", AsyncMock(return_value=fake_candidates)
    )
    monkeypatch.setattr(
        "app.resolver.find_planets", AsyncMock(return_value=([], None))
    )

    record = await cache_mod.get_or_resolve("51 Peg Ambiguous Test")

    assert record.resolution_state == "AMBIGUOUS"
    assert len(record.candidates) == 2
    assert {c["main_id"] for c in record.candidates} == {"51 Peg", "51 Peg B"}

    # No confirmed object yet -> no AI summary should be generated/cached.
    assert record.ai_summary is None

    # Short, self-healing TTL (like UNRESOLVED), not the full 14-day TTL.
    remaining = record.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert remaining.total_seconds() < 2 * 60 * 60


@pytest.mark.asyncio
async def test_ambiguous_candidates_survive_a_cache_hit(monkeypatch):
    """Re-searching within the TTL window should serve the cached candidate
    list rather than dropping it on the second lookup."""

    fake_candidates = [
        {"main_id": "Alpha A", "ra": 1.0, "dec": 2.0, "otype": "Star", "sp_type": "G2V", "aliases": []},
        {"main_id": "Alpha B", "ra": 1.1, "dec": 2.1, "otype": "Star", "sp_type": "K1V", "aliases": []},
    ]
    resolve_mock = AsyncMock(return_value=fake_candidates)
    monkeypatch.setattr("app.resolver.resolve_identity", resolve_mock)
    monkeypatch.setattr("app.resolver.find_planets", AsyncMock(return_value=([], None)))

    first = await cache_mod.get_or_resolve("Alpha Cache Test")
    second = await cache_mod.get_or_resolve("Alpha Cache Test")

    assert first.candidates == second.candidates
    # Second call should be served from cache -> SIMBAD not queried again.
    assert resolve_mock.await_count == 1


@pytest.mark.asyncio
async def test_resolved_record_relationships_readable_after_session_closes(monkeypatch):
    """The core bug this test guards against: result.html and to_dict() both read
    `.identifiers` and `.planets` on the ObjectRecord returned by get_or_resolve(), well
    after the SessionLocal() context that produced it has closed. Those are lazy-loaded
    relationships -- touching them outside an open session used to raise
    sqlalchemy.orm.exc.DetachedInstanceError, which surfaced to real users as a 500
    "Internal Server Error" on essentially every search (RESOLVED, PARTIAL, and
    UNRESOLVED all read .identifiers/.planets in the template; only AMBIGUOUS skipped it).
    """
    monkeypatch.setattr(
        "app.resolver.resolve_identity",
        AsyncMock(
            return_value={
                "main_id": "51 Peg",
                "ra": 344.36,
                "dec": 20.77,
                "otype": "Star",
                "sp_type": "G2V",
                "aliases": ["HD 217014"],
            }
        ),
    )
    monkeypatch.setattr(
        "app.resolver.find_planets",
        AsyncMock(return_value=([{"pl_name": "51 Peg b", "pl_letter": "b"}], "HD 217014")),
    )

    record = await cache_mod.get_or_resolve("51 Peg Relationship Test")

    # No exception should be raised reaching into these relationships, even though the
    # session used inside get_or_resolve() has already been closed by this point.
    assert len(record.identifiers) == 1
    assert record.identifiers[0].identifier == "HD 217014"
    assert len(record.planets) == 1
    assert record.planets[0].pl_name == "51 Peg b"

    # Also confirm a cache hit (get_cached path, not store_result) survives the same way.
    cached_record = await cache_mod.get_or_resolve("51 Peg Relationship Test")
    assert len(cached_record.identifiers) == 1
    assert len(cached_record.planets) == 1
