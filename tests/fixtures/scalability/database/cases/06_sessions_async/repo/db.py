from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from settings import settings

engine = create_async_engine("postgresql+asyncpg://u:p@localhost/db", pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)
legacy_factory = sessionmaker(engine, class_=AsyncSession)
other_engine = create_async_engine(settings.database_url)
registry_sessions = scoped_session(sessionmaker())


async def get_session():
    async with async_session() as session:
        yield session


async def get_legacy() -> AsyncIterator[AsyncSession]:
    yield make_session()


async def get_conn():
    async with engine.connect() as conn:
        yield conn


def get_scoped():
    return registry_sessions()
