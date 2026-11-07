from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from quota.api import admin, health, quota
from quota.core.database import create_db_pool, init_db
from quota.core.exceptions import DatabaseUnavailableError


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Creates the connection pool unless one was injected (tests do this)."""
    if getattr(app.state, "db_pool", None) is not None:
        yield
        return
    pool = await create_db_pool()
    await init_db(pool)
    app.state.db_pool = pool
    try:
        yield
    finally:
        await pool.close()


async def db_unavailable_handler(request: Request, exc: DatabaseUnavailableError):
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
    )


async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


def create_application() -> FastAPI:
    app = FastAPI(title="Quota Metering Service", version="1.0.0", lifespan=lifespan)
    app.add_exception_handler(DatabaseUnavailableError, db_unavailable_handler)
    app.add_exception_handler(ValueError, value_error_handler)
    app.include_router(health.router)
    app.include_router(quota.router, prefix="/v1")
    app.include_router(admin.router, prefix="/v1")
    return app


app = create_application()
