import uuid
from datetime import datetime
import asyncpg
from quota.models import (
    ConsumeResult,
    FinalizeResult,
    RefundResult,
    UsageResponse,
)
from quota.period import get_current_period, get_next_reset_time
from quota.repository import QuotaRepository

class DatabaseUnavailableError(Exception):
    """Raised when PostgreSQL connection fails or database is unreachable (fail-closed)."""
    pass

class QuotaService:
    """Service layer encapsulating business logic, input validation, and database reliability."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.repo = QuotaRepository(pool)

    def _wrap_db_errors(self, exc: Exception) -> Exception:
        """Translates connection or fatal database errors to DatabaseUnavailableError."""
        if isinstance(exc, (asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError, OSError, ConnectionRefusedError)):
            return DatabaseUnavailableError("Database connection failed. Service is failing closed to prevent over-consumption.")
        return exc

    async def consume(
        self,
        org_id: str,
        feature: str,
        units: int,
        idempotency_key: str,
        period: str | None = None,
    ) -> ConsumeResult:
        """Validates input and requests quota consumption."""
        if units <= 0:
            raise ValueError("Units must be greater than zero")
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("Idempotency key must not be empty")

        op_id = uuid.uuid4()
        reset_at = get_next_reset_time()

        try:
            return await self.repo.consume(
                org_id=org_id.strip(),
                feature=feature.strip(),
                units=units,
                idempotency_key=idempotency_key.strip(),
                operation_id=op_id,
                reset_at=reset_at,
                period=period,
            )
        except Exception as e:
            wrapped = self._wrap_db_errors(e)
            if isinstance(wrapped, DatabaseUnavailableError):
                raise wrapped
            raise

    async def finalize(self, operation_id: uuid.UUID) -> FinalizeResult:
        """Finalizes a charged operation upon successful downstream completion."""
        try:
            return await self.repo.finalize(operation_id)
        except Exception as e:
            wrapped = self._wrap_db_errors(e)
            if isinstance(wrapped, DatabaseUnavailableError):
                raise wrapped
            raise

    async def refund(self, operation_id: uuid.UUID, reason: str = "") -> RefundResult:
        """Refunds a charged operation upon confirmed downstream failure."""
        try:
            return await self.repo.refund(operation_id, reason)
        except Exception as e:
            wrapped = self._wrap_db_errors(e)
            if isinstance(wrapped, DatabaseUnavailableError):
                raise wrapped
            raise

    async def get_usage(
        self, org_id: str, feature: str, period: str | None = None
    ) -> UsageResponse:
        """Retrieves usage and remaining quota for an organization and feature."""
        try:
            res = await self.repo.get_usage(org_id.strip(), feature.strip(), period)
            if res is None:
                raise ValueError(f"No quota configured for feature '{feature}'")
            return res
        except Exception as e:
            wrapped = self._wrap_db_errors(e)
            if isinstance(wrapped, DatabaseUnavailableError):
                raise wrapped
            raise

    async def set_default_limit(self, feature: str, limit: int) -> None:
        """Configures a default monthly limit for a feature."""
        if limit < 0:
            raise ValueError("Limit cannot be negative")
        try:
            await self.repo.set_default_limit(feature.strip(), limit)
        except Exception as e:
            wrapped = self._wrap_db_errors(e)
            if isinstance(wrapped, DatabaseUnavailableError):
                raise wrapped
            raise

    async def set_override_limit(self, org_id: str, feature: str, limit: int) -> None:
        """Configures an organization-specific monthly limit override."""
        if limit < 0:
            raise ValueError("Limit cannot be negative")
        try:
            await self.repo.set_override_limit(org_id.strip(), feature.strip(), limit)
        except Exception as e:
            wrapped = self._wrap_db_errors(e)
            if isinstance(wrapped, DatabaseUnavailableError):
                raise wrapped
            raise

    async def update_current_period_limit(
        self, org_id: str, feature: str, new_limit: int, period: str | None = None
    ) -> UsageResponse:
        """Updates the current period's quota limit."""
        if new_limit < 0:
            raise ValueError("Limit cannot be negative")
        try:
            return await self.repo.update_current_period_limit(
                org_id.strip(), feature.strip(), new_limit, period
            )
        except Exception as e:
            wrapped = self._wrap_db_errors(e)
            if isinstance(wrapped, DatabaseUnavailableError):
                raise wrapped
            raise
