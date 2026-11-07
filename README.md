# Per-Customer Quota Metering Service

Production-ready, highly consistent, and high-performance quota metering service implemented in FastAPI and PostgreSQL, adhering strictly to the architecture and design specified in [DESIGN.md](DESIGN.md).

---

## 1. Overview & Key Design Decisions

- **Single Source of Truth in PostgreSQL:** Quota limits, monthly usage counters, and idempotency operation history live in PostgreSQL. Avoids dual-write inconsistency and cross-database coordination (no Redis needed).
- **Atomic Conditional Updates:** Quota deduction is performed using a single conditional SQL update:
  ```sql
  UPDATE quota_usage
  SET used = used + :units, updated_at = NOW()
  WHERE org_id = :org
    AND feature = :feature
    AND period = :period
    AND used <= quota_limit - :units
  RETURNING used, quota_limit;
  ```
- **All-or-Nothing Batching:** A request for $N$ units either succeeds completely or is rejected with `429 Too Many Requests` (`Quota limit exceeded`) without deducting anything.
- **Idempotency & Replay Protection:** Client sends an `idempotency_key` scoped to `(org_id, feature)`. Same key and payload replays the stored response without charging twice; same key with different payload returns `409 Conflict`.
- **Downstream Failure Refunds:** Consumed units are refunded upon confirmed downstream failure, locking the operation row first and usage row second to prevent deadlocks and race conditions.
- **Period-Based Billing:** Usage is tracked by calendar month in UTC (`YYYY-MM`). Usage rows are initialized lazily on the first consume operation of the month.
- **Cross-Month Refunds:** Operation records preserve the original `period`. A refund executed after a month boundary refunds the original month's usage row, leaving the new month intact.
- **Fail Closed:** When PostgreSQL is unreachable, the service fails closed with `503 Service Unavailable`, preventing over-consumption from stale balances.

---

## 2. Quickstart with Docker Compose

To build and start both PostgreSQL and the Quota Metering service:

```bash
docker compose up --build -d
```

The service is exposed at `http://localhost:8000`.

If a local PostgreSQL already uses port 5432, change the `postgres` port mapping in `docker-compose.yml` (for example `"15432:5432"`); the service itself reaches the database over the Compose network and is unaffected.

To view logs:
```bash
docker compose logs -f quota-service
```

To stop containers:
```bash
docker compose down
```

---

## 3. Local Development Setup

### Prerequisites
- Python 3.12+ (or 3.14)
- PostgreSQL 14+ running locally (e.g. database `quota_metering`)

### 1. Create Virtual Environment & Install Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment (Optional)
```bash
export DATABASE_URL="postgresql://localhost/quota_metering"
export DB_POOL_MIN=5
export DB_POOL_MAX=20
```

### 3. Run FastAPI Service
```bash
PYTHONPATH=src uvicorn quota.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 4. Running Tests

Run the full automated test suite (including concurrency, idempotency, race condition, fail-closed, and latency overhead tests):

```bash
pytest tests/ -v
```

### Key Test Suites:
- `tests/test_concurrent_consume_never_exceeds_quota.py`: 100 concurrent requests of 10 units against a quota of 500 (verifies exactly 50 pass, 50 reject with 429, and final usage is exactly 500).
- `tests/test_idempotent_retry_and_key_conflict.py`: Verifies replay protection, same key payload mismatch (409 Conflict), and rejected request replay.
- `tests/test_refund_credits_original_period.py`: Verifies cross-month billing period refund isolation.
- `tests/test_finalize_refund_race.py`: Verifies race condition between finalize and refund (only one state transition succeeds).
- `tests/test_admin_limits_defaults_overrides_and_period_update.py`: Verifies feature defaults, org overrides, and rejecting limit reductions below current usage.
- `tests/test_database_outage_returns_503.py`: Verifies 503 fail-closed behavior when database is unreachable.
- `tests/test_operation_overhead_under_10ms.py`: Verifies that quota enforcement latency overhead is `< 10ms` (measured ~2ms p50).

---

## 5. Load Testing & Benchmarks

Run the benchmark suite:

```bash
./load_tests/run_benchmarks.sh
```

Or run Locust interactively:
```bash
PYTHONPATH=src locust -f load_tests/locustfile.py --host http://localhost:8000
```

### Measured Benchmark Results (Apple M1 Max, local PostgreSQL, 4 service instances):

| Scenario | Throughput | p50 | p95 | p99 | Errors |
| :--- | :---: | :---: | :---: | :---: | :---: |
| 5k organizations, sustained | 6,505 req/s | 6 ms | 11 ms | 19 ms | 0 |
| 5k organizations, peak burst | 7,284 req/s | 16 ms | 31 ms | 45 ms | 0 |
| Single hot organization/feature | 2,758 req/s | 7 ms | 36 ms | 62 ms | 0 |

The 10,000 req/s peak target is not met on this single-machine setup. See DESIGN.md section 7 for analysis.

---

## 6. API Reference & cURL Examples

### Health Check
```bash
curl http://localhost:8000/health
```
**Response (200 OK):**
```json
{"status": "ok", "database": "connected"}
```

---

### Admin: Set Feature Default Limit
```bash
curl -X POST http://localhost:8000/v1/admin/quota/limits/default \
  -H "Content-Type: application/json" \
  -d '{
    "feature": "ai_inference",
    "monthly_limit": 1000
  }'
