"""Database configuration and async SQLAlchemy session helpers."""

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base

# Use the database URL from the environment when present, otherwise fall back to a local SQLite file.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./astronomy.db")
# Create the async SQLAlchemy engine once so all modules share the same connection pool.
engine = create_async_engine(DATABASE_URL, echo=False)
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
