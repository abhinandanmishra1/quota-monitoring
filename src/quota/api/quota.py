from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from quota.api.dependencies import get_quota_service
from quota.schemas.quota import (
    ConsumeRequest,
    ConsumeResponse,
    FinalizeRequest,
    FinalizeResponse,
    RefundRequest,
    RefundResponse,
    UsageResponse,
)
from quota.services.quota_service import QuotaService

router = APIRouter()

@router.post(
    "/quota/consume",
    response_model=ConsumeResponse,
    status_code=status.HTTP_200_OK,
    tags=["Quota Operations"],
)
async def consume_quota(
    req: ConsumeRequest,
    svc: QuotaService = Depends(get_quota_service),
):
    """
    Atomically consumes quota units for an organization and feature.
    - All-or-nothing units: rejects with 429 if insufficient.
    - Idempotent: same key and payload returns previous response without charging again.
    - Conflict: same key with different payload returns 409 Conflict.
    """
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

@router.post(
    "/quota/finalize",
    response_model=FinalizeResponse,
    tags=["Quota Operations"],
)
async def finalize_quota(
    req: FinalizeRequest,
    svc: QuotaService = Depends(get_quota_service),
):
    """Marks a CHARGED operation as FINALIZED upon downstream completion."""
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

@router.post(
    "/quota/refund",
    response_model=RefundResponse,
    tags=["Quota Operations"],
)
async def refund_quota(
    req: RefundRequest,
    svc: QuotaService = Depends(get_quota_service),
):
    """
    Refunds units for a CHARGED operation upon confirmed downstream failure.
    Deducts units from the original billing period recorded on the operation.
    """
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

@router.get(
    "/quota/usage",
    response_model=UsageResponse,
    tags=["Quota Usage"],
)
async def get_quota_usage(
    org_id: str = Query(..., description="Organization identifier"),
    feature: str = Query(..., description="Feature identifier"),
    svc: QuotaService = Depends(get_quota_service),
):
    """
    Returns total usage, limit, remaining quota, and next reset time for an organization.
    Returns 200 OK with used = 0 if no usage record exists yet for current period.
    """
    try:
        return await svc.get_usage(org_id, feature)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
