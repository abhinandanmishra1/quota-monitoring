import random
import uuid
from locust import HttpUser, task, between, events

class DistributedQuotaUser(HttpUser):
    """
    Traffic distributed across 5,000 organizations.
    Tests horizontal scalability and database connection pool efficiency.
    """
    wait_time = between(0.001, 0.005)

    @task
    def consume_quota(self):
        org_id = f"org_{random.randint(1, 5000)}"
        payload = {
            "org_id": org_id,
            "feature": "load_feature",
            "units": random.randint(1, 5),
            "idempotency_key": str(uuid.uuid4()),
        }
        self.client.post("/v1/quota/consume", json=payload)

class HotOrgQuotaUser(HttpUser):
    """
    Traffic concentrated on a single organization and feature.
    Tests row-level lock contention and serialization performance in PostgreSQL.
    """
    wait_time = between(0.001, 0.002)

    @task
    def consume_hot_quota(self):
        payload = {
            "org_id": "hot_organization",
            "feature": "load_feature",
            "units": 1,
            "idempotency_key": str(uuid.uuid4()),
        }
        self.client.post("/v1/quota/consume", json=payload)
