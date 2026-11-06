from contextlib import asynccontextmanager
from typing import Annotated, Optional
import asyncpg
from fastapi import FastAPI, HTTPException, Query, status, Request
from fastapi.responses import JSONResponse

from quota.config import settings
from quota.database import create_db_pool, init_db
from quota.models import (
    ConsumeRequest,
    ConsumeResponse,
    FinalizeRequest,
    FinalizeResponse,
    RefundRequest,
    RefundResponse,
    UsageResponse,
    DefaultLimitRequest,
    OverrideLimitRequest,
    UpdateCurrentPeriodLimitRequest,
)
from quota.service import QuotaService, DatabaseUnavailableError

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize connection pool if not already injected (e.g. during testing)
    if not hasattr(app.state, "db_pool") or app.state.db_pool is None:
        pool = await create_db_pool()
        await init_db(pool)
        app.state.db_pool = pool
        yield
        await pool.close()
    else:
        yield

app = FastAPI(
    title="Quota Metering Service",
    description="Per-customer quota metering service adhering to DESIGN.md",
    version="0.1.0",
    lifespan=lifespan,
)

def get_service(request: Request) -> QuotaService:
    if not hasattr(request.app.state, "db_pool") or request.app.state.db_pool is None:
        raise DatabaseUnavailableError("Database connection pool is not available")
    return QuotaService(request.app.state.db_pool)

# Global Exception Handlers for Database Fail-Closed and Validation
@app.exception_handler(DatabaseUnavailableError)
async def db_unavailable_handler(request: Request, exc: DatabaseUnavailableError):
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
    )

@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )

# --- Healthcheck Endpoint ---

@app.get("/health", tags=["Monitoring"])
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
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database health check failed: {str(e)}",
        )

# --- Core Quota Endpoints ---

@app.post(
    "/v1/quota/consume",
    response_model=ConsumeResponse,
    status_code=status.HTTP_200_OK,
    tags=["Quota Operations"],
)
async def consume_quota(req: ConsumeRequest, request: Request):
    """
    Atomically consumes quota units for an organization and feature.
    - All-or-nothing units: rejects with 429 if insufficient.
    - Idempotent: same key and payload returns previous response without charging again.
    - Conflict: same key with different payload returns 409 Conflict.
    """
    svc = get_service(request)
    result = await svc.consume(
        org_id=req.org_id,
        feature=req.feature,
        units=req.units,
        idempotency_key=req.idempotency_key,
    )

    if result.status_code == 200:
        return ConsumeResponse(
            operation_id=result.operation_id,
            status=result.status,
            units=result.units,
            remaining=result.remaining,
            reset_at=result.reset_at,
            period=result.period,
        )
    elif result.status_code == 429:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "detail": result.detail or "Quota limit exceeded",
                "requested": result.units,
                "remaining": result.remaining,
                "reset_at": result.reset_at.isoformat(),
            },
        )
    elif result.status_code == 409:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.detail or "Idempotency conflict",
        )
    elif result.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.detail or "Resource not found",
        )
    else:
        raise HTTPException(
            status_code=result.status_code,
            detail=result.detail or "Operation failed",
        )

@app.post(
    "/v1/quota/finalize",
    response_model=FinalizeResponse,
    tags=["Quota Operations"],
)
async def finalize_quota(req: FinalizeRequest, request: Request):
    """Marks a CHARGED operation as FINALIZED upon downstream completion."""
    svc = get_service(request)
    result = await svc.finalize(req.operation_id)
    if result.status_code == 200:
        return FinalizeResponse(
            operation_id=result.operation_id,
            status=result.status,
        )
    elif result.status_code == 404:
        raise HTTPException(status_code=404, detail=result.detail)
    elif result.status_code == 409:
        raise HTTPException(status_code=409, detail=result.detail)
    else:
        raise HTTPException(status_code=result.status_code, detail=result.detail)

