"""Tests for Decision F's favoriting (saved_searches): logged-in users can
favorite/unfavorite an object; anonymous attempts are rejected, not silently
ignored."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest_asyncio
from fastapi.testclient import TestClient

from app.database import engine, init_db
from app.main import app
from app.models import Base


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


def _mock_betelgeuse(monkeypatch):
    monkeypatch.setattr(
        "app.resolver.resolve_identity",
        AsyncMock(
            return_value={
                "main_id": "* alf Ori",
                "ra": 88.79,
                "dec": 7.41,
                "otype": "Star",
                "sp_type": "M1-M2Ia-Iab",
                "aliases": ["Betelgeuse"],
            }
        ),
    )
    monkeypatch.setattr("app.resolver.find_planets", AsyncMock(return_value=([], None, False)))


def test_favoriting_as_logged_in_user_creates_saved_search_row(monkeypatch):
    """Favoriting a resolved object while logged in must create a saved_searches
    row, and the object page + /account/saved must reflect it."""
    _mock_betelgeuse(monkeypatch)
    encoded_id = quote("* alf Ori", safe="")

    with TestClient(app) as client:
        client.post("/register", data={"email": "wolfie@example.com", "password": "hunter2"})
        client.get("/search?q=Betelgeuse")

        favorite_response = client.post(f"/object/{encoded_id}/favorite", follow_redirects=False)
        assert favorite_response.status_code == 303

        object_page = client.get(f"/object/{encoded_id}")
        saved_page = client.get("/account/saved")

    assert "\u2605" in object_page.text  # filled star = already favorited
    assert "Betelgeuse" in saved_page.text


def test_anonymous_favorite_attempt_redirects_to_login_not_silently_ignored(monkeypatch):
    """An anonymous favorite attempt must be rejected/redirected to login -- not a
    silent no-op that leaves the user thinking it worked."""
    _mock_betelgeuse(monkeypatch)
    encoded_id = quote("* alf Ori", safe="")

    with TestClient(app) as client:
        client.get("/search?q=Betelgeuse")
        response = client.post(f"/object/{encoded_id}/favorite", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_unfavorite_removes_saved_search_row(monkeypatch):
    """Unfavoriting must actually remove the row, reflected in /account/saved."""
    _mock_betelgeuse(monkeypatch)
    encoded_id = quote("* alf Ori", safe="")

    with TestClient(app) as client:
        client.post("/register", data={"email": "wolfie@example.com", "password": "hunter2"})
        client.get("/search?q=Betelgeuse")
        client.post(f"/object/{encoded_id}/favorite")

        client.post(f"/object/{encoded_id}/unfavorite", follow_redirects=False)
        saved_page = client.get("/account/saved")

    assert "haven't saved any objects yet" in saved_page.text


def test_favoriting_same_object_twice_is_idempotent_not_an_error(monkeypatch):
    """Re-clicking Favorite on an already-favorited object should not raise/500."""
    _mock_betelgeuse(monkeypatch)
    encoded_id = quote("* alf Ori", safe="")

    with TestClient(app) as client:
        client.post("/register", data={"email": "wolfie@example.com", "password": "hunter2"})
        client.get("/search?q=Betelgeuse")

        first = client.post(f"/object/{encoded_id}/favorite", follow_redirects=False)
        second = client.post(f"/object/{encoded_id}/favorite", follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
