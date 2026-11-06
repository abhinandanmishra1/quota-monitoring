import pytest
import uuid
from quota.repository import QuotaRepository
from quota.period import get_current_period, get_next_reset_time

@pytest.mark.asyncio
async def test_atomic_consume_and_insufficient_quota(db_pool):
    repo = QuotaRepository(db_pool)
    period = get_current_period()
    reset_at = get_next_reset_time()

    # Configure default limit of 100 for 'ai_tokens'
    await repo.set_default_limit("ai_tokens", 100)

    # 1. Consume 60 units -> Success, remaining = 40
    res1 = await repo.consume("org_1", "ai_tokens", 60, "idem_1", uuid.uuid4(), reset_at)
    assert res1.success is True
    assert res1.status_code == 200
    assert res1.remaining == 40
    assert res1.status == "CHARGED"

    # 2. Consume 50 units -> Fails (insufficient quota), remaining = 40 (nothing deducted)
    res2 = await repo.consume("org_1", "ai_tokens", 50, "idem_2", uuid.uuid4(), reset_at)
    assert res2.success is False
    assert res2.status_code == 429
    assert res2.remaining == 40
    assert res2.detail == "Quota limit exceeded"

    # 3. Consume remaining 40 units -> Success, remaining = 0
    res3 = await repo.consume("org_1", "ai_tokens", 40, "idem_3", uuid.uuid4(), reset_at)
    assert res3.success is True
    assert res3.remaining == 0

@pytest.mark.asyncio
async def test_idempotency_same_key_same_payload_and_different_payload(db_pool):
    repo = QuotaRepository(db_pool)
    reset_at = get_next_reset_time()
    await repo.set_default_limit("emails", 100)

    # 1. First consume
    res1 = await repo.consume("org_1", "emails", 25, "key_x", uuid.uuid4(), reset_at)
    assert res1.success is True
    assert res1.remaining == 75

    # 2. Replay with same key and same payload -> returns stored result, does not charge again
    res2 = await repo.consume("org_1", "emails", 25, "key_x", uuid.uuid4(), reset_at)
    assert res2.success is True
    assert res2.operation_id == res1.operation_id
    assert res2.remaining == 75

    # Check actual DB used is still 25
    usage = await repo.get_usage("org_1", "emails")
    assert usage.used == 25

    # 3. Replay with same key but different payload -> 409 Conflict
    res3 = await repo.consume("org_1", "emails", 30, "key_x", uuid.uuid4(), reset_at)
    assert res3.success is False
    assert res3.status_code == 409

@pytest.mark.asyncio
async def test_finalize_and_refund_lifecycle(db_pool):
    repo = QuotaRepository(db_pool)
    reset_at = get_next_reset_time()
    await repo.set_default_limit("search", 100)

    # Consume 20
    op_id = uuid.uuid4()
    res = await repo.consume("org_1", "search", 20, "search_1", op_id, reset_at)
    assert res.success is True
    assert res.status == "CHARGED"

    # Finalize
    fin_res = await repo.finalize(op_id)
    assert fin_res.status == "FINALIZED"

    # Attempt to refund after finalize -> should fail because status is not CHARGED
    ref_res = await repo.refund(op_id, "downstream timeout")
    assert ref_res.success is False
    assert ref_res.status_code == 409

    # Now consume another operation and test successful refund
    op_id2 = uuid.uuid4()
    res2 = await repo.consume("org_1", "search", 30, "search_2", op_id2, reset_at)
    assert res2.success is True
    assert res2.remaining == 50

    # Confirmed failure refund
    ref_res2 = await repo.refund(op_id2, "downstream 500 error")
    assert ref_res2.success is True
    assert ref_res2.status == "REFUNDED"
    assert ref_res2.refunded_units == 30
    assert ref_res2.remaining == 80  # 100 limit - 20 used (the 30 was refunded)
