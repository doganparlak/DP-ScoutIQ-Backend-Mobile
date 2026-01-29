from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession
import os
from typing import Optional, Any, Dict
import datetime as dt
import json
import jwt
from api_module.utilities import now_iso
from sqlalchemy.orm import Session
from sqlalchemy import text
from appstoreserverlibrary.api_client import AppStoreServerAPIClient, APIException
from appstoreserverlibrary.models.Environment import Environment
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.auth.transport.requests import Request


def _normalize_apple_private_key(raw: str) -> bytes:
    """
    Normalize APPLE_IAP_PRIVATE_KEY from env so it works in:
    - local .env with escaped '\n'
    - Render dashboard with real multi-line PEM
    """
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]

    if "\\n" in raw and "BEGIN PRIVATE KEY" in raw:
        raw = raw.replace("\\r\\n", "\\n")
        raw = raw.replace("\\n", "\n")

    return raw.encode("utf-8")


#IOS_PRO_PRODUCT_ID = os.getenv("IOS_PRO_PRODUCT_ID", "scoutwise_pro_monthly_ios")
#ANDROID_PRO_PRODUCT_ID = os.getenv("ANDROID_PRO_PRODUCT_ID", "scoutwise_pro_monthly_android")
IOS_PRO_MONTHLY_PRODUCT_ID = os.getenv("IOS_PRO_MONTHLY_PRODUCT_ID", "scoutwise_pro_monthly_ios")
IOS_PRO_YEARLY_PRODUCT_ID  = os.getenv("IOS_PRO_YEARLY_PRODUCT_ID", "scoutwise_pro_yearly_ios")
ANDROID_PRO_MONTHLY_PRODUCT_ID = os.getenv("ANDROID_PRO_MONTHLY_PRODUCT_ID", "scoutwise_pro_monthly_android")
ANDROID_PRO_YEARLY_PRODUCT_ID  = os.getenv("ANDROID_PRO_YEARLY_PRODUCT_ID", "scoutwise_pro_yearly_android")

APPLE_IAP_KEY_ID = os.environ["APPLE_IAP_KEY_ID"]
APPLE_IAP_ISSUER_ID = os.environ["APPLE_IAP_ISSUER_ID"]
APPLE_IAP_PRIVATE_KEY = os.environ["APPLE_IAP_PRIVATE_KEY"]
APPLE_BUNDLE_ID = os.environ["APPLE_BUNDLE_ID"]
APPLE_USE_SANDBOX = os.environ.get("APPLE_IAP_USE_SANDBOX", "false").lower() == "true"

environment = Environment.SANDBOX if APPLE_USE_SANDBOX else Environment.PRODUCTION
signing_key_bytes = _normalize_apple_private_key(APPLE_IAP_PRIVATE_KEY)

app_store_client = AppStoreServerAPIClient(
    signing_key_bytes,
    APPLE_IAP_KEY_ID,
    APPLE_IAP_ISSUER_ID,
    APPLE_BUNDLE_ID,
    environment,
)


GOOGLE_PLAY_SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON",
    "/etc/secrets/play_service_account.json",
)

GOOGLE_PLAY_PACKAGE_NAME = os.environ.get("GOOGLE_PLAY_PACKAGE_NAME", "")
GOOGLE_PLAY_SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]
        
def _decode_jws_without_verification(jws: str) -> Dict[str, Any]:
    """
    Decode an Apple JWS (signedTransactionInfo / signedRenewalInfo)
    WITHOUT verifying the signature (we just need the payload fields).
    """
    try:
        # options disables signature and exp/aud checks
        return jwt.decode(
            jws,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
            },
            algorithms=["ES256"],  # Apple's JWS uses ES256
        )
    except Exception as e:
        print("[subscriptions] jws decode error:", e)
        return {}

