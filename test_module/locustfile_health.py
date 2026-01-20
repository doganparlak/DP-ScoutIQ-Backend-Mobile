import os
from locust import HttpUser, task, between

class HealthUser(HttpUser):
    # Small wait to avoid totally unrealistic “machine-gun” traffic
    wait_time = between(0.1, 0.5)

    @task
    def health(self):
        with self.client.get("/health", name="/health", timeout=10, catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"status={r.status_code}")
                return
            try:
                data = r.json()
            except Exception:
                r.failure("invalid json")
                return
            if data.get("ok") is not True:
                r.failure(f"unexpected body: {data}")
            else:
                r.success()
