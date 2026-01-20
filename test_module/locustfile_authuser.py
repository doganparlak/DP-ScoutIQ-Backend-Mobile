import os
from locust import HttpUser, task, between
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.environ.get("TEST_EMAIL", "")
PASSWORD = os.environ.get("TEST_PASSWORD", "")
print("EMAIL:", EMAIL)
print("PASSWORD:",PASSWORD)
class AuthUser(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self):
        if not EMAIL or not PASSWORD:
            raise RuntimeError("Set TEST_EMAIL and TEST_PASS env vars")

        r = self.client.post(
            "/auth/login",
            json={
                "email": EMAIL,
                "password": PASSWORD,
                "uiLanguage": "en"
            },
            timeout=15
        )

        if r.status_code != 200:
            raise RuntimeError(f"Login failed: {r.status_code} {r.text}")

        token = r.json().get("token")
        if not token:
            raise RuntimeError("No token returned")

        self.headers = {
            "Authorization": f"Bearer {token}"
        }

    @task
    def me(self):
        with self.client.get("/me", headers=self.headers, timeout=10, catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"status={r.status_code}")
            else:
                r.success()
