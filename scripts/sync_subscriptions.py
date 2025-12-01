# scripts/sync_subscriptions.py
from api_module.database import SessionLocal
from api_module.payment_utilities import run_subscription_sync

def main():
    db = SessionLocal()
    try:
        print("DB FETCH SUCCESSFUL, RUNNING SYNC...")
        run_subscription_sync(db)
    finally:
        print("DB FETCH UNSUCCESSFUL, CLOSING SESSION...")
        db.close()

if __name__ == "__main__":
    print("STARTING SUBSCRIPTION SYNC")
    main()
    print("ENDING SUBSCRIPTION SYNC")
