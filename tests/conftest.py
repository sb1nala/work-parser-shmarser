import os

# Ryuk reaper is flaky on Windows/Docker Desktop; the container is cleaned up by the fixture.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("postgres:16") as pg:
        url = pg.get_connection_url()
        for prefix in ("postgresql+psycopg2://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+asyncpg://" + url[len(prefix):]
                break
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        yield url


@pytest_asyncio.fixture
async def conn_outer(pg_url):
    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        outer = await conn.begin()
        try:
            yield conn
        finally:
            await outer.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def session(conn_outer):
    s = AsyncSession(
        bind=conn_outer, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def sm(conn_outer):
    """Sessionmaker whose sessions (and their commits) stay inside the test's outer transaction."""
    return async_sessionmaker(
        bind=conn_outer, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
