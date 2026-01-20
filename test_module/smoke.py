import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get("BACKEND_URL", "https://dp-scoutiq-backend-mobile.onrender.com")

def check(name, cond, detail=""):
    if cond:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name} {detail}".strip())
        sys.exit(1)

def main():
    # /health
    r = requests.get(f"{BASE}/health", timeout=10)
    check("GET /health status", r.status_code == 200, f"(got {r.status_code})")
    data = r.json()
    check("GET /health ok=true", data.get("ok") is True, f"(got {data})")

    print("Smoke test passed ✅")

if __name__ == "__main__":
    main()
