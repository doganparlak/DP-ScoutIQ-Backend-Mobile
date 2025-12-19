# scripts/sync_subscriptions.py
from api_module.database import SessionLocal
from api_module.payment_utilities import run_entitlements_sync, run_subscription_sync

def main():
    db = SessionLocal()
    try:
        print("DB FETCH SUCCESSFUL, RUNNING ENTITLEMENTS SYNC...")
        run_entitlements_sync(db, limit=2000)

        print("RUNNING USERS SUBSCRIPTION SYNC...")
        run_subscription_sync(db)

        print("SYNC COMPLETE.")
    finally:
        print("CLOSING SESSION...")
        db.close()

if __name__ == "__main__":
    print("STARTING SUBSCRIPTION SYNC")
    main()
    print("ENDING SUBSCRIPTION SYNC")
