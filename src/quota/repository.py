import uuid
from datetime import datetime, timezone
import asyncpg
from quota.models import (
    OperationStatus,
    ConsumeResult,
    FinalizeResult,
    RefundResult,
    UsageResponse,
)
from quota.period import get_current_period, get_next_reset_time

class QuotaRepository:
    """PostgreSQL repository handling all atomic transactions, locking, and quota logic."""

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
        # 1. Check override
        override = await conn.fetchval(
            "SELECT monthly_limit FROM quota_config_override WHERE org_id = $1 AND feature = $2;",
            org_id,
            feature,
        )
        if override is not None:
            return override

        # 2. Check feature default
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
            # Check existing usage row
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

            # No usage row yet: check if feature is configured
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
        All updates happen within a single PostgreSQL transaction.
        """
        target_period = period or get_current_period()

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. Check idempotency record (lock row to serialize concurrent identical retries)
                existing_op = await conn.fetchrow(
                    """
                    SELECT operation_id, request_units, status, response 
                    FROM quota_operations 
                    WHERE org_id = $1 AND feature = $2 AND idempotency_key = $3
                    FOR UPDATE;
                    """,
                    org_id,
                    feature,
                    idempotency_key,
                )

                if existing_op:
                    # Same key with same requested units: replay stored response
                    if existing_op["request_units"] == units:
                        stored = existing_op["response"]
                        # Reconstruct ConsumeResult from stored JSON payload
                        is_charged = existing_op["status"] in ("CHARGED", "FINALIZED")
                        status_code = 200 if is_charged else 429
                        return ConsumeResult(
                            success=is_charged,
                            status_code=status_code,
                            operation_id=str(existing_op["operation_id"]),
                            status=existing_op["status"],
                            units=stored.get("units", units),
                            remaining=stored.get("remaining", 0),
                            reset_at=datetime.fromisoformat(stored["reset_at"]),
                            period=stored.get("period", target_period),
                            detail=stored.get("detail"),
                        )
                    else:
                        # Same key with different payload: 409 Conflict
                        return ConsumeResult(
                            success=False,
                            status_code=409,
                            operation_id=str(existing_op["operation_id"]),
                            status="CONFLICT",
                            units=units,
                            remaining=0,
                            reset_at=reset_at,
                            period=target_period,
                            detail="Idempotency key reused with different parameters",
                        )

                # 2. Check effective configured limit
                effective_limit = await self.get_effective_limit(conn, org_id, feature)
                if effective_limit is None:
                    return ConsumeResult(
                        success=False,
                        status_code=404,
                        operation_id=str(operation_id),
                        status="NOT_FOUND",
                        units=units,
                        remaining=0,
                        reset_at=reset_at,
                        period=target_period,
                        detail=f"No quota configured for feature '{feature}'",
                    )

                # 3. Lazily initialize usage row if not already present for this period
                await conn.execute(
                    """
                    INSERT INTO quota_usage (org_id, feature, period, quota_limit, used)
                    VALUES ($1, $2, $3, $4, 0)
                    ON CONFLICT (org_id, feature, period) DO NOTHING;
                    """,
                    org_id,
                    feature,
                    target_period,
                    effective_limit,
                )

                # 4. Atomic conditional deduction (DESIGN.md Section 4.1)
                updated_row = await conn.fetchrow(
                    """
                    UPDATE quota_usage
                    SET used = used + $1, updated_at = NOW()
                    WHERE org_id = $2
                      AND feature = $3
                      AND period = $4
                      AND used <= quota_limit - $1
                    RETURNING used, quota_limit;
                    """,
                    units,
                    org_id,
                    feature,
                    target_period,
                )

                if updated_row:
                    # Successful deduction: all units allocated
                    used = updated_row["used"]
                    quota_limit = updated_row["quota_limit"]
                    remaining = quota_limit - used

                    response_data = {
                        "operation_id": str(operation_id),
                        "status": "CHARGED",
                        "units": units,
                        "remaining": remaining,
                        "reset_at": reset_at.isoformat(),
                        "period": target_period,
                    }

                    # Record operation as CHARGED
                    await conn.execute(
                        """
                        INSERT INTO quota_operations (
                            operation_id, org_id, feature, idempotency_key, 
                            request_units, period, status, response
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, 'CHARGED', $7);
                        """,
                        operation_id,
                        org_id,
                        feature,
                        idempotency_key,
                        units,
                        target_period,
                        response_data,
                    )

                    return ConsumeResult(
                        success=True,
                        status_code=200,
                        operation_id=str(operation_id),
                        status="CHARGED",
                        units=units,
                        remaining=remaining,
                        reset_at=reset_at,
                        period=target_period,
                    )
                else:
                    # Insufficient quota: nothing is deducted (All-or-nothing, Section 4.2)
                    current_usage = await conn.fetchrow(
                        """
                        SELECT used, quota_limit 
                        FROM quota_usage 
                        WHERE org_id = $1 AND feature = $2 AND period = $3;
                        """,
                        org_id,
                        feature,
                        target_period,
                    )
                    used = current_usage["used"] if current_usage else 0
                    quota_limit = current_usage["quota_limit"] if current_usage else effective_limit
                    remaining = quota_limit - used

                    response_data = {
                        "detail": "Quota limit exceeded",
                        "requested": units,
                        "remaining": remaining,
                        "reset_at": reset_at.isoformat(),
                        "period": target_period,
                    }

                    # Record rejected operation to ensure idempotency on replay
                    await conn.execute(
                        """
                        INSERT INTO quota_operations (
                            operation_id, org_id, feature, idempotency_key, 
                            request_units, period, status, response
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, 'REJECTED', $7);
                        """,
                        operation_id,
                        org_id,
                        feature,
                        idempotency_key,
                        units,
                        target_period,
                        response_data,
                    )

                    return ConsumeResult(
                        success=False,
                        status_code=429,
                        operation_id=str(operation_id),
                        status="REJECTED",
                        units=units,
                        remaining=remaining,
                        reset_at=reset_at,
                        period=target_period,
                        detail="Quota limit exceeded",
                    )

    async def finalize(self, operation_id: uuid.UUID) -> FinalizeResult:
        """
        Marks an operation as FINALIZED conditional on still being CHARGED.
        If already FINALIZED, returns success idempotently.
        """
        async with self.pool.acquire() as conn:
            # Conditional state transition: CHARGED -> FINALIZED
            row = await conn.fetchrow(
                """
                UPDATE quota_operations
                SET status = 'FINALIZED', updated_at = NOW()
                WHERE operation_id = $1 AND status = 'CHARGED'
                RETURNING status;
                """,
                operation_id,
            )
            if row:
                return FinalizeResult(
                    success=True,
                    status_code=200,
                    operation_id=str(operation_id),
                    status="FINALIZED",
                )

            # If no row was updated, inspect current state
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
            if current["status"] == "FINALIZED":
                # Idempotent replay
                return FinalizeResult(
                    success=True,
                    status_code=200,
                    operation_id=str(operation_id),
                    status="FINALIZED",
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

                if op["status"] != "CHARGED":
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
                    SET status = 'REFUNDED', updated_at = NOW()
                    WHERE operation_id = $1;
                    """,
                    operation_id,
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
                    status="REFUNDED",
                    refunded_units=op["request_units"],
                    remaining=remaining,
                )

    async def update_current_period_limit(
        self, org_id: str, feature: str, new_limit: int, period: str | None = None
    ) -> UsageResponse:
        """
        Updates the quota limit for the current period.
        Rejects any limit below the already consumed usage (DESIGN.md Section 4.4).
        """
        target_period = period or get_current_period()
        reset_at = get_next_reset_time()

        async with self.pool.acquire() as conn:
            # Check current usage
            current = await conn.fetchrow(
                """
                SELECT used, quota_limit 
                FROM quota_usage 
                WHERE org_id = $1 AND feature = $2 AND period = $3;
                """,
                org_id,
                feature,
                target_period,
            )
            if not current:
                # If no usage row exists yet, check configured limit and create it
                effective_limit = await self.get_effective_limit(conn, org_id, feature)
                if effective_limit is None:
                    raise ValueError(f"No quota configured for feature '{feature}'")
                if new_limit < 0:
                    raise ValueError("Quota limit cannot be negative")

                await conn.execute(
                    """
                    INSERT INTO quota_usage (org_id, feature, period, quota_limit, used)
                    VALUES ($1, $2, $3, $4, 0);
                    """,
                    org_id,
                    feature,
                    target_period,
                    new_limit,
                )
                return UsageResponse(
                    org_id=org_id,
                    feature=feature,
                    period=target_period,
                    quota_limit=new_limit,
                    used=0,
                    remaining=new_limit,
                    reset_at=reset_at,
                )

            used = current["used"]
            if new_limit < used:
                raise ValueError(
                    f"New limit ({new_limit}) cannot be less than already consumed usage ({used})"
                )

            updated = await conn.fetchrow(
                """
                UPDATE quota_usage
                SET quota_limit = $1, updated_at = NOW()
                WHERE org_id = $2 AND feature = $3 AND period = $4
                RETURNING used, quota_limit;
                """,
                new_limit,
                org_id,
                feature,
                target_period,
            )

            return UsageResponse(
                org_id=org_id,
                feature=feature,
                period=target_period,
                quota_limit=updated["quota_limit"],
                used=updated["used"],
                remaining=updated["quota_limit"] - updated["used"],
                reset_at=reset_at,
            )
