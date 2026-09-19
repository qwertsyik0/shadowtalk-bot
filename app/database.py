from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


class Database:
    """Owns the SQLAlchemy engine and short-lived transactional sessions."""

    def __init__(self, settings: Settings) -> None:
        self.engine: AsyncEngine = create_async_engine(
            settings.sqlalchemy_database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_recycle=900,
        )
        self._session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        session = self._session_factory()
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def dispose(self) -> None:
        await self.engine.dispose()
