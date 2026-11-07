import uuid
from datetime import datetime
import asyncpg
from quota.core.period import get_current_period, get_next_reset_time
from quota.schemas.common import OperationStatus
from quota.schemas.quota import (
    ConsumeResult,
    FinalizeResult,
    RefundResult,
    UsageResponse,
)

class QuotaRepository:
    """
    PostgreSQL repository handling all atomic transactions, locking, and quota logic.
    Follows DESIGN.md specifications for conditional updates, idempotency, and lock ordering.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def set_default_limit(self, feature: str, limit: int) -> None:
        """Sets or updates the default monthly limit for a feature."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO quota_config_default (feature, monthly_limit, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (feature)
                DO UPDATE SET monthly_limit = EXCLUDED.monthly_limit, updated_at = NOW();
                """,
                feature,
                limit,
            )

    async def set_override_limit(self, org_id: str, feature: str, limit: int) -> None:
        """Sets or updates an organization-specific monthly limit override."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO quota_config_override (org_id, feature, monthly_limit, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (org_id, feature)
                DO UPDATE SET monthly_limit = EXCLUDED.monthly_limit, updated_at = NOW();
                """,
                org_id,
                feature,
                limit,
            )

    async def get_effective_limit(
        self, conn: asyncpg.Connection, org_id: str, feature: str
    ) -> int | None:
        """
        Determines the effective limit for an organization and feature.
        Organization-specific override takes precedence over feature default.
        """
        override = await conn.fetchval(
            "SELECT monthly_limit FROM quota_config_override WHERE org_id = $1 AND feature = $2;",
            org_id,
            feature,
        )
        if override is not None:
            return override

        default_limit = await conn.fetchval(
            "SELECT monthly_limit FROM quota_config_default WHERE feature = $1;",
            feature,
        )
        return default_limit

    async def get_usage(
        self, org_id: str, feature: str, period: str | None = None
    ) -> UsageResponse | None:
        """
        Fetches usage for the given org, feature, and period.
        If no usage record exists for the current period, returns used = 0
        with the configured limit (per DESIGN.md Section 4.4).
        """
        target_period = period or get_current_period()
        reset_at = get_next_reset_time()

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT quota_limit, used 
                FROM quota_usage 
                WHERE org_id = $1 AND feature = $2 AND period = $3;
                """,
                org_id,
                feature,
                target_period,
            )
            if row:
                return UsageResponse(
                    org_id=org_id,
                    feature=feature,
                    period=target_period,
                    quota_limit=row["quota_limit"],
                    used=row["used"],
                    remaining=row["quota_limit"] - row["used"],
                    reset_at=reset_at,
                )

            effective_limit = await self.get_effective_limit(conn, org_id, feature)
            if effective_limit is None:
                return None

            return UsageResponse(
                org_id=org_id,
                feature=feature,
                period=target_period,
                quota_limit=effective_limit,
                used=0,
                remaining=effective_limit,
                reset_at=reset_at,
            )

    async def consume(
        self,
        org_id: str,
        feature: str,
        units: int,
        idempotency_key: str,
        operation_id: uuid.UUID,
        reset_at: datetime,
        period: str | None = None,
    ) -> ConsumeResult:
        """
        Executes an atomic consume operation with idempotency and all-or-nothing semantics.
        Executes via fn_quota_consume in a single database round-trip (DESIGN.md Section 8)
        to minimize lock hold duration and maximize sustained throughput.
        """
        target_period = period or get_current_period()

        query = """
            SELECT out_status_code, out_operation_id, out_status, out_units,
                   out_remaining, out_reset_at, out_period, out_detail
            FROM fn_quota_consume($1, $2, $3, $4, $5, $6, $7);
            """
        args = (
            org_id, feature, units, idempotency_key, operation_id, target_period, reset_at,
        )

        async with self.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(query, *args)
            except asyncpg.UniqueViolationError:
                # Concurrent retry with the same key lost the insert race. The whole
                # call rolled back (nothing charged); the winner's row now exists,
                # so a second call replays the stored result.
                row = await conn.fetchrow(query, *args)

            is_success = row["out_status_code"] == 200
            return ConsumeResult(
                success=is_success,
                status_code=row["out_status_code"],
                operation_id=str(row["out_operation_id"]),
                status=row["out_status"],
                units=row["out_units"],
                remaining=row["out_remaining"],
                reset_at=row["out_reset_at"],
                period=row["out_period"],
                detail=row["out_detail"],
            )

    async def finalize(self, operation_id: uuid.UUID) -> FinalizeResult:
        """
        Marks an operation as FINALIZED conditional on still being CHARGED.
        If already FINALIZED, returns success idempotently.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE quota_operations
                SET status = $2, updated_at = NOW()
                WHERE operation_id = $1 AND status = $3
                RETURNING status;
                """,
                operation_id,
                OperationStatus.FINALIZED.value,
                OperationStatus.CHARGED.value,
            )
            if row:
                return FinalizeResult(
                    success=True,
                    status_code=200,
                    operation_id=str(operation_id),
                    status=OperationStatus.FINALIZED.value,
                )

            current = await conn.fetchrow(
                "SELECT status FROM quota_operations WHERE operation_id = $1;",
                operation_id,
            )
            if not current:
                return FinalizeResult(
                    success=False,
                    status_code=404,
                    operation_id=str(operation_id),
                    status="NOT_FOUND",
                    detail="Operation not found",
                )
            if current["status"] == OperationStatus.FINALIZED:
                return FinalizeResult(
                    success=True,
                    status_code=200,
                    operation_id=str(operation_id),
                    status=OperationStatus.FINALIZED.value,
                )
            else:
                return FinalizeResult(
                    success=False,
                    status_code=409,
                    operation_id=str(operation_id),
                    status=current["status"],
                    detail=f"Cannot finalize operation in {current['status']} state",
                )

    async def refund(self, operation_id: uuid.UUID, reason: str = "") -> RefundResult:
        """
        Refunds units for an operation on confirmed downstream failure.
        Adheres to strict lock ordering (operation row first, usage row second)
        and deducts from the original billing period stored in the operation row.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. Lock operation row first (Section 6)
                op = await conn.fetchrow(
                    """
                    SELECT org_id, feature, request_units, period, status 
                    FROM quota_operations 
                    WHERE operation_id = $1 
                    FOR UPDATE;
                    """,
                    operation_id,
                )
                if not op:
                    return RefundResult(
                        success=False,
                        status_code=404,
                        operation_id=str(operation_id),
                        status="NOT_FOUND",
                        detail="Operation not found",
                    )

                if op["status"] != OperationStatus.CHARGED:
                    return RefundResult(
                        success=False,
                        status_code=409,
                        operation_id=str(operation_id),
                        status=op["status"],
                        detail=f"Cannot refund operation in {op['status']} state",
                    )

                # 2. Update operation status to REFUNDED
                await conn.execute(
                    """
                    UPDATE quota_operations
                    SET status = $2, updated_at = NOW()
                    WHERE operation_id = $1;
                    """,
                    operation_id,
                    OperationStatus.REFUNDED.value,
                )

                # 3. Lock and update usage row for the ORIGINAL period
                usage_row = await conn.fetchrow(
                    """
                    UPDATE quota_usage
                    SET used = used - $1, updated_at = NOW()
                    WHERE org_id = $2 AND feature = $3 AND period = $4
                    RETURNING used, quota_limit;
                    """,
                    op["request_units"],
                    op["org_id"],
                    op["feature"],
                    op["period"],
                )

                remaining = (
                    usage_row["quota_limit"] - usage_row["used"] if usage_row else 0
                )

                return RefundResult(
                    success=True,
                    status_code=200,
                    operation_id=str(operation_id),
                    status=OperationStatus.REFUNDED.value,
                    refunded_units=op["request_units"],
                    remaining=remaining,
                )

    async def update_current_period_limit(
        self, org_id: str, feature: str, new_limit: int, period: str | None = None
    ) -> UsageResponse:
        """
        Updates the limit for the period. Rejects a limit below consumed usage
        (DESIGN.md Section 4.4). The check lives in the UPDATE itself
        (used <= new_limit), so a concurrent consume cannot slip past it.
        """
        target_period = period or get_current_period()
        update_sql = """
            UPDATE quota_usage
            SET quota_limit = $1, updated_at = NOW()
            WHERE org_id = $2 AND feature = $3 AND period = $4 AND used <= $1
            RETURNING used, quota_limit;
            """
        args = (new_limit, org_id, feature, target_period)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(update_sql, *args)
            if row is None:
                used = await conn.fetchval(
                    "SELECT used FROM quota_usage WHERE org_id = $1 AND feature = $2 AND period = $3;",
                    org_id,
                    feature,
                    target_period,
                )
                if used is None:
                    # No usage row yet: create it (if the feature is configured), then apply.
                    if await self.get_effective_limit(conn, org_id, feature) is None:
                        raise ValueError(f"No quota configured for feature '{feature}'")
                    await conn.execute(
                        """
                        INSERT INTO quota_usage (org_id, feature, period, quota_limit, used)
                        VALUES ($1, $2, $3, $4, 0)
                        ON CONFLICT (org_id, feature, period) DO NOTHING;
                        """,
                        org_id,
                        feature,
                        target_period,
                        new_limit,
                    )
                    row = await conn.fetchrow(update_sql, *args)
                if row is None:
                    used = await conn.fetchval(
                        "SELECT used FROM quota_usage WHERE org_id = $1 AND feature = $2 AND period = $3;",
                        org_id,
                        feature,
                        target_period,
                    )
                    raise ValueError(
                        f"New limit ({new_limit}) cannot be less than already consumed usage ({used})"
                    )

        return UsageResponse(
            org_id=org_id,
            feature=feature,
            period=target_period,
            quota_limit=row["quota_limit"],
            used=row["used"],
            remaining=row["quota_limit"] - row["used"],
            reset_at=get_next_reset_time(),
        )

    async def list_operations(
        self, status: str | None, older_than_seconds: int | None, limit: int
    ) -> list[dict]:
        """Lists operations, newest first, for reconciliation and auditing."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT operation_id, org_id, feature, idempotency_key, request_units,
                       period, status, created_at, updated_at
                FROM quota_operations
                WHERE ($1::text IS NULL OR status = $1)
                  AND ($2::int IS NULL OR created_at <= NOW() - INTERVAL '1 second' * $2)
                ORDER BY created_at DESC
                LIMIT $3;
                """,
                status.upper() if status else None,
                older_than_seconds,
                limit,
            )
        return [
            {
                **dict(r),
                "operation_id": str(r["operation_id"]),
                "created_at": r["created_at"].isoformat(),
                "updated_at": r["updated_at"].isoformat(),
            }
            for r in rows
        ]
