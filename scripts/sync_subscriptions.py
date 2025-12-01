# scripts/sync_subscriptions.py
from api_module.database import SessionLocal
from api_module.payment_utilities import run_subscription_sync

def main():
    db = SessionLocal()
    try:
        run_subscription_sync(db)
    finally:
        db.close()

if __name__ == "__main__":
    main()
    print("here")
