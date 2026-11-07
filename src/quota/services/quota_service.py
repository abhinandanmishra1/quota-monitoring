import functools
import uuid

import asyncpg

from quota.core.exceptions import DatabaseUnavailableError
from quota.core.period import get_next_reset_time
from quota.repository.quota_repository import QuotaRepository
from quota.schemas.quota import (
    ConsumeResult,
    FinalizeResult,
    RefundResult,
    UsageResponse,
)

_DB_DOWN = (
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.InterfaceError,
    asyncpg.InternalServerError,
    OSError,
)


def fail_closed(fn):
    """Maps connection-level DB errors to DatabaseUnavailableError (HTTP 503)."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except _DB_DOWN as exc:
            raise DatabaseUnavailableError(
                f"Database connection failed ({type(exc).__name__}). "
                "Failing closed to prevent over-consumption."
            ) from exc

    return wrapper


class QuotaService:
    """Input validation and fail-closed error mapping on top of QuotaRepository."""

    def __init__(self, pool: asyncpg.Pool):
        self.repo = QuotaRepository(pool)

    @fail_closed
    async def consume(
        self,
        org_id: str,
        feature: str,
        units: int,
        idempotency_key: str,
        period: str | None = None,
    ) -> ConsumeResult:
        if units <= 0:
            raise ValueError("Units must be greater than zero")
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("Idempotency key must not be empty")
        return await self.repo.consume(
            org_id=org_id.strip(),
            feature=feature.strip(),
            units=units,
            idempotency_key=idempotency_key.strip(),
            operation_id=uuid.uuid4(),
            reset_at=get_next_reset_time(),
            period=period,
        )

    @fail_closed
    async def finalize(self, operation_id: uuid.UUID) -> FinalizeResult:
        return await self.repo.finalize(operation_id)

    @fail_closed
    async def refund(self, operation_id: uuid.UUID, reason: str = "") -> RefundResult:
        return await self.repo.refund(operation_id, reason)

    @fail_closed
    async def get_usage(
        self, org_id: str, feature: str, period: str | None = None
    ) -> UsageResponse:
        res = await self.repo.get_usage(org_id.strip(), feature.strip(), period)
        if res is None:
            raise ValueError(f"No quota configured for feature '{feature}'")
        return res

    @fail_closed
    async def set_default_limit(self, feature: str, limit: int) -> None:
        if limit < 0:
            raise ValueError("Limit cannot be negative")
        await self.repo.set_default_limit(feature.strip(), limit)

    @fail_closed
    async def set_override_limit(self, org_id: str, feature: str, limit: int) -> None:
        if limit < 0:
            raise ValueError("Limit cannot be negative")
        await self.repo.set_override_limit(org_id.strip(), feature.strip(), limit)

    @fail_closed
    async def update_current_period_limit(
        self, org_id: str, feature: str, new_limit: int, period: str | None = None
    ) -> UsageResponse:
        if new_limit < 0:
            raise ValueError("Limit cannot be negative")
        return await self.repo.update_current_period_limit(
            org_id.strip(), feature.strip(), new_limit, period
        )

    @fail_closed
    async def list_operations(
        self, status: str | None, older_than_seconds: int | None, limit: int
    ) -> list[dict]:
        return await self.repo.list_operations(status, older_than_seconds, limit)
