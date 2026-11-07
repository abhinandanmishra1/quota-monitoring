# Tests the HTTP API end to end (DESIGN.md 4.3, 4.4).
# Case: test_api_full_lifecycle
#   - /health, set default limit, usage with no row yet (used=0, 200)
#   - consume, same-key replay (no second charge), same key different units (409)
#   - over-limit consume (429), finalize, consume then refund
import pytest
from httpx import AsyncClient, ASGITransport
from quota.main import app
from quota.core.database import init_db

@pytest.mark.asyncio
async def test_api_full_lifecycle(db_pool):
    # Initialize DB schema
    await init_db(db_pool)
    app.state.db_pool = db_pool

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Health check
        health_res = await client.get("/health")
        assert health_res.status_code == 200
        assert health_res.json()["status"] == "ok"

        # 2. Set default limit
        set_res = await client.post(
            "/v1/admin/quota/limits/default",
            json={"feature": "reports", "monthly_limit": 100},
        )
        assert set_res.status_code == 200

        # 3. Check initial usage (lazy row creation per Section 4.4: used=0, limit=100)
        usage_res = await client.get("/v1/quota/usage?org_id=acme&feature=reports")
        assert usage_res.status_code == 200
        usage_data = usage_res.json()
        assert usage_data["used"] == 0
        assert usage_data["remaining"] == 100
        assert usage_data["quota_limit"] == 100

        # 4. Consume 40 units -> 200 OK
        consume_res = await client.post(
            "/v1/quota/consume",
            json={"org_id": "acme", "feature": "reports", "units": 40, "idempotency_key": "req_1"},
        )
        assert consume_res.status_code == 200
        consume_data = consume_res.json()
        assert consume_data["status"] == "CHARGED"
        assert consume_data["remaining"] == 60
        op_id = consume_data["operation_id"]

        # 5. Retry same consume request -> returns stored result, remaining still 60
        retry_res = await client.post(
            "/v1/quota/consume",
            json={"org_id": "acme", "feature": "reports", "units": 40, "idempotency_key": "req_1"},
        )
        assert retry_res.status_code == 200
        assert retry_res.json()["operation_id"] == op_id
        assert retry_res.json()["remaining"] == 60

        # 6. Retry same key with different units -> 409 Conflict
        conflict_res = await client.post(
            "/v1/quota/consume",
            json={"org_id": "acme", "feature": "reports", "units": 10, "idempotency_key": "req_1"},
        )
        assert conflict_res.status_code == 409

        # 7. Consume 70 units -> exceeds remaining 60 -> 429 Too Many Requests
        excess_res = await client.post(
            "/v1/quota/consume",
            json={"org_id": "acme", "feature": "reports", "units": 70, "idempotency_key": "req_2"},
        )
        assert excess_res.status_code == 429
        assert excess_res.json()["detail"] == "Quota limit exceeded"
        assert excess_res.json()["remaining"] == 60

        # 8. Finalize operation
        fin_res = await client.post("/v1/quota/finalize", json={"operation_id": op_id})
        assert fin_res.status_code == 200
        assert fin_res.json()["status"] == "FINALIZED"

        # 9. Consume 30 units and refund on failure
        res3 = await client.post(
            "/v1/quota/consume",
            json={"org_id": "acme", "feature": "reports", "units": 30, "idempotency_key": "req_3"},
        )
        assert res3.status_code == 200
        op3_id = res3.json()["operation_id"]
        assert res3.json()["remaining"] == 30

        # Refund operation 3
        refund_res = await client.post(
            "/v1/quota/refund",
            json={"operation_id": op3_id, "reason": "downstream failure"},
        )
        assert refund_res.status_code == 200
        assert refund_res.json()["status"] == "REFUNDED"
        assert refund_res.json()["refunded_units"] == 30
        assert refund_res.json()["remaining"] == 60
