import uuid
from datetime import datetime
from pydantic import BaseModel, Field
from quota.schemas.common import OperationStatus

class ConsumeRequest(BaseModel):
    org_id: str = Field(..., min_length=1, max_length=100, description="Organization identifier")
    feature: str = Field(..., min_length=1, max_length=100, description="Feature identifier")
    units: int = Field(..., gt=0, description="Units to consume (must be > 0)")
    idempotency_key: str = Field(..., min_length=1, max_length=255, description="Client unique request key")

class FinalizeRequest(BaseModel):
    operation_id: uuid.UUID = Field(..., description="Operation UUID to finalize")

class RefundRequest(BaseModel):
    operation_id: uuid.UUID = Field(..., description="Operation UUID to refund")
    reason: str = Field(default="", description="Reason for downstream failure refund")

class ConsumeResponse(BaseModel):
    operation_id: str = Field(..., description="Unique operation identifier")
    status: OperationStatus = Field(..., description="Operation status")
    units: int = Field(..., description="Allocated units")
    remaining: int = Field(..., description="Remaining quota units")
    reset_at: datetime = Field(..., description="Next billing period reset time")
    period: str = Field(..., description="Billing period (YYYY-MM)")

class FinalizeResponse(BaseModel):
    operation_id: str
    status: OperationStatus

class RefundResponse(BaseModel):
    operation_id: str
    status: OperationStatus
    refunded_units: int
    remaining: int

class UsageResponse(BaseModel):
    org_id: str
    feature: str
    period: str
    quota_limit: int
    used: int
    remaining: int
    reset_at: datetime

# Internal Result models used between Repository/Service and API
class ConsumeResult(BaseModel):
    success: bool
    status_code: int
    operation_id: str
    status: str
    units: int
    remaining: int
    reset_at: datetime
    period: str
    detail: str | None = None

class FinalizeResult(BaseModel):
    success: bool
    status_code: int
    operation_id: str
    status: str
    detail: str | None = None

class RefundResult(BaseModel):
    success: bool
    status_code: int
    operation_id: str
    status: str
    refunded_units: int = 0
    remaining: int = 0
    detail: str | None = None
