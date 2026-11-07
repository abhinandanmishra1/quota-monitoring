from fastapi import APIRouter, HTTPException, Request, status
from quota.schemas.common import HealthResponse

router = APIRouter()

@router.get("/health", response_model=HealthResponse, tags=["Monitoring"])
async def health_check(request: Request):
    """Checks PostgreSQL connectivity. Fails closed with 503 if unavailable."""
    if not hasattr(request.app.state, "db_pool") or request.app.state.db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database pool not initialized",
        )
    try:
        async with request.app.state.db_pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1;")
            if val != 1:
                raise Exception("Invalid database response")
        return HealthResponse(status="ok", database="connected")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database health check failed: {str(e)}",
        )
