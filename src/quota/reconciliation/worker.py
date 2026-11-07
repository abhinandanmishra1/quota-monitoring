import asyncio
import logging
from typing import Any
import asyncpg
from quota.core.database import create_db_pool

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

async def purge_expired_operations(
    pool: asyncpg.Pool, retention_hours: int = 24
) -> int:
    """
    Cleanup job (DESIGN.md Section 8): deletes finished operations older than the
    idempotency retention window. CHARGED operations are never deleted: they may
    still need a finalize/refund or investigation. Returns rows deleted.
    """
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM quota_operations
            WHERE status IN ('FINALIZED', 'REFUNDED', 'REJECTED')
              AND updated_at <= NOW() - INTERVAL '1 hour' * $1;
            """,
            retention_hours,
        )
    return int(result.split()[-1])

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

async def run_purge(retention_hours: int = 24) -> None:
    """CLI runner for the retention cleanup job."""
    pool = await create_db_pool()
    try:
        deleted = await purge_expired_operations(pool, retention_hours)
        print(f"Purged {deleted} operations older than {retention_hours}h")
    finally:
        await pool.close()

if __name__ == "__main__":
    import sys
    # usage: python -m quota.reconciliation.worker [report [max_age_s]] | purge [hours]
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    arg = int(sys.argv[2]) if len(sys.argv) > 2 else None
    if cmd == "purge":
        asyncio.run(run_purge(arg or 24))
    else:
        asyncio.run(run_reconciliation(arg or 300))
