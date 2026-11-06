import uuid
from enum import Enum
from datetime import datetime
from pydantic import BaseModel, Field

class OperationStatus(str, Enum):
    CHARGED = "CHARGED"
    FINALIZED = "FINALIZED"
    REFUNDED = "REFUNDED"
    REJECTED = "REJECTED"

# --- Request Models ---

class ConsumeRequest(BaseModel):
    org_id: str = Field(..., description="Organization identifier")
    feature: str = Field(..., description="Feature identifier")
    units: int = Field(..., gt=0, description="Number of units to consume (must be > 0)")
    idempotency_key: str = Field(..., min_length=1, max_length=255, description="Client-generated unique key")

class FinalizeRequest(BaseModel):
    operation_id: uuid.UUID = Field(..., description="Operation UUID to finalize")

class RefundRequest(BaseModel):
    operation_id: uuid.UUID = Field(..., description="Operation UUID to refund")
    reason: str = Field(default="", description="Optional failure reason")

class DefaultLimitRequest(BaseModel):
    feature: str = Field(..., description="Feature identifier")
    monthly_limit: int = Field(..., ge=0, description="Monthly default limit (>= 0)")

class OverrideLimitRequest(BaseModel):
    org_id: str = Field(..., description="Organization identifier")
    feature: str = Field(..., description="Feature identifier")
    monthly_limit: int = Field(..., ge=0, description="Monthly override limit (>= 0)")

class UpdateCurrentPeriodLimitRequest(BaseModel):
    org_id: str = Field(..., description="Organization identifier")
    feature: str = Field(..., description="Feature identifier")
    new_limit: int = Field(..., ge=0, description="New quota limit for current period")

# --- Response Models ---

class ConsumeResponse(BaseModel):
    operation_id: str
    status: OperationStatus
    units: int
    remaining: int
    reset_at: datetime
    period: str

class InsufficientQuotaDetail(BaseModel):
    detail: str = "Quota limit exceeded"
    requested: int
    remaining: int
    reset_at: datetime

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
