import pytest
from httpx import AsyncClient, ASGITransport
from quota.api import app
from quota.database import init_db

@pytest.mark.asyncio
async def test_admin_config_defaults_overrides_and_current_period_update(db_pool):
    """
    DESIGN.md Section 4.4 & 6:
    - Feature defaults and org-specific overrides.
    - Admin limit update rejects new limit below already consumed usage.
    - Missing quota configuration returns 404.
    """
    await init_db(db_pool)
    app.state.db_pool = db_pool

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unconfigured feature consume -> 404
        unconf_res = await client.post(
            "/v1/quota/consume",
            json={"org_id": "org_x", "feature": "unconfigured_svc", "units": 5, "idempotency_key": "k_unconf"},
        )
        assert unconf_res.status_code == 404

        # 2. Configure feature default = 50
        await client.post(
            "/v1/admin/quota/limits/default",
            json={"feature": "unconfigured_svc", "monthly_limit": 50},
        )

        # 3. Configure org override for 'org_vip' = 100
        await client.post(
            "/v1/admin/quota/limits/override",
            json={"org_id": "org_vip", "feature": "unconfigured_svc", "monthly_limit": 100},
        )

        # Verify defaults vs overrides via usage endpoint
        res_default = await client.get("/v1/quota/usage?org_id=org_regular&feature=unconfigured_svc")
        assert res_default.json()["quota_limit"] == 50

        res_override = await client.get("/v1/quota/usage?org_id=org_vip&feature=unconfigured_svc")
        assert res_override.json()["quota_limit"] == 100

        # 4. Consume 40 units for org_regular
        await client.post(
            "/v1/quota/consume",
            json={"org_id": "org_regular", "feature": "unconfigured_svc", "units": 40, "idempotency_key": "k_reg"},
        )

        # 5. Admin tries to lower limit to 30 (below 40 used) -> 400 Bad Request
        bad_update = await client.patch(
            "/v1/admin/quota/limits/current-period",
            json={"org_id": "org_regular", "feature": "unconfigured_svc", "new_limit": 30},
        )
        assert bad_update.status_code == 400
        assert "cannot be less than already consumed usage" in bad_update.json()["detail"]

        # 6. Admin updates limit to 80 -> succeeds
        good_update = await client.patch(
            "/v1/admin/quota/limits/current-period",
            json={"org_id": "org_regular", "feature": "unconfigured_svc", "new_limit": 80},
        )
        assert good_update.status_code == 200
        assert good_update.json()["quota_limit"] == 80
        assert good_update.json()["remaining"] == 40
