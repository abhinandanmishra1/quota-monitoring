from pydantic import BaseModel, Field

class DefaultLimitRequest(BaseModel):
    feature: str = Field(..., min_length=1, max_length=100, description="Feature identifier")
    monthly_limit: int = Field(..., ge=0, description="Monthly default limit (>= 0)")

class OverrideLimitRequest(BaseModel):
    org_id: str = Field(..., min_length=1, max_length=100, description="Organization identifier")
    feature: str = Field(..., min_length=1, max_length=100, description="Feature identifier")
    monthly_limit: int = Field(..., ge=0, description="Monthly override limit (>= 0)")

class UpdateCurrentPeriodLimitRequest(BaseModel):
    org_id: str = Field(..., min_length=1, max_length=100, description="Organization identifier")
    feature: str = Field(..., min_length=1, max_length=100, description="Feature identifier")
    new_limit: int = Field(..., ge=0, description="New quota limit for current period")
