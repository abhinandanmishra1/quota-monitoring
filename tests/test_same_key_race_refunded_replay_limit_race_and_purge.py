# Tests fixes made during the design-vs-implementation review.
# Cases:
#   - test_concurrent_same_key_replays_and_charges_once: 20 parallel retries of one new key
#     all return 200, same operation, charged once (no UniqueViolation 500s)
#   - test_replay_of_refunded_key_is_409_not_429: replaying a refunded key returns 409
#   - test_period_limit_update_never_drops_below_used_under_load: limit decrease racing
#     with consumes keeps 0 <= used <= limit
#   - test_period_limit_update_below_used_rejected: limit below usage is refused
#   - test_purge_keeps_charged_and_recent_deletes_old_terminal: retention cleanup deletes
#     only old FINALIZED/REFUNDED/REJECTED operations, never CHARGED
import asyncio
import pytest
from quota.services.quota_service import QuotaService
from quota.reconciliation.worker import purge_expired_operations


@pytest.mark.asyncio
async def test_concurrent_same_key_replays_and_charges_once(db_pool):
    """20 simultaneous retries of one key: all succeed, one charge, no 500s."""
    svc = QuotaService(db_pool)
    await svc.set_default_limit("race_feat", 1000)
    results = await asyncio.gather(
        *[svc.consume("race_org", "race_feat", 10, "same-key") for _ in range(20)]
    )
    assert {r.status_code for r in results} == {200}
    assert len({r.operation_id for r in results}) == 1
    assert (await svc.get_usage("race_org", "race_feat")).used == 10


@pytest.mark.asyncio
async def test_replay_of_refunded_key_is_409_not_429(db_pool):
    svc = QuotaService(db_pool)
    await svc.set_default_limit("f", 100)
    first = await svc.consume("o", "f", 10, "k")
    import uuid
    await svc.refund(uuid.UUID(first.operation_id))
    again = await svc.consume("o", "f", 10, "k")
    assert again.status_code == 409
    assert "refunded" in again.detail
    assert (await svc.get_usage("o", "f")).used == 0


@pytest.mark.asyncio
async def test_period_limit_update_never_drops_below_used_under_load(db_pool):
    """Consumes race with a limit decrease: the invariant used <= limit always holds."""
    svc = QuotaService(db_pool)
    await svc.set_default_limit("f", 1000)
    await svc.consume("o", "f", 1, "seed")

    async def shrink():
        try:
            await svc.update_current_period_limit("o", "f", 50)
        except ValueError:
            pass

    await asyncio.gather(
        shrink(), *[svc.consume("o", "f", 5, f"k{i}") for i in range(40)]
    )
    usage = await svc.get_usage("o", "f")
    assert 0 <= usage.used <= usage.quota_limit


@pytest.mark.asyncio
async def test_period_limit_update_below_used_rejected(db_pool):
    svc = QuotaService(db_pool)
    await svc.set_default_limit("f", 100)
    await svc.consume("o", "f", 30, "k")
    with pytest.raises(ValueError, match="less than already consumed"):
        await svc.update_current_period_limit("o", "f", 10)
    assert (await svc.update_current_period_limit("o", "f", 30)).remaining == 0


@pytest.mark.asyncio
async def test_purge_keeps_charged_and_recent_deletes_old_terminal(db_pool):
    svc = QuotaService(db_pool)
    await svc.set_default_limit("f", 100)
    for key in ("charged", "finalized", "recent"):
        await svc.consume("o", "f", 1, key)
    ops = {o["idempotency_key"]: o["operation_id"] for o in await svc.list_operations(None, None, 50)}
    import uuid
    await svc.finalize(uuid.UUID(ops["finalized"]))
    await svc.finalize(uuid.UUID(ops["recent"]))
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE quota_operations SET created_at = NOW() - INTERVAL '48 hours', "
            "updated_at = NOW() - INTERVAL '48 hours' WHERE idempotency_key IN ('charged', 'finalized')"
        )
    assert await purge_expired_operations(db_pool, retention_hours=24) == 1
    remaining = {o["idempotency_key"] for o in await svc.list_operations(None, None, 50)}
    assert remaining == {"charged", "recent"}
