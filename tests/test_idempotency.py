import pytest
from httpx import AsyncClient, ASGITransport
from quota.api import app
from quota.database import init_db

@pytest.mark.asyncio
async def test_idempotent_retry_and_conflict_handling(db_pool):
    """
    DESIGN.md Section 4.3 & 6:
    - Same key and same payload: return stored result; do not charge again.
    - Same key with different payload: return 409 Conflict.
    - Retrying a rejected operation returns stored 429 without charging.
    """
    await init_db(db_pool)
    app.state.db_pool = db_pool

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/admin/quota/limits/default",
            json={"feature": "notifications", "monthly_limit": 50},
        )

        # 1. First consume: 20 units
        res1 = await client.post(
            "/v1/quota/consume",
            json={"org_id": "org_idem", "feature": "notifications", "units": 20, "idempotency_key": "k100"},
        )
        assert res1.status_code == 200
        op_id = res1.json()["operation_id"]
        assert res1.json()["remaining"] == 30

        # 2. Replay with identical payload -> 200 OK, same op_id, same remaining
        res2 = await client.post(
            "/v1/quota/consume",
            json={"org_id": "org_idem", "feature": "notifications", "units": 20, "idempotency_key": "k100"},
        )
        assert res2.status_code == 200
        assert res2.json()["operation_id"] == op_id
        assert res2.json()["remaining"] == 30

        # Verify DB counter was not incremented twice
        usage = await client.get("/v1/quota/usage?org_id=org_idem&feature=notifications")
        assert usage.json()["used"] == 20

        # 3. Replay with different payload (units: 25 instead of 20) -> 409 Conflict
        res3 = await client.post(
            "/v1/quota/consume",
            json={"org_id": "org_idem", "feature": "notifications", "units": 25, "idempotency_key": "k100"},
        )
        assert res3.status_code == 409
        assert "Idempotency key reused" in res3.json()["detail"]

        # 4. Attempt to consume 40 units -> exceeds remaining 30 -> 429
        res4 = await client.post(
            "/v1/quota/consume",
            json={"org_id": "org_idem", "feature": "notifications", "units": 40, "idempotency_key": "k101"},
        )
        assert res4.status_code == 429
        assert res4.json()["detail"] == "Quota limit exceeded"

        # 5. Retry the rejected request -> replays 429 without side-effects
        res5 = await client.post(
            "/v1/quota/consume",
            json={"org_id": "org_idem", "feature": "notifications", "units": 40, "idempotency_key": "k101"},
        )
        assert res5.status_code == 429
        assert res5.json()["detail"] == "Quota limit exceeded"
