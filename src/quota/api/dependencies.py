from fastapi import Request
from quota.core.exceptions import DatabaseUnavailableError
from quota.services.quota_service import QuotaService

def get_quota_service(request: Request) -> QuotaService:
    """Dependency injection provider for QuotaService from application state."""
    if not hasattr(request.app.state, "db_pool") or request.app.state.db_pool is None:
        raise DatabaseUnavailableError("Database connection pool is not available")
    return QuotaService(request.app.state.db_pool)
