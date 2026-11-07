# Tests the latency target (NFR 3: under 10ms per operation).
# Case: test_operation_overhead_less_than_10ms
#   - sequential consume requests through the app (in-process ASGI, real PostgreSQL)
#   - asserts p50 latency is under 10ms (p95 is only printed); excludes network and Uvicorn
import time
import pytest
from httpx import AsyncClient, ASGITransport
from quota.main import app
from quota.core.database import init_db

@pytest.mark.asyncio
async def test_operation_overhead_less_than_10ms(db_pool):
    """
    DESIGN.md Section 2 Non-Functional Requirement 3:
    'Quota enforcement should add less than 10ms overhead per operation.'
    Measures 100 consecutive consume operations against local PostgreSQL.
    """
    await init_db(db_pool)
    app.state.db_pool = db_pool

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/admin/quota/limits/default",
            json={"feature": "overhead_test", "monthly_limit": 100000},
        )

        latencies_ms = []
        for i in range(100):
            start = time.perf_counter()
            resp = await client.post(
                "/v1/quota/consume",
                json={
                    "org_id": f"org_perf_{i % 10}",
                    "feature": "overhead_test",
                    "units": 1,
                    "idempotency_key": f"perf_key_{i}",
                },
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            assert resp.status_code == 200
            latencies_ms.append(elapsed_ms)

        latencies_ms.sort()
        p50 = latencies_ms[int(len(latencies_ms) * 0.50)]
        p95 = latencies_ms[int(len(latencies_ms) * 0.95)]

        print(f"\n[Overhead Benchmark] p50: {p50:.2f}ms | p95: {p95:.2f}ms")
        # Assert p50 latency overhead is well within 10ms target
        assert p50 < 10.0, f"Expected p50 < 10ms, got {p50:.2f}ms"