@app.post(
    "/v1/quota/refund",
    response_model=RefundResponse,
    tags=["Quota Operations"],
)
async def refund_quota(req: RefundRequest, request: Request):
    """
    Refunds units for a CHARGED operation upon confirmed downstream failure.
    Deducts units from the original billing period recorded on the operation.
    """
    svc = get_service(request)
    result = await svc.refund(req.operation_id, req.reason)
    if result.status_code == 200:
        return RefundResponse(
            operation_id=result.operation_id,
            status=result.status,
            refunded_units=result.refunded_units,
            remaining=result.remaining,
        )
    elif result.status_code == 404:
        raise HTTPException(status_code=404, detail=result.detail)
    elif result.status_code == 409:
        raise HTTPException(status_code=409, detail=result.detail)
    else:
        raise HTTPException(status_code=result.status_code, detail=result.detail)

@app.get(
    "/v1/quota/usage",
    response_model=UsageResponse,
    tags=["Quota Usage"],
)
async def get_quota_usage(
    request: Request,
    org_id: str = Query(..., description="Organization identifier"),
    feature: str = Query(..., description="Feature identifier"),
):
    """
    Returns total usage, limit, remaining quota, and next reset time for an organization.
    Returns 200 OK with used = 0 if no usage record exists yet for current period.
    """
    svc = get_service(request)
    try:
        return await svc.get_usage(org_id, feature)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

# --- Admin Configuration Endpoints ---

@app.post("/v1/admin/quota/limits/default", tags=["Admin"])
async def set_default_limit(req: DefaultLimitRequest, request: Request):
    """Sets feature-level monthly quota default."""
    svc = get_service(request)
    await svc.set_default_limit(req.feature, req.monthly_limit)
    return {"status": "ok", "feature": req.feature, "monthly_limit": req.monthly_limit}

@app.post("/v1/admin/quota/limits/override", tags=["Admin"])
async def set_override_limit(req: OverrideLimitRequest, request: Request):
    """Sets organization-specific quota override for a feature."""
    svc = get_service(request)
    await svc.set_override_limit(req.org_id, req.feature, req.monthly_limit)
    return {
        "status": "ok",
        "org_id": req.org_id,
        "feature": req.feature,
        "monthly_limit": req.monthly_limit,
    }

@app.patch("/v1/admin/quota/limits/current-period", tags=["Admin"])
async def update_current_period_limit(req: UpdateCurrentPeriodLimitRequest, request: Request):
    """
    Updates the current period's limit for an organization and feature.
    Rejects any limit below the already consumed usage.
    """
    svc = get_service(request)
    try:
        return await svc.update_current_period_limit(
            req.org_id, req.feature, req.new_limit
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/v1/admin/quota/operations", tags=["Admin"])
async def list_operations(
    status: Optional[str] = Query(None, description="Filter by operation status"),
    older_than_seconds: Optional[int] = Query(None, description="Filter operations created older than X seconds"),
    limit: int = Query(50, ge=1, le=500, description="Max operations to return"),
):
    """Inspects operations for reconciliation and auditing."""
    if not hasattr(app.state, "db_pool") or app.state.db_pool is None:
        raise DatabaseUnavailableError("Database pool unavailable")
    
    query = "SELECT operation_id, org_id, feature, idempotency_key, request_units, period, status, created_at, updated_at FROM quota_operations WHERE 1=1"
    params = []
    idx = 1
    if status:
        query += f" AND status = ${idx}"
        params.append(status.upper())
        idx += 1
    if older_than_seconds is not None:
        query += f" AND created_at <= NOW() - INTERVAL '1 second' * ${idx}"
        params.append(older_than_seconds)
        idx += 1
    query += f" ORDER BY created_at DESC LIMIT ${idx};"
    params.append(limit)

    async with app.state.db_pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
        return [
            {
                "operation_id": str(r["operation_id"]),
                "org_id": r["org_id"],
                "feature": r["feature"],
                "idempotency_key": r["idempotency_key"],
                "request_units": r["request_units"],
                "period": r["period"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat(),
                "updated_at": r["updated_at"].isoformat(),
            }
            for r in rows
        ]
