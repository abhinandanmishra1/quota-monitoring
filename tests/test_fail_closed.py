import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI
from quota.api import app
from quota.service import DatabaseUnavailableError

@pytest.mark.asyncio
async def test_database_outage_fails_closed_with_503():
    """
    DESIGN.md Section 6:
    When PostgreSQL is unavailable, the service fails closed with 503 Service Unavailable.
    Stale balances are never used.
    """
    # Create an app instance with a non-existent / broken database pool
    bad_app = FastAPI()
    # Import and register the same exception handlers and routes
    from quota.api import consume_quota, health_check, db_unavailable_handler
    bad_app.add_exception_handler(DatabaseUnavailableError, db_unavailable_handler)
    bad_app.add_api_route("/v1/quota/consume", consume_quota, methods=["POST"])
    bad_app.add_api_route("/health", health_check, methods=["GET"])
    bad_app.state.db_pool = None  # DB is down / pool unavailable

    transport = ASGITransport(app=bad_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Health check returns 503
        health_resp = await client.get("/health")
        assert health_resp.status_code == 503

        # 2. Consume request returns 503 fail-closed
        consume_resp = await client.post(
            "/v1/quota/consume",
            json={
                "org_id": "org_failclosed",
                "feature": "ai_inference",
                "units": 10,
                "idempotency_key": "fail_key",
            },
        )
        assert consume_resp.status_code == 503
        assert "Database" in consume_resp.json()["detail"]
