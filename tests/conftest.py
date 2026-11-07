import pytest_asyncio
import asyncpg
from quota.core.config import settings
from quota.core.database import create_db_pool, init_db

@pytest_asyncio.fixture(scope="function")
async def db_pool():
    """Function-scoped asyncpg connection pool."""
    pool = await create_db_pool(settings.DATABASE_URL)
    await init_db(pool)
    yield pool
    await pool.close()

@pytest_asyncio.fixture(autouse=True)
async def clean_db(db_pool: asyncpg.Pool):
    """Truncates all tables between test cases for complete test isolation."""
    yield
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE TABLE 
                quota_operations,
                quota_usage,
                quota_config_override,
                quota_config_default
            CASCADE;
            """
        )
