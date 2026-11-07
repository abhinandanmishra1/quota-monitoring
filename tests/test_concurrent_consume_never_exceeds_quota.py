# Tests concurrent consumption never over-consumes (NFR 1, 2; DESIGN.md 4.1, 4.2).
# Cases:
#   - test_key_concurrency_100_requests_against_500_quota: 100 parallel requests of 10 units
#     against a 500 quota; exactly 50 succeed, 50 get 429, final usage is exactly 500
#   - test_mixed_request_sizes_and_concurrent_first_time_creation: mixed 15/5-unit requests
#     racing on first-time usage-row creation; no 500s, usage never exceeds the limit and
#     equals the sum of successful units
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from quota.main import app
from quota.core.database import init_db

@pytest.mark.asyncio
async def test_key_concurrency_100_requests_against_500_quota(db_pool):
    """
    DESIGN.md Section 7 key concurrency test:
    - 100 concurrent requests of 10 units against a quota of 500.
    - Expected result:
      * Exactly 50 requests succeed.
      * Exactly 50 requests are rejected.
      * Final usage is exactly 500.
      * Usage never exceeds the configured quota.
    """
    await init_db(db_pool)
    app.state.db_pool = db_pool

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Set quota of 500
        await client.post(
            "/v1/admin/quota/limits/default",
            json={"feature": "ai_tokens", "monthly_limit": 500},
        )

        async def send_consume(idx: int):
            return await client.post(
                "/v1/quota/consume",
                json={
                    "org_id": "concurrency_org",
                    "feature": "ai_tokens",
                    "units": 10,
                    "idempotency_key": f"key_{idx}",
                },
            )

        # Launch 100 concurrent requests simultaneously
        responses = await asyncio.gather(*[send_consume(i) for i in range(100)])

        success_count = sum(1 for r in responses if r.status_code == 200)
        rejected_count = sum(1 for r in responses if r.status_code == 429)

        assert success_count == 50, f"Expected 50 successes, got {success_count}"
        assert rejected_count == 50, f"Expected 50 rejections, got {rejected_count}"

        # Verify final usage in DB is exactly 500
        usage_res = await client.get("/v1/quota/usage?org_id=concurrency_org&feature=ai_tokens")
        assert usage_res.status_code == 200
        usage_data = usage_res.json()
        assert usage_data["used"] == 500
        assert usage_data["remaining"] == 0
        assert usage_data["quota_limit"] == 500

@pytest.mark.asyncio
async def test_mixed_request_sizes_and_concurrent_first_time_creation(db_pool):
    """
    Concurrent requests with mixed units hitting a brand new org/feature with no existing row.
    """
    await init_db(db_pool)
    app.state.db_pool = db_pool

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/admin/quota/limits/default",
            json={"feature": "mixed_feature", "monthly_limit": 100},
        )

        # 10 requests of 15 units, 10 requests of 5 units = 20 concurrent requests
        reqs = []
        for i in range(10):
            reqs.append((f"large_{i}", 15))
        for i in range(10):
            reqs.append((f"small_{i}", 5))

        async def fire(key: str, units: int):
            return await client.post(
                "/v1/quota/consume",
                json={
                    "org_id": "mixed_org",
                    "feature": "mixed_feature",
                    "units": units,
                    "idempotency_key": key,
                },
            )

        responses = await asyncio.gather(*[fire(k, u) for k, u in reqs])

        # Verify no response resulted in 500 errors
        status_codes = [r.status_code for r in responses]
        assert all(code in (200, 429) for code in status_codes)

        # Final usage must be <= 100 and match sum of successful units
        usage_res = await client.get("/v1/quota/usage?org_id=mixed_org&feature=mixed_feature")
        used = usage_res.json()["used"]
        assert 0 <= used <= 100

        # Sum of units from successful requests
        success_units = sum(
            r.json()["units"] for r in responses if r.status_code == 200
        )
        assert used == success_units
