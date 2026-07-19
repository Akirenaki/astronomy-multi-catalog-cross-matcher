"""Tests for Decision F: registration, login/logout, and the auth helpers they're
built on. See tests/test_favorites.py and tests/test_summary_snapshots.py for the
routes that actually require a logged-in user."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app import auth as auth_mod
from app.database import engine, init_db
from app.main import app
from app.models import Base


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    """Same isolation rationale as the rest of the suite -- see test_cache.py."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


# --- app.auth unit tests -----------------------------------------------------

@pytest.mark.asyncio
async def test_create_user_hashes_password_not_plaintext():
    """The stored password_hash must not be the plaintext password."""
    user = await auth_mod.create_user(email="wolfie@example.com", password="hunter2")
    assert user.password_hash != "hunter2"
    assert auth_mod.verify_password("hunter2", user.password_hash) is True


def test_verify_password_rejects_wrong_password():
    """verify_password is synchronous -- confirm it actually distinguishes right
    from wrong passwords rather than always returning True."""
    password_hash = auth_mod.hash_password("correct-horse")
    assert auth_mod.verify_password("correct-horse", password_hash) is True
    assert auth_mod.verify_password("wrong-guess", password_hash) is False


@pytest.mark.asyncio
async def test_create_user_duplicate_email_raises_cleanly():
    """Registering the same email twice must raise DuplicateEmailError (caught by
    the /register route to re-render a friendly error), not crash with a raw
    IntegrityError/500."""
    await auth_mod.create_user(email="wolfie@example.com", password="first-password")
    with pytest.raises(auth_mod.DuplicateEmailError):
        await auth_mod.create_user(email="wolfie@example.com", password="second-password")


# --- route-level tests --------------------------------------------------------

def test_register_route_creates_account_and_logs_in():
    """POST /register should create the user and leave the session logged in --
    the next request in the same client should be recognized as that user."""
    with TestClient(app) as client:
        response = client.post(
            "/register",
            data={"email": "wolfie@example.com", "password": "hunter2"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        home_response = client.get("/")

    assert "wolfie@example.com" in home_response.text


def test_register_route_rejects_duplicate_email_with_400_not_500():
    """The doc's explicit regression case: a duplicate-email registration must fail
    cleanly with a 400 and a visible error message, not a raw 500."""
    with TestClient(app) as client:
        client.post("/register", data={"email": "wolfie@example.com", "password": "hunter2"})
        second = client.post(
            "/register",
            data={"email": "wolfie@example.com", "password": "different-password"},
            follow_redirects=False,
        )

    assert second.status_code == 400
    assert "already registered" in second.text


def test_login_route_sets_session_and_protected_route_succeeds():
    """Login sets a session cookie; a protected route (here, /account/saved)
    succeeds after login."""
    with TestClient(app) as client:
        client.post("/register", data={"email": "wolfie@example.com", "password": "hunter2"})
        client.post("/logout")

        # Confirm the protected route rejects/redirects before login.
        before_login = client.get("/account/saved", follow_redirects=False)
        assert before_login.status_code == 303
        assert before_login.headers["location"].startswith("/login")

        login_response = client.post(
            "/login",
            data={"email": "wolfie@example.com", "password": "hunter2"},
            follow_redirects=False,
        )
        assert login_response.status_code == 303

        after_login = client.get("/account/saved")

    assert after_login.status_code == 200


def test_login_route_rejects_wrong_password():
    """A wrong password must not log the user in."""
    with TestClient(app) as client:
        client.post("/register", data={"email": "wolfie@example.com", "password": "hunter2"})
        client.post("/logout")

        response = client.post(
            "/login",
            data={"email": "wolfie@example.com", "password": "wrong-password"},
            follow_redirects=False,
        )
        assert response.status_code == 400

        protected = client.get("/account/saved", follow_redirects=False)

    assert protected.status_code == 303  # still anonymous


def test_logout_clears_session():
    """POST /logout must actually clear the logged-in state."""
    with TestClient(app) as client:
        client.post("/register", data={"email": "wolfie@example.com", "password": "hunter2"})
        client.post("/logout")

        response = client.get("/account/saved", follow_redirects=False)

    assert response.status_code == 303
