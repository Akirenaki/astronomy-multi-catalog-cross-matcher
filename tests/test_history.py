"""Tests for the recent-history page and its object links.

Also covers Decision C: /history is a global, site-wide feed by design (it draws
directly from the shared object cache, not from any per-visitor activity log --
see the accompanying explanation for why "scope /history per visitor" wasn't the
right fix). These tests confirm that stays true regardless of login state, and that
logged-in users are pointed at their own /account/saved list alongside it.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from types import SimpleNamespace

import pytest_asyncio
from fastapi.testclient import TestClient

from app.database import engine, init_db
from app.main import app
from app.models import Base

from conftest import get_csrf_token


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


def test_history_links_to_object_profile_when_simbad_id_exists(monkeypatch):
    """History rows should link to the object profile when a canonical SIMBAD ID exists."""

    monkeypatch.setattr(
        "app.main.list_recent_objects",
        lambda limit=10: [
            SimpleNamespace(
                query_text="51 Peg",
                resolution_state="RESOLVED",
                simbad_main_id="51 Peg",
            )
        ],
    )

    with TestClient(app) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert 'href="/object/51%20Peg"' in response.text


def test_history_is_identical_regardless_of_login_state(monkeypatch):
    """Decision C: /history's actual object listing must not change based on who's
    viewing it -- it's a global feed, not scoped per visitor."""
    monkeypatch.setattr(
        "app.main.list_recent_objects",
        lambda limit=10: [
            SimpleNamespace(query_text="51 Peg", resolution_state="RESOLVED", simbad_main_id="51 Peg")
        ],
    )

    with TestClient(app) as anon_client, TestClient(app) as logged_in_client:
        anon_response = anon_client.get("/history")
        csrf_token = get_csrf_token(logged_in_client)
        logged_in_client.post(
            "/register", data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token}
        )
        logged_in_response = logged_in_client.get("/history")

    assert 'href="/object/51%20Peg"' in anon_response.text
    assert 'href="/object/51%20Peg"' in logged_in_response.text


def test_history_links_to_saved_objects_when_logged_in(monkeypatch):
    """Logged-in visitors should see a pointer to their personal saved-objects
    list, distinct from this global feed."""
    monkeypatch.setattr("app.main.list_recent_objects", lambda limit=10: [])

    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.post(
            "/register", data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token}
        )
        response = client.get("/history")

    assert '/account/saved' in response.text


def test_history_prompts_anonymous_visitors_to_log_in_for_personal_history():
    """Anonymous visitors should see a nudge toward logging in rather than an
    absent or confusing personal-history concept."""
    with TestClient(app) as client:
        response = client.get("/history")

    assert '/login' in response.text
