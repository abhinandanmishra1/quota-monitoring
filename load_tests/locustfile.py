import random
import uuid
from locust import task, between
from locust.contrib.fasthttp import FastHttpUser

INSTANCES = [
    "http://127.0.0.1:8001",
    "http://127.0.0.1:8002",
    "http://127.0.0.1:8003",
    "http://127.0.0.1:8004",
]

class DistributedQuotaUser(FastHttpUser):
    """
    Traffic distributed across 5,000 organizations.
    Tests horizontal scalability across multiple FastAPI service instances
    and database connection pool efficiency.
    """
    wait_time = between(0.0005, 0.002)

    @task
    def consume_quota(self):
        target = random.choice(INSTANCES)
        org_id = f"org_{random.randint(1, 5000)}"
        payload = {
            "org_id": org_id,
            "feature": "load_feature",
            "units": random.randint(1, 5),
            "idempotency_key": str(uuid.uuid4()),
        }
        self.client.post(f"{target}/v1/quota/consume", json=payload)

class PeakQuotaUser(FastHttpUser):
    """
    Peak burst traffic distributed across 5,000 organizations with zero wait.
    """
    @task
    def consume_peak_quota(self):
        target = random.choice(INSTANCES)
        org_id = f"org_{random.randint(1, 5000)}"
        payload = {
            "org_id": org_id,
            "feature": "load_feature",
            "units": 1,
            "idempotency_key": str(uuid.uuid4()),
        }
        self.client.post(f"{target}/v1/quota/consume", json=payload)

class HotOrgQuotaUser(FastHttpUser):
    """
    Traffic concentrated on a single organization and feature.
    Tests row-level lock contention and serialization performance in PostgreSQL.
    """
    wait_time = between(0.001, 0.003)

    @task
    def consume_hot_quota(self):
        target = random.choice(INSTANCES)
        payload = {
            "org_id": "hot_organization",
            "feature": "load_feature",
            "units": 1,
            "idempotency_key": str(uuid.uuid4()),
        }
        self.client.post(f"{target}/v1/quota/consume", json=payload)
