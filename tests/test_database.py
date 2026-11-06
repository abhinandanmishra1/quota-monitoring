import pytest
import asyncpg
from quota.database import init_db
from quota.period import get_current_period, get_next_reset_time
from datetime import datetime, timezone

@pytest.mark.asyncio
async def test_init_db_creates_all_tables(db_pool: asyncpg.Pool):
    await init_db(db_pool)
    async with db_pool.acquire() as conn:
        tables = await conn.fetch(
            """
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public'
            ORDER BY tablename;
            """
        )
        table_names = [r["tablename"] for r in tables]
        assert "quota_config_default" in table_names
        assert "quota_config_override" in table_names
        assert "quota_usage" in table_names
        assert "quota_operations" in table_names

def test_period_calculation():
    # 2026-11-06 23:30:00 UTC
    dt = datetime(2026, 11, 6, 23, 30, 0, tzinfo=timezone.utc)
    period = get_current_period(dt)
    assert period == "2026-11"

    reset_time = get_next_reset_time(dt)
    assert reset_time == datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

def test_period_calculation_year_rollover():
    # 2026-12-15 10:00:00 UTC
    dt = datetime(2026, 12, 15, 10, 0, 0, tzinfo=timezone.utc)
    period = get_current_period(dt)
    assert period == "2026-12"

    reset_time = get_next_reset_time(dt)
    assert reset_time == datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
