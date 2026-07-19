"""Password hashing and session-based auth helpers for Decision F.

Uses Starlette's SessionMiddleware (signed-cookie sessions) rather than JWTs or a
hand-rolled session-token table -- this app is server-rendered with Jinja2, not an
SPA, so cookie sessions fit the existing architecture and need no new dependency
beyond itsdangerous (SessionMiddleware's own signing library).

Password hashing uses the `bcrypt` package directly rather than passlib[bcrypt].
The handoff doc offered either; bcrypt directly was chosen here because passlib's
CryptContext auto-detects the bcrypt backend's version via an `__about__` attribute
that bcrypt >=4.1 removed, which raises a (harmless but noisy) AttributeError warning
on every passlib import against a current bcrypt install. Calling bcrypt.hashpw /
bcrypt.checkpw ourselves avoids that entirely for a two-function surface we don't
need an abstraction layer over.
"""
from __future__ import annotations

import bcrypt
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import User

# bcrypt truncates/errors past 72 bytes -- cap defensively so a very long pasted
# password fails predictably in our own validation rather than inside bcrypt.
_MAX_PASSWORD_BYTES = 72


class DuplicateEmailError(Exception):
    """Raised by create_user() when the email is already registered.

    Mirrors the CS50 Finance pset pattern already used elsewhere in this project's
    history: attempt the insert and catch the DB's UNIQUE violation rather than
    pre-checking existence with a separate SELECT first (which would itself be a
    check-then-act race under concurrent registration attempts).
    """


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage."""
    if len(password.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        raise ValueError("Password too long")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plaintext password against a stored hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/legacy hash -- treat as a non-match rather than raising into a
        # login route.
        return False


async def create_user(email: str, password: str) -> User:
    """Create a new user with a hashed password.

    Raises DuplicateEmailError if the email is already registered, ValueError if
    the password is empty or too long.
    """
    if not password:
        raise ValueError("Password required")
    password_hash = hash_password(password)

    async with SessionLocal() as session:
        user = User(email=email, password_hash=password_hash)
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise DuplicateEmailError(f"Email already registered: {email!r}")
        await session.refresh(user)
        return user


async def get_user_by_email(email: str) -> User | None:
    """Look up a user by email, or None if no such user exists."""
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()


async def get_user_by_id(user_id: int) -> User | None:
    """Look up a user by primary key, or None if no such user exists."""
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()


async def authenticate(email: str, password: str) -> User | None:
    """Verify credentials and return the matching User, or None if invalid."""
    user = await get_user_by_email(email)
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def log_in_session(request: Request, user: User) -> None:
    """Stamp the current session as belonging to this logged-in user."""
    request.session["user_id"] = user.id


def log_out_session(request: Request) -> None:
    """Clear any logged-in-user state from the current session."""
    request.session.pop("user_id", None)


async def get_current_user(request: Request) -> User | None:
    """Return the logged-in User for this request's session, or None if anonymous.

    Used as a FastAPI dependency. Deliberately returns None rather than raising for
    the anonymous case -- most routes in this app (search, object pages, summaries)
    work the same for anonymous and logged-in visitors, so "no user" is a normal
    outcome, not an error. Routes that require login check the return value
    themselves (see require_login below) rather than this function enforcing it,
    since a single dependency can't know which of its callers need that enforced.
    """
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = await get_user_by_id(user_id)
    if user is None:
        # Session points at a user that no longer exists (e.g. deleted account) --
        # clear the stale session state rather than silently treating it as valid.
        log_out_session(request)
    return user


def get_session_id(request: Request) -> str:
    """Return a stable per-visitor identifier for anonymous rate limiting.

    Starlette's SessionMiddleware assigns a session to every request once the
    middleware is installed, regardless of login state -- this reuses that same
    cookie-backed session (rather than a separate IP-based or newly invented
    tracking mechanism) as the "session" dimension in RateLimitEvent for visitors
    who haven't logged in.
    """
    session_id = request.session.get("session_id")
    if session_id is None:
        import secrets

        session_id = secrets.token_hex(16)
        request.session["session_id"] = session_id
    return session_id
