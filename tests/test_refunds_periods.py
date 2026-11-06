import pytest
import uuid
from httpx import AsyncClient, ASGITransport
from quota.api import app
from quota.database import init_db
from quota.service import QuotaService
from quota.period import get_next_reset_time

@pytest.mark.asyncio
async def test_cross_month_refund_isolation(db_pool):
    """
    DESIGN.md Section 4.4 & Section 5:
    - Operation charged in period '2026-10'.
    - In period '2026-11', refund request arrives for that operation.
    - Refund must decrement '2026-10' usage row, NOT '2026-11'.
    """
    await init_db(db_pool)
    app.state.db_pool = db_pool

    svc = QuotaService(db_pool)
    await svc.set_default_limit("storage", 100)

    # 1. Charge 40 units in period '2026-10'
    res_oct = await svc.consume(
        org_id="org_cross",
        feature="storage",
        units=40,
        idempotency_key="oct_charge",
        period="2026-10",
    )
    assert res_oct.success is True
    assert res_oct.remaining == 60
    op_oct_id = res_oct.operation_id

    # 2. Charge 20 units in current period '2026-11'
    res_nov = await svc.consume(
        org_id="org_cross",
        feature="storage",
        units=20,
        idempotency_key="nov_charge",
        period="2026-11",
    )
    assert res_nov.success is True
    assert res_nov.remaining == 80

    # Verify usage in both periods before refund
    usage_oct_before = await svc.get_usage("org_cross", "storage", period="2026-10")
    assert usage_oct_before.used == 40

    usage_nov_before = await svc.get_usage("org_cross", "storage", period="2026-11")
    assert usage_nov_before.used == 20

    # 3. Now execute refund on the October operation
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ref_resp = await client.post(
            "/v1/quota/refund",
            json={"operation_id": op_oct_id, "reason": "delayed downstream failure"},
        )
        assert ref_resp.status_code == 200
        assert ref_resp.json()["status"] == "REFUNDED"
        assert ref_resp.json()["refunded_units"] == 40

    # 4. Assert: October used is now 0, November used is still 20!
    usage_oct_after = await svc.get_usage("org_cross", "storage", period="2026-10")
    assert usage_oct_after.used == 0
    assert usage_oct_after.remaining == 100

    usage_nov_after = await svc.get_usage("org_cross", "storage", period="2026-11")
    assert usage_nov_after.used == 20
    assert usage_nov_after.remaining == 80