def verify_ios_subscription(
    product_id: str,
    original_transaction_id: Optional[str],
) -> tuple[bool, dt.datetime, bool]:
    """
    Validate an iOS subscription using Apple's /verifyReceipt endpoint.

    Returns: (is_active, expires_at, auto_renew)
    """
    now = dt.datetime.now(dt.timezone.utc)
    if not original_transaction_id:
        return False, now, False

    try:
        # Ask Apple for all subscription statuses for this original transaction
        status_response = app_store_client.get_all_subscription_statuses(
            original_transaction_id
        )
       
    except APIException as e:
        return False, now, False
    
    latest_expires_at: Optional[dt.datetime] = None
    is_active = False
    will_auto_renew = False
    groups = getattr(status_response, "data", []) or []
    for group in groups:
        # Each group has lastTransactions: list[LastTransactionsItem]
        for last_tx in getattr(group, "lastTransactions", []) or []:
            signed_tx = getattr(last_tx, "signedTransactionInfo", None)
            if not signed_tx:
                continue

            decoded_tx = _decode_jws_without_verification(signed_tx)
            if not decoded_tx:
                continue
            
            tx_product_id = decoded_tx.get("productId")
            if tx_product_id != product_id:
                continue

            expires_ms = decoded_tx.get("expiresDate")
            if not expires_ms:
                continue
            
            try:
                expires_at = dt.datetime.fromtimestamp(
                    int(expires_ms) / 1000.0, tz=dt.timezone.utc
                )
            except Exception as e:
                continue
            
            if latest_expires_at is None or expires_at > latest_expires_at:
                latest_expires_at = expires_at

                # active if not expired and ownership is PURCHASED
                ownership = decoded_tx.get("inAppOwnershipType")
                is_active = expires_at > now and ownership in (
                    "PURCHASED",
                    "FAMILY_SHARED",
                )

                will_auto_renew = is_active

            # Try to refine auto_renew by looking at signedRenewalInfo
            signed_renewal = getattr(last_tx, "signedRenewalInfo", None)
            if signed_renewal:
                decoded_renewal = _decode_jws_without_verification(signed_renewal)
                if decoded_renewal:
                    auto_status = decoded_renewal.get("autoRenewStatus")
                    # According to Apple docs: 1 = ON, 0 = OFF
                    if auto_status is not None:
                        will_auto_renew = auto_status == 1

    if latest_expires_at is None:
        return False, now, False
    
    return is_active, latest_expires_at, will_auto_renew

def verify_android_subscription(
    product_id: str,
    purchase_token: str,
    receipt: str | None = None,  
) -> tuple[bool, dt.datetime, bool]:
    """
    Verify Android subscription via Google Play Developer API.

    Returns: (is_active, expires_at, auto_renew)
    """
    now = dt.datetime.now(dt.timezone.utc)

    if not purchase_token:
        return False, now, False

    try:
        print("CREDENTIALS SETTING")
        print("SERVICE ACCOUNT PATH:", GOOGLE_PLAY_SERVICE_ACCOUNT_JSON)
        print("FILE EXISTS:", os.path.exists(GOOGLE_PLAY_SERVICE_ACCOUNT_JSON))
        credentials = service_account.Credentials.from_service_account_file(
            GOOGLE_PLAY_SERVICE_ACCOUNT_JSON,
            scopes=GOOGLE_PLAY_SCOPES,
        )
        print("SA email:", credentials.service_account_email)
        credentials.refresh(Request())
        print("Access token starts with:", credentials.token[:20])
        print("SERVICE BUILDING")
        service = build("androidpublisher", "v3", credentials=credentials, cache_discovery=False)

        package_name = GOOGLE_PLAY_PACKAGE_NAME
        print(f"VERIFYING ANDROID SUBSCRIPTION: package={package_name}, product={product_id}, token={purchase_token}")
        result = (
            service.purchases()
            .subscriptions()
            .get(
                packageName=package_name,
                subscriptionId=product_id,
                token=purchase_token,
            )
            .execute()
        )
        print("[GOOGLE RESULT]", result)
        # Google returns milliseconds
        expiry_ms = int(result.get("expiryTimeMillis", 0))
        expires_at = dt.datetime.fromtimestamp(expiry_ms / 1000, tz=dt.timezone.utc)

        auto_renew = bool(result.get("autoRenewing", False))
        is_active = expires_at > now

        return is_active, expires_at, auto_renew

    except Exception as e:
        print("[ANDROID VERIFY ERROR]", str(e))
        return False, now, False

