# Tests the mandatory multi-instance concurrency scenario (DESIGN.md 7, NFR 1, 4).
# Case: test_concurrency_across_4_instances
#   - starts 4 separate Uvicorn processes (ports 8001-8004) on one shared PostgreSQL
#   - 100 parallel requests of 10 units, round-robin across instances, quota 500
#   - exactly 50 succeed, 50 get 429, final usage is exactly 500
import asyncio
import subprocess
import sys
import time
import httpx
import pytest
from quota.core.database import create_db_pool, init_db

PORTS = [8001, 8002, 8003, 8004]

@pytest.fixture(scope="module")
def multi_instances():
    """Starts 4 independent FastAPI service instances on ports 8001-8004."""
    procs = []
    for port in PORTS:
        p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "quota.main:app",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            env={
                "PATH": subprocess.os.environ["PATH"],
                "PYTHONPATH": "src",
                "DATABASE_URL": subprocess.os.environ.get(
                    "DATABASE_URL", "postgresql://localhost/quota_metering"
                ),
                "DB_POOL_MIN": "5",
                "DB_POOL_MAX": "20",
            },
        )
        procs.append(p)

    # Wait for all 4 instances to be healthy
    start = time.time()
    for port in PORTS:
        healthy = False
        while time.time() - start < 10:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if r.status_code == 200:
                    healthy = True
                    break
            except Exception:
                time.sleep(0.1)
        if not healthy:
            for p in procs:
                p.terminate()
            raise RuntimeError(f"Instance on port {port} failed to start")

    yield [f"http://127.0.0.1:{port}" for port in PORTS]

    for p in procs:
        p.terminate()
        p.wait()

@pytest.mark.asyncio
async def test_concurrency_across_4_instances(multi_instances):
    """
    Mandatory Concurrency Test across multiple service instances:
    - 100 concurrent requests of 10 units against a quota of 500.
    - Requests distributed round-robin across 4 service instances (8001, 8002, 8003, 8004).
    - Expected result:
      * Exactly 50 requests succeed (HTTP 200).
      * Exactly 50 requests are rejected (HTTP 429).
      * Final usage is exactly 500.
      * Zero over-consumption.
    """
    pool = await create_db_pool()
    await init_db(pool)

    # Clean previous state and set default limit of 500
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE quota_operations, quota_usage, quota_config_override, quota_config_default CASCADE;")

    async with httpx.AsyncClient() as client:
        # Set limit on instance 0
        set_res = await client.post(
            f"{multi_instances[0]}/v1/admin/quota/limits/default",
            json={"feature": "multi_inst_feat", "monthly_limit": 500},
        )
        assert set_res.status_code == 200

        # Prepare 100 requests distributed across the 4 instances (25 per instance)
        tasks = []
        for i in range(100):
            target_url = multi_instances[i % len(multi_instances)]
            payload = {
                "org_id": "multi_inst_org",
                "feature": "multi_inst_feat",
                "units": 10,
                "idempotency_key": f"multi_key_{i}",
            }
            tasks.append(client.post(f"{target_url}/v1/quota/consume", json=payload))

        # Launch all 100 concurrently across all 4 instances
        responses = await asyncio.gather(*tasks)

        status_codes = [r.status_code for r in responses]
        success_count = sum(1 for c in status_codes if c == 200)
        rejected_count = sum(1 for c in status_codes if c == 429)

        print(f"\n[Multi-Instance Concurrency Results]: Success={success_count}, Rejected={rejected_count}")
        assert success_count == 50, f"Expected 50 successes across 4 instances, got {success_count}"
        assert rejected_count == 50, f"Expected 50 rejections across 4 instances, got {rejected_count}"

        # Verify PostgreSQL database state directly
        async with pool.acquire() as conn:
            usage_row = await conn.fetchrow(
                "SELECT used, quota_limit FROM quota_usage WHERE org_id = 'multi_inst_org' AND feature = 'multi_inst_feat';"
            )
            assert usage_row is not None
            assert usage_row["used"] == 500
            assert usage_row["quota_limit"] == 500

    await pool.close()
