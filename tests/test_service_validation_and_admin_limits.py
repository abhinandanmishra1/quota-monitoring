# Tests QuotaService validation and admin logic (DESIGN.md 4.4, 6).
# Cases:
#   - test_service_validations_and_unconfigured_feature: units must be > 0, key must be
#     non-empty, unconfigured feature returns 404
#   - test_service_admin_limit_updates_and_overrides: default vs override limits and
#     current-period limit updates (rejected below usage)
import pytest
from quota.services.quota_service import QuotaService
from quota.core.exceptions import DatabaseUnavailableError

@pytest.mark.asyncio
async def test_service_validations_and_unconfigured_feature(db_pool):
    svc = QuotaService(db_pool)

    # 1. Units must be > 0
    with pytest.raises(ValueError, match="Units must be greater than zero"):
        await svc.consume("org_1", "feature_x", 0, "idem_zero")

    with pytest.raises(ValueError, match="Units must be greater than zero"):
        await svc.consume("org_1", "feature_x", -5, "idem_neg")

    # 2. Empty idempotency key
    with pytest.raises(ValueError, match="Idempotency key must not be empty"):
        await svc.consume("org_1", "feature_x", 10, "")

    # 3. Unconfigured feature returns 404 status in result
    res = await svc.consume("org_1", "unconfigured_feat", 10, "idem_1")
    assert res.status_code == 404
    assert "No quota configured" in (res.detail or "")

@pytest.mark.asyncio
async def test_service_admin_limit_updates_and_overrides(db_pool):
    svc = QuotaService(db_pool)

    # Set default 100
    await svc.set_default_limit("reports", 100)
    # Set override for org_vip: 200
    await svc.set_override_limit("org_vip", "reports", 200)

    # Check normal org usage: limit is 100
    usage_normal = await svc.get_usage("org_normal", "reports")
    assert usage_normal.quota_limit == 100
    assert usage_normal.used == 0

    # Check VIP org usage: limit is 200
    usage_vip = await svc.get_usage("org_vip", "reports")
    assert usage_vip.quota_limit == 200
    assert usage_vip.used == 0

    # Consume 60 for org_vip
    consume_res = await svc.consume("org_vip", "reports", 60, "vip_1")
    assert consume_res.success is True
    assert consume_res.remaining == 140

    # Try to reduce current period limit below 60 -> should raise ValueError
    with pytest.raises(ValueError, match="cannot be less than already consumed usage"):
        await svc.update_current_period_limit("org_vip", "reports", 50)

    # Update limit to 150 -> succeeds
    updated = await svc.update_current_period_limit("org_vip", "reports", 150)
    assert updated.quota_limit == 150
    assert updated.remaining == 90
