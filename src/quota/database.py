import json
import asyncpg
from quota.config import settings

# Database Schema Definition according to DESIGN.md Section 5
SCHEMA_SQL = """
-- 1. Default monthly quota limits for each feature
CREATE TABLE IF NOT EXISTS quota_config_default (
    feature VARCHAR(100) PRIMARY KEY,
    monthly_limit BIGINT NOT NULL CHECK (monthly_limit >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Organization-specific quota overrides for a feature
CREATE TABLE IF NOT EXISTS quota_config_override (
    org_id VARCHAR(100) NOT NULL,
    feature VARCHAR(100) NOT NULL,
    monthly_limit BIGINT NOT NULL CHECK (monthly_limit >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, feature)
);

-- 3. Monthly period-based quota usage records
-- Unique on (org_id, feature, period); check constraint: 0 <= used <= quota_limit
CREATE TABLE IF NOT EXISTS quota_usage (
    org_id VARCHAR(100) NOT NULL,
    feature VARCHAR(100) NOT NULL,
    period VARCHAR(7) NOT NULL, -- Format: YYYY-MM
    quota_limit BIGINT NOT NULL CHECK (quota_limit >= 0),
    used BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, feature, period),
    CONSTRAINT chk_quota_usage_range CHECK (used >= 0 AND used <= quota_limit)
);

-- 4. Quota operation audit and idempotency records
-- Unique on (org_id, feature, idempotency_key)
CREATE TABLE IF NOT EXISTS quota_operations (
    operation_id UUID PRIMARY KEY,
    org_id VARCHAR(100) NOT NULL,
    feature VARCHAR(100) NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    request_units BIGINT NOT NULL,
    period VARCHAR(7) NOT NULL, -- Original billing period charged
    status VARCHAR(20) NOT NULL CHECK (status IN ('CHARGED', 'FINALIZED', 'REFUNDED', 'REJECTED')),
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_quota_operations_key UNIQUE (org_id, feature, idempotency_key)
);

-- Index for background reconciliation of stale CHARGED operations
CREATE INDEX IF NOT EXISTS idx_quota_operations_status_created 
ON quota_operations (status, created_at);
"""

async def init_connection(conn: asyncpg.Connection):
    """Connection initialization helper: set up JSON encoder/decoder."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )

async def create_db_pool(dsn: str | None = None) -> asyncpg.Pool:
    """Creates and returns an asyncpg connection pool."""
    database_url = dsn or settings.DATABASE_URL
    return await asyncpg.create_pool(
        dsn=database_url,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
        command_timeout=settings.DB_TIMEOUT,
        init=init_connection,
    )

async def init_db(pool: asyncpg.Pool) -> None:
    """Executes the DDL schema to ensure all required tables exist."""
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
