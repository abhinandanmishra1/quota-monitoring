import json
import os
import asyncpg
from quota.core.config import get_settings

def load_schema_sql() -> str:
    """Loads the database DDL and stored procedures from sql/schema.sql."""
    schema_path = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        return f.read()

SCHEMA_SQL = load_schema_sql()

async def init_connection(conn: asyncpg.Connection):
    """Sets up JSONB codec for automatic serialization."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )

async def create_db_pool(dsn: str | None = None) -> asyncpg.Pool:
    """Creates and returns an asyncpg connection pool."""
    settings = get_settings()
    database_url = dsn or settings.DATABASE_URL
    return await asyncpg.create_pool(
        dsn=database_url,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
        command_timeout=settings.DB_TIMEOUT,
        init=init_connection,
    )

async def init_db(pool: asyncpg.Pool) -> None:
    """Executes the DDL schema to ensure all tables and procedures are configured."""
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
