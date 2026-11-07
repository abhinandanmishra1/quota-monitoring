from enum import Enum
from pydantic import BaseModel, Field

class OperationStatus(str, Enum):
    """Operation state machine states per DESIGN.md Section 4.3."""
    CHARGED = "CHARGED"
    FINALIZED = "FINALIZED"
    REFUNDED = "REFUNDED"
    REJECTED = "REJECTED"

class HealthResponse(BaseModel):
    status: str = Field("ok", description="Service status")
    database: str = Field("connected", description="Database connectivity state")
