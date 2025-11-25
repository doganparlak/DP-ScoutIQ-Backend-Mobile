# scripts/run_subscription_sync.py
import os
import requests

# Your deployed backend base URL
BACKEND_URL = os.environ.get("BACKEND_URL")

# Must match SUBSCRIPTION_SYNC_TOKEN in your backend .env
ADMIN_TOKEN = os.environ.get("SUBSCRIPTION_SYNC_TOKEN")

if not ADMIN_TOKEN:
    raise RuntimeError("SUBSCRIPTION_SYNC_TOKEN is not set in environment")

def main():
    url = f"{BACKEND_URL.rstrip('/')}/internal/subscriptions/sync"
    headers = {
        "X-Admin-Token": ADMIN_TOKEN,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, headers=headers, timeout=30)
        print("Status:", resp.status_code)
        print("Body:", resp.text)
        resp.raise_for_status()
    except Exception as e:
        # In production you might want to log to Sentry etc.
        print("Error calling subscription sync:", e)

if __name__ == "__main__":
    main()
