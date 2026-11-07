from fastapi import APIRouter, Depends, Query

from quota.api.dependencies import get_quota_service
from quota.schemas.admin import (
    DefaultLimitRequest,
    OverrideLimitRequest,
    UpdateCurrentPeriodLimitRequest,
)
from quota.services.quota_service import QuotaService

router = APIRouter(prefix="/admin/quota", tags=["Admin"])


@router.post("/limits/default")
async def set_default_limit(
    req: DefaultLimitRequest, svc: QuotaService = Depends(get_quota_service)
):
    """Sets the feature-level monthly default."""
    await svc.set_default_limit(req.feature, req.monthly_limit)
    return {"status": "ok", "feature": req.feature, "monthly_limit": req.monthly_limit}


@router.post("/limits/override")
async def set_override_limit(
    req: OverrideLimitRequest, svc: QuotaService = Depends(get_quota_service)
):
    """Sets an organization-specific override for a feature."""
    await svc.set_override_limit(req.org_id, req.feature, req.monthly_limit)
    return {
        "status": "ok",
        "org_id": req.org_id,
        "feature": req.feature,
        "monthly_limit": req.monthly_limit,
    }


@router.patch("/limits/current-period")
async def update_current_period_limit(
    req: UpdateCurrentPeriodLimitRequest,
    svc: QuotaService = Depends(get_quota_service),
):
    """Updates this period's limit; a limit below consumed usage is rejected (400)."""
    return await svc.update_current_period_limit(req.org_id, req.feature, req.new_limit)


@router.get("/operations")
async def list_operations(
    status: str | None = Query(None, description="Filter by operation status"),
    older_than_seconds: int | None = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=500),
    svc: QuotaService = Depends(get_quota_service),
):
    """Lists operations for reconciliation and auditing."""
    return await svc.list_operations(status, older_than_seconds, limit)
