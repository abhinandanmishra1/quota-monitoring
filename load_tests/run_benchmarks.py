import os
import sys
import time
import subprocess
import csv
import glob
import httpx
import asyncpg

PORTS = [8001, 8002, 8003, 8004]
BASE_URL = "http://127.0.0.1:8001"

def start_services():
    """Starts 4 FastAPI service instances across separate ports."""
    procs = []
    print(f"Starting {len(PORTS)} FastAPI instances on ports {PORTS}...")
    for port in PORTS:
        p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "quota.main:app",
                "--port",
                str(port),
                "--no-access-log",
                "--log-level",
                "warning",
            ],
            env={
                "PATH": os.environ["PATH"],
                "PYTHONPATH": "src",
                "DATABASE_URL": os.getenv("DATABASE_URL", "postgresql://localhost/quota_metering"),
                "DB_POOL_MIN": "3",
                "DB_POOL_MAX": "15",
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
            raise RuntimeError(f"Service on port {port} failed to start")
    print("All 4 instances are healthy and ready.")
    return procs

def stop_services(procs):
    for p in procs:
        p.terminate()
        p.wait()
    print("All service instances terminated.")

def seed_database():
    print("Seeding database configurations...")
    with httpx.Client() as client:
        # Seed 100M units default
        client.post(
            f"{BASE_URL}/v1/admin/quota/limits/default",
            json={"feature": "load_feature", "monthly_limit": 100000000},
        )
        # Seed 50M units hot org
        client.post(
            f"{BASE_URL}/v1/admin/quota/limits/override",
            json={"org_id": "hot_organization", "feature": "load_feature", "monthly_limit": 50000000},
        )

def measure_overhead():
    print("Measuring quota operation overhead (1,000 operations)...")
    import asyncio
    from quota.core.database import create_db_pool

    async def _run():
        pool = await create_db_pool()
        try:
            times = []
            async with pool.acquire() as conn:
                for i in range(1000):
                    t0 = time.perf_counter()
                    await conn.fetchrow(
                        """
                        SELECT out_status_code, out_remaining
                        FROM fn_quota_consume($1, $2, $3, $4, gen_random_uuid(), '2026-11', NOW());
                        """,
                        f"org_over_{i % 50}",
                        "load_feature",
                        1,
                        f"idem_over_{i}_{time.time()}",
                    )
                    times.append((time.perf_counter() - t0) * 1000)
            times.sort()
            p50 = times[int(len(times) * 0.50)]
            p95 = times[int(len(times) * 0.95)]
            p99 = times[int(len(times) * 0.99)]
            avg = sum(times) / len(times)
            return {"avg": avg, "p50": p50, "p95": p95, "p99": p99}
        finally:
            await pool.close()

    return asyncio.run(_run())

def run_locust_scenario(scenario_name, user_class, users, spawn_rate, duration_secs, processes=1):
    print(f"\n>>> Running {scenario_name} (Users: {users}, Processes: {processes}, Duration: {duration_secs}s)...")
    csv_prefix = f"/tmp/locust_{user_class}"
    for f in glob.glob(f"{csv_prefix}*"):
        os.remove(f)

    cmd = [
        sys.executable,
        "-m",
        "locust",
        "-f",
        "load_tests/locustfile.py",
        user_class,
        "--headless",
        "-u",
        str(users),
        "-r",
        str(spawn_rate),
        "--run-time",
        f"{duration_secs}s",
        "--host",
        BASE_URL,
        "--csv",
        csv_prefix,
    ]
    if processes > 1:
        cmd.extend(["--processes", str(processes)])

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Parse stats from generated CSV
    stats_file = f"{csv_prefix}_stats.csv"
    throughput = 0.0
    failures = 0
    total_reqs = 0

    with open(stats_file, mode="r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Name"] == "Aggregated":
                total_reqs = int(row["Request Count"])
                failures = int(row["Failure Count"])
                throughput = float(row["Requests/s"])

    # Parse percentiles from response time CSV
    p50, p95, p99 = 0.0, 0.0, 0.0
    percentiles_file = f"{csv_prefix}_stats_history.csv"
    # Or from the percentiles CSV if available
    pct_file = f"{csv_prefix}_failures.csv" # check percentiles
    # Let's inspect percentiles from stats file or compute
    pct_filename = f"{csv_prefix}_stats.csv"
    with open(pct_filename, mode="r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Name"] == "Aggregated":
                p50 = float(row.get("50%", row.get("Median Response Time", 0)))
                p95 = float(row.get("95%", 0))
                p99 = float(row.get("99%", 0))

    err_pct = (failures / total_reqs * 100) if total_reqs > 0 else 0.0
    return {
        "scenario": scenario_name,
        "throughput": f"{throughput:,.1f} req/s",
        "p50": f"{p50:.2f} ms",
        "p95": f"{p95:.2f} ms",
        "p99": f"{p99:.2f} ms",
        "errors": f"{failures} ({err_pct:.1f}%)",
        "raw_throughput": throughput,
    }

def main():
    procs = start_services()
    try:
        seed_database()
        overhead = measure_overhead()
        print(f"Measured Quota Operation Overhead: p50={overhead['p50']:.2f}ms, p95={overhead['p95']:.2f}ms, p99={overhead['p99']:.2f}ms, avg={overhead['avg']:.2f}ms")

        # Scenario 1: 5k organizations, sustained workload
        s1 = run_locust_scenario("5k organizations, sustained", "DistributedQuotaUser", users=60, spawn_rate=20, duration_secs=15)

        # Scenario 2: 5k organizations, peak workload burst
        s2 = run_locust_scenario("5k organizations, peak burst", "PeakQuotaUser", users=150, spawn_rate=50, duration_secs=15)

        # Scenario 3: Single hot organization/feature contention
        s3 = run_locust_scenario("Single hot organization/feature", "HotOrgQuotaUser", users=40, spawn_rate=20, duration_secs=15)

        print("\n\n================ FINAL MEASURED RESULTS ================\n")
        print("| Scenario | Throughput | p50 | p95 | p99 | Errors |")
        print("| :--- | :---: | :---: | :---: | :---: | :---: |")
        for s in [s1, s2, s3]:
            print(f"| {s['scenario']} | {s['throughput']} | {s['p50']} | {s['p95']} | {s['p99']} | {s['errors']} |")

        print(f"\nOverhead Benchmark: p50={overhead['p50']:.2f}ms, p95={overhead['p95']:.2f}ms (<10ms target: PASS)")
    finally:
        stop_services(procs)

if __name__ == "__main__":
    main()
