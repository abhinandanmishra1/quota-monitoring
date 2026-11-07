# Tests the sample consumer and reconciliation (DESIGN.md 3, 4.3).
# Case: test_sample_consumer_and_reconciliation
#   - downstream success leads to consume then finalize
#   - confirmed downstream failure leads to consume then refund
#   - downstream timeout leaves the operation CHARGED (no refund)
#   - reconciliation reports the stale CHARGED operation
import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from quota.main import app
from quota.core.database import init_db
from quota.client.consumer import SampleDownstreamConsumer
from quota.reconciliation.worker import find_stale_charged_operations

@pytest.mark.asyncio
async def test_sample_consumer_and_reconciliation(db_pool):
    await init_db(db_pool)
    app.state.db_pool = db_pool

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Set limit
        await client.post(
            "/v1/admin/quota/limits/default",
            json={"feature": "ai_inference", "monthly_limit": 100},
        )

        consumer = SampleDownstreamConsumer(client)

        # 1. Successful downstream execution -> consumes and finalizes
        async def mock_downstream_success():
            return "SUCCESS"

        res1 = await consumer.execute_task(
            org_id="org_1",
            feature="ai_inference",
            units=20,
            idempotency_key="task_1",
            downstream_fn=mock_downstream_success,
        )
        assert res1["status"] == "COMPLETED"
        assert res1["quota_status"] == "FINALIZED"

        # 2. Confirmed downstream failure -> consumes and refunds
        async def mock_downstream_failure():
            raise RuntimeError("Downstream service rejected transaction (confirmed)")

        res2 = await consumer.execute_task(
            org_id="org_1",
            feature="ai_inference",
            units=30,
            idempotency_key="task_2",
            downstream_fn=mock_downstream_failure,
        )
        assert res2["status"] == "FAILED_REFUNDED"
        assert res2["quota_status"] == "REFUNDED"

        # Check usage: 20 used, 80 remaining
        usage_res = await client.get("/v1/quota/usage?org_id=org_1&feature=ai_inference")
        assert usage_res.json()["used"] == 20
        assert usage_res.json()["remaining"] == 80

        # 3. Downstream timeout -> remains CHARGED (no refund per Section 4.3 & 6)
        async def mock_downstream_timeout():
            raise asyncio.TimeoutError("Downstream network timeout")

        res3 = await consumer.execute_task(
            org_id="org_1",
            feature="ai_inference",
            units=15,
            idempotency_key="task_3",
            downstream_fn=mock_downstream_timeout,
        )
        assert res3["status"] == "TIMEOUT_AMBIGUOUS"
        assert res3["quota_status"] == "CHARGED"

        # 4. Reconciliation: finds stale CHARGED operations older than 0 seconds
        stale_ops = await find_stale_charged_operations(db_pool, max_age_seconds=0)
        assert len(stale_ops) >= 1
        stale_op_ids = [str(r["operation_id"]) for r in stale_ops]
        assert res3["operation_id"] in stale_op_ids
