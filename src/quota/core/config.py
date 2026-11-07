import os
from functools import lru_cache
from pydantic import BaseModel, Field

class Settings(BaseModel):
    DATABASE_URL: str = Field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "postgresql://localhost/quota_metering"
        )
    )
    DB_POOL_MIN: int = Field(default_factory=lambda: int(os.getenv("DB_POOL_MIN", "5")))
    DB_POOL_MAX: int = Field(default_factory=lambda: int(os.getenv("DB_POOL_MAX", "20")))
    DB_TIMEOUT: float = Field(default_factory=lambda: float(os.getenv("DB_TIMEOUT", "5.0")))
    SERVICE_PORT: int = Field(default_factory=lambda: int(os.getenv("PORT", "8000")))

@lru_cache()
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