# AUTO CHECK FOR SUBSCRIPTION UPDATES FOR ALL USERS
def run_subscription_sync(db: Session):
    """
    Sync USERS table from store verification.
    Keeps users.plan accurate for active accounts.
    """
    rows = db.execute(
        text("""
            SELECT
                id,
                subscription_platform,
                subscription_external_id,
                subscription_end_at,
                subscription_receipt
            FROM users
            WHERE subscription_external_id IS NOT NULL
        """)
    ).mappings().all()

    now = dt.datetime.now(dt.timezone.utc)

    for r in rows:
        uid = r["id"]
        platform = r["subscription_platform"]
        ext_id = r["subscription_external_id"]
        receipt = r["subscription_receipt"]

        if not platform or not ext_id:
            continue

        # Default to inactive
        active = False
        new_end = None
        auto_renew = False
        plan = "Free"

        try:
            if platform == "ios":
                # yearly first
                ok, end_at, ar = verify_ios_subscription(IOS_PRO_YEARLY_PRODUCT_ID, ext_id)
                if ok and end_at and end_at > now:
                    active = True
                    new_end = end_at
                    auto_renew = ar
                    plan = "Pro Yearly"
                else:
                    ok, end_at, ar = verify_ios_subscription(IOS_PRO_MONTHLY_PRODUCT_ID, ext_id)
                    if ok and end_at and end_at > now:
                        active = True
                        new_end = end_at
                        auto_renew = ar
                        plan = "Pro Monthly"
            else:
                # yearly first
                ok, end_at, ar = verify_android_subscription(ANDROID_PRO_YEARLY_PRODUCT_ID, ext_id, receipt)
                if ok and end_at and end_at > now:
                    active = True
                    new_end = end_at
                    auto_renew = ar
                    plan = "Pro Yearly"
                else:
                    ok, end_at, ar = verify_android_subscription(ANDROID_PRO_MONTHLY_PRODUCT_ID, ext_id, receipt)
                    if ok and end_at and end_at > now:
                        active = True
                        new_end = end_at
                        auto_renew = ar
                        plan = "Pro Monthly"
        except Exception:
            active = False

        if not active:
            db.execute(
                text("""
                    UPDATE users
                    SET subscription_end_at        = NULL,
                        subscription_auto_renew    = FALSE,
                        subscription_platform      = NULL,
                        subscription_external_id   = NULL,
                        subscription_receipt       = NULL,
                        plan                       = 'Free',
                        subscription_last_checked_at = :checked_at
                    WHERE id = :id
                """),
                {"id": uid, "checked_at": now_iso()},
            )
            continue

        db.execute(
            text("""
                UPDATE users
                SET subscription_end_at          = :end_at,
                    subscription_auto_renew      = :auto_renew,
                    subscription_last_checked_at = :checked_at,
                    plan                         = :plan
                WHERE id = :id
            """),
            {
                "end_at": new_end.isoformat(),
                "auto_renew": bool(auto_renew),
                "checked_at": now_iso(),
                "plan": plan,
                "id": uid,
            },
        )

    db.commit()

def run_entitlements_sync(db: Session, limit: int = 2000):
    now = dt.datetime.now(dt.timezone.utc)

    ents = db.execute(
        text("""
            SELECT
              platform,
              external_id,
              COALESCE(product_id, '') AS product_id,
              last_seen_user_id,
              last_seen_email
            FROM subscription_entitlements
            ORDER BY last_verified_at ASC NULLS FIRST, updated_at ASC
            LIMIT :lim
        """),
        {"lim": limit},
    ).mappings().all()

    for e in ents:
        platform = e["platform"]
        ext_id = e["external_id"]

        product_id = e["product_id"] or (
            IOS_PRO_MONTHLY_PRODUCT_ID if platform == "ios" else ANDROID_PRO_MONTHLY_PRODUCT_ID
        )

        # Default result
        ok = False
        expires_at = now
        auto_renew = False

        try:
            if platform == "ios":
                ok, expires_at, auto_renew = verify_ios_subscription(product_id, ext_id)
            else:
                ok, expires_at, auto_renew = verify_android_subscription(product_id, ext_id)
        except Exception:
            ok, expires_at, auto_renew = False, now, False

        active = bool(ok and expires_at and expires_at > now)

        # 1) Update entitlement row (iOS)
        db.execute(
            text("""
                UPDATE subscription_entitlements
                SET product_id = :product_id,
                    is_active = :active,
                    expires_at = :exp,
                    auto_renew = :ar,
                    last_verified_at = NOW(),
                    updated_at = NOW()
                WHERE platform = :platform AND external_id = :ext_id
            """),
            {
                "platform": platform,
                "ext_id": ext_id,
                "product_id": product_id,
                "active": active,
                "exp": expires_at.isoformat() if expires_at else None,
                "ar": bool(auto_renew),
            },
        )

        # 2) Optional: update linked user if they still exist
        uid = e.get("last_seen_user_id")
        if uid:
            user_exists = db.execute(
                text("SELECT 1 FROM users WHERE id = :id"),
                {"id": uid},
            ).first()

            if user_exists:
                if not active:
                    db.execute(
                        text("""
                            UPDATE users
                            SET plan = 'Free',
                                subscription_end_at = NULL,
                                subscription_auto_renew = FALSE,
                                subscription_platform = NULL,
                                subscription_external_id = NULL,
                                subscription_receipt = NULL
                            WHERE id = :id
                        """),
                        {"id": uid},
                    )
                else:
                    plan = "Pro Yearly" if "yearly" in (product_id or "").lower() else "Pro Monthly"
                    db.execute(
                        text("""
                            UPDATE users
                            SET plan = :plan,
                                subscription_end_at = :end_at,
                                subscription_auto_renew = :auto_renew,
                                subscription_platform = :platform,
                                subscription_external_id = :ext_id
                            WHERE id = :id
                        """),
                        {
                            "id": uid,
                            "plan": plan,
                            "end_at": expires_at.isoformat(),
                            "auto_renew": bool(auto_renew),
                            "platform": platform,
                            "ext_id": ext_id,
                        },
                    )

    db.commit()