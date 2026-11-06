import os
from pydantic import BaseModel, Field

class Settings(BaseModel):
    # PostgreSQL connection string
    DATABASE_URL: str = Field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "postgresql://localhost/quota_metering"
        )
    )
    # Connection pool configuration
    DB_POOL_MIN: int = Field(default_factory=lambda: int(os.getenv("DB_POOL_MIN", "5")))
    DB_POOL_MAX: int = Field(default_factory=lambda: int(os.getenv("DB_POOL_MAX", "20")))
    # Command timeout in seconds
    DB_TIMEOUT: float = Field(default_factory=lambda: float(os.getenv("DB_TIMEOUT", "5.0")))

settings = Settings()
