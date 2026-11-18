import os

# Ryuk reaper is flaky on Windows/Docker Desktop; the container is cleaned up by the fixture.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
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
async def session(pg_url):
    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        outer = await conn.begin()
        s = AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield s
        finally:
            await s.close()
            await outer.rollback()
    await engine.dispose()
