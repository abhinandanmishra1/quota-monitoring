# Tests state-transition races (DESIGN.md 6: conditional transitions).
# Case: test_racing_finalize_and_refund_transitions
#   - finalize and refund fired concurrently on one CHARGED operation: exactly one gets 200,
#     the other 409, and the operation ends in a single valid state
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from quota.main import app
from quota.core.database import init_db

@pytest.mark.asyncio
async def test_racing_finalize_and_refund_transitions(db_pool):
    """
    DESIGN.md Section 6:
    'finalize and refund are conditional on the operation still being CHARGED,
     ensuring only one transition can succeed if both requests race.'
    """
    await init_db(db_pool)
    app.state.db_pool = db_pool

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/admin/quota/limits/default",
            json={"feature": "transcode", "monthly_limit": 100},
        )

        # Consume 50 units
        consume_res = await client.post(
            "/v1/quota/consume",
            json={"org_id": "org_race", "feature": "transcode", "units": 50, "idempotency_key": "race_op"},
        )
        assert consume_res.status_code == 200
        op_id = consume_res.json()["operation_id"]

        # Race finalize and refund concurrently
        async def call_finalize():
            return await client.post("/v1/quota/finalize", json={"operation_id": op_id})

        async def call_refund():
            return await client.post("/v1/quota/refund", json={"operation_id": op_id, "reason": "race test"})

        res_fin, res_ref = await asyncio.gather(call_finalize(), call_refund())

        # Exactly one MUST succeed with 200, and the other MUST be rejected with 409
        status_codes = sorted([res_fin.status_code, res_ref.status_code])
        assert status_codes == [200, 409], f"Expected [200, 409], got {status_codes}"

        # Operation must NOT be in an invalid or split state
        async with db_pool.acquire() as conn:
            final_status = await conn.fetchval(
                "SELECT status FROM quota_operations WHERE operation_id = $1;",
                consume_res.json()["operation_id"],
            )
            assert final_status in ("FINALIZED", "REFUNDED")
