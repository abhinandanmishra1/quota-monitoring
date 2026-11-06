import asyncio
import time
import uuid
import random
import statistics
from httpx import AsyncClient, ASGITransport
import asyncpg
from quota.api import app
from quota.database import create_db_pool, init_db
from quota.config import settings

async def setup_benchmarks(pool: asyncpg.Pool):
    await init_db(pool)
    app.state.db_pool = pool

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Seed default limit: 100,000,000 units
        await client.post(
            "/v1/admin/quota/limits/default",
            json={"feature": "load_feature", "monthly_limit": 100000000},
        )
        # Seed hot org limit: 50,000,000 units
        await client.post(
            "/v1/admin/quota/limits/override",
            json={"org_id": "hot_organization", "feature": "load_feature", "monthly_limit": 50000000},
        )

async def benchmark_scenario(
    name: str,
    concurrency: int,
    total_requests: int,
    target_hot_org: bool = False,
) -> dict:
    transport = ASGITransport(app=app)
    latencies_ms: list[float] = []
    errors = 0
    start_time = time.perf_counter()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        sem = asyncio.Semaphore(concurrency)

        async def worker(req_idx: int):
            nonlocal errors
            org = "hot_organization" if target_hot_org else f"org_{random.randint(1, 5000)}"
            payload = {
                "org_id": org,
                "feature": "load_feature",
                "units": 1,
                "idempotency_key": str(uuid.uuid4()),
            }

            async with sem:
                t0 = time.perf_counter()
                try:
                    resp = await client.post("/v1/quota/consume", json=payload)
                    t1 = time.perf_counter()
                    if resp.status_code in (200, 429):
                        latencies_ms.append((t1 - t0) * 1000)
                    else:
                        errors += 1
                except Exception:
                    errors += 1

        # Run workers
        tasks = [asyncio.create_task(worker(i)) for i in range(total_requests)]
        await asyncio.gather(*tasks)

    total_time = time.perf_counter() - start_time
    throughput = len(latencies_ms) / total_time if total_time > 0 else 0

    latencies_ms.sort()
    p50 = latencies_ms[int(len(latencies_ms) * 0.50)] if latencies_ms else 0.0
    p95 = latencies_ms[int(len(latencies_ms) * 0.95)] if latencies_ms else 0.0
    p99 = latencies_ms[int(len(latencies_ms) * 0.99)] if latencies_ms else 0.0

    return {
        "scenario": name,
        "throughput": f"{throughput:,.1f} req/s",
        "p50": f"{p50:.2f} ms",
        "p95": f"{p95:.2f} ms",
        "p99": f"{p99:.2f} ms",
        "errors": f"{errors} ({errors/total_requests:.1%})" if errors else "0 (0.0%)",
    }

async def main():
    pool = await create_db_pool()
    try:
        await setup_benchmarks(pool)

        print("\n================ RUNNING BENCHMARKS ================")
        
        # Scenario 1: 5k organizations, sustained ~2k ops/sec workload
        print("Running Scenario 1: 5k organizations distributed workload (5,000 requests, concurrency=30)...")
        res1 = await benchmark_scenario("5k organizations, sustained 2k ops/sec", concurrency=30, total_requests=5000)

        # Scenario 2: 5k organizations, peak workload
        print("Running Scenario 2: 5k organizations peak burst (10,000 requests, concurrency=80)...")
        res2 = await benchmark_scenario("5k organizations, peak near 10k ops/sec", concurrency=80, total_requests=10000)

        # Scenario 3: Single hot organization/feature row contention
        print("Running Scenario 3: Single hot organization/feature contention (3,000 requests, concurrency=20)...")
        res3 = await benchmark_scenario("Single hot organization/feature", concurrency=20, total_requests=3000, target_hot_org=True)

        results = [res1, res2, res3]
        print("\n\n### Measured Benchmark Results Table\n")
        print("| Scenario | Throughput | p50 | p95 | p99 | Errors |")
        print("| :--- | :---: | :---: | :---: | :---: | :---: |")
        for r in results:
            print(f"| {r['scenario']} | {r['throughput']} | {r['p50']} | {r['p95']} | {r['p99']} | {r['errors']} |")

    finally:
        await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
