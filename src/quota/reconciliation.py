import asyncio
import logging
from typing import Any
import asyncpg
from quota.config import settings
from quota.database import create_db_pool

logger = logging.getLogger(__name__)

async def find_stale_charged_operations(
    pool: asyncpg.Pool, max_age_seconds: int = 300, limit: int = 100
) -> list[dict[str, Any]]:
    """
    Identifies operations remaining in CHARGED state older than max_age_seconds.
    As per DESIGN.md Section 4.3:
    Ambiguous operations remain in CHARGED state until their outcome is resolved.
    A reconciliation process periodically identifies old CHARGED operations and
    reports them for investigation. We do not automatically refund these operations
    based only on their age, as the downstream operation may have succeeded.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT operation_id, org_id, feature, idempotency_key, request_units, period, status, created_at 
            FROM quota_operations 
            WHERE status = 'CHARGED'
              AND created_at <= NOW() - INTERVAL '1 second' * $1
            ORDER BY created_at ASC
            LIMIT $2;
            """,
            max_age_seconds,
            limit,
        )
        return [dict(r) for r in rows]

async def run_reconciliation(max_age_seconds: int = 300) -> None:
    """CLI runner to inspect and report stale charged operations."""
    pool = await create_db_pool()
    try:
        stale_ops = await find_stale_charged_operations(pool, max_age_seconds=max_age_seconds)
        print(f"--- Reconciliation Report: Found {len(stale_ops)} stale CHARGED operations ---")
        for op in stale_ops:
            print(
                f"[INVESTIGATION NEEDED] Op: {op['operation_id']} | Org: {op['org_id']} | "
                f"Feature: {op['feature']} | Units: {op['request_units']} | Age: {op['created_at']}"
            )
    finally:
        await pool.close()

if __name__ == "__main__":
    import sys
    age = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    asyncio.run(run_reconciliation(age))
