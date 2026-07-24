"""Password hashing and session-based auth helpers."""
from __future__ import annotations

import secrets

import bcrypt
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import User

# bcrypt truncates/errors past 72 bytes -- cap defensively so a very long pasted
# password fails predictably in our own validation rather than inside bcrypt.
_MAX_PASSWORD_BYTES = 72

# README §VII.G/EVALUATION.md suggestion #7: no complexity requirement beyond a
# floor is imposed. A hard minimum-length check catches the most common weak
# passwords (empty, "a", "123") without being punitive for a portfolio project;
# it is deliberately not a "complexity" policy (no forced digits/symbols/case
# mixing), which research on password strength generally finds pushes people
# toward predictable substitutions rather than genuinely stronger passwords.
_MIN_PASSWORD_LENGTH = 8


class DuplicateEmailError(Exception):
    """Raised by create_user() when the email is already registered.

    The insert is attempted directly so concurrent registrations race on the DB's
    UNIQUE constraint instead of on a separate existence check.
    """


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage."""
    if len(password.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        raise ValueError("Password too long")
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plaintext password against a stored hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Treat malformed hashes as a login failure.
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

    Most routes in this app work the same for anonymous and logged-in visitors, so
    "no user" is a normal outcome rather than an error.
    """
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = await get_user_by_id(user_id)
    if user is None:
        # Clear stale session state for deleted accounts.
        log_out_session(request)
    return user


def get_session_id(request: Request) -> str:
    """Return a stable per-visitor identifier for anonymous rate limiting.

    This reuses the existing cookie-backed session rather than inventing a second
    anonymous-tracking mechanism.
    """
    session_id = request.session.get("session_id")
    if session_id is None:
        session_id = secrets.token_hex(16)
        request.session["session_id"] = session_id
    return session_id


def get_csrf_token(request: Request) -> str:
    """Return this session's CSRF token, minting one on first use.

    Stored in the signed, itsdangerous-backed session cookie SessionMiddleware
    already provides, so it can't be forged or read by an attacker who doesn't
    already have a valid session for this app. This is a standard synchronizer
    token: a cross-site form or fetch() can make the victim's browser attach
    their session cookie automatically, but the attacker's page has no way to
    read this value out of the victim's session to include it in the forged
    request, so verify_csrf_token() rejects it even though the request carries
    valid session auth. See EVALUATION.md suggestion #6 -- README §VII.G had
    flagged this as a known, undefended gap.
    """
    token = request.session.get("csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def verify_csrf_token(request: Request, submitted_token: str | None) -> bool:
    """Constant-time check of a submitted CSRF token against this session's own.

    False whenever either side is missing -- a session with no token yet (nothing
    to compare against) is just as invalid as a request with no token attached.
    """
    expected = request.session.get("csrf_token")
    if not expected or not submitted_token:
        return False
    return secrets.compare_digest(expected, submitted_token)