```

---

### Admin: Set Organization-Specific Override
```bash
curl -X POST http://localhost:8000/v1/admin/quota/limits/override \
  -H "Content-Type: application/json" \
  -d '{
    "org_id": "org_enterprise_1",
    "feature": "ai_inference",
    "monthly_limit": 5000
  }'
```

---

### Consume Quota
```bash
curl -X POST http://localhost:8000/v1/quota/consume \
  -H "Content-Type: application/json" \
  -d '{
    "org_id": "org_enterprise_1",
    "feature": "ai_inference",
    "units": 25,
    "idempotency_key": "order_uuid_101"
  }'
```
**Response (200 OK):**
```json
{
  "operation_id": "a92e59e2-55db-446a-843e-ec32bebfcf48",
  "status": "CHARGED",
  "units": 25,
  "remaining": 4975,
  "reset_at": "2026-12-01T00:00:00Z",
  "period": "2026-11"
}
```

If remaining quota is insufficient:
**Response (429 Too Many Requests):**
```json
{
  "detail": "Quota limit exceeded",
  "requested": 6000,
  "remaining": 4975,
  "reset_at": "2026-12-01T00:00:00Z"
}
```

---

### Finalize Operation (Downstream Success)
```bash
curl -X POST http://localhost:8000/v1/quota/finalize \
  -H "Content-Type: application/json" \
  -d '{
    "operation_id": "a92e59e2-55db-446a-843e-ec32bebfcf48"
  }'
```
**Response (200 OK):**
```json
{
  "operation_id": "a92e59e2-55db-446a-843e-ec32bebfcf48",
  "status": "FINALIZED"
}
```

---

### Refund Operation (Downstream Confirmed Failure)
```bash
curl -X POST http://localhost:8000/v1/quota/refund \
  -H "Content-Type: application/json" \
  -d '{
    "operation_id": "a92e59e2-55db-446a-843e-ec32bebfcf48",
    "reason": "Payment processing failed"
  }'
```
**Response (200 OK):**
```json
{
  "operation_id": "a92e59e2-55db-446a-843e-ec32bebfcf48",
  "status": "REFUNDED",
  "refunded_units": 25,
  "remaining": 5000
}
```

---

### Check Quota Usage
```bash
curl "http://localhost:8000/v1/quota/usage?org_id=org_enterprise_1&feature=ai_inference"
```
**Response (200 OK):**
```json
{
  "org_id": "org_enterprise_1",
  "feature": "ai_inference",
  "period": "2026-11",
  "quota_limit": 5000,
  "used": 0,
  "remaining": 5000,
  "reset_at": "2026-12-01T00:00:00Z"
}
```

---

### Stale Operation Reconciliation
To identify stale `CHARGED` operations that timed out downstream and require investigation:

```bash
PYTHONPATH=src .venv/bin/python3 -m quota.reconciliation.worker report 300
```

### Retention Cleanup
Deletes `FINALIZED`, `REFUNDED` and `REJECTED` operations older than the 24-hour idempotency window (never `CHARGED`):

```bash
PYTHONPATH=src .venv/bin/python3 -m quota.reconciliation.worker purge 24
```
