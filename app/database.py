"""Database configuration and async SQLAlchemy session helpers."""

import os
from collections.abc import AsyncGenerator

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base

# Use the database URL from the environment when present, otherwise fall back to a local SQLite file.
_raw_database_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./astronomy.db")

# Managed Postgres providers (Neon, Supabase, etc.) hand out libpq-style connection
# strings like postgresql://...?sslmode=require&channel_binding=require. asyncpg (the
# async driver this app uses) doesn't understand those query params -- passing them
# straight through raises "connect() got an unexpected keyword argument 'sslmode'".
# Detect a Postgres URL, force the asyncpg dialect, strip those params, and request
# TLS the way asyncpg actually expects it: via connect_args, not the URL string.
_connect_args: dict = {}
if _raw_database_url.startswith("postgresql://") or _raw_database_url.startswith("postgres://"):
    DATABASE_URL = make_url(_raw_database_url).set(drivername="postgresql+asyncpg", query={})
    _connect_args = {"ssl": "require"}
else:
    DATABASE_URL = _raw_database_url

# Create the async SQLAlchemy engine once so all modules share the same connection pool.
engine = create_async_engine(DATABASE_URL, echo=False, connect_args=_connect_args)
# Build a session factory that can open short-lived async database sessions.
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)


async def init_db() -> None:
    """Create the database tables defined by the SQLAlchemy models if they do not exist yet."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session that can be used inside FastAPI dependency-style code."""
    async with SessionLocal() as session:
        yield session
