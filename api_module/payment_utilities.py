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


IOS_PRO_PRODUCT_ID = os.getenv("IOS_PRO_PRODUCT_ID", "scoutwise_pro_monthly_ios")
ANDROID_PRO_PRODUCT_ID = os.getenv("ANDROID_PRO_PRODUCT_ID", "scoutwise_pro_monthly_android")
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

GOOGLE_PLAY_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "")
GOOGLE_PLAY_PACKAGE_NAME = os.environ.get("GOOGLE_PLAY_PACKAGE_NAME", "")
GOOGLE_PLAY_SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]

_google_play_session: Optional[AuthorizedSession] = None

def _get_google_play_session() -> Optional[AuthorizedSession]:
    global _google_play_session
    if _google_play_session is not None:
        return _google_play_session
    if not GOOGLE_PLAY_SERVICE_ACCOUNT_JSON:
        return None
    try:
        info = json.loads(GOOGLE_PLAY_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=GOOGLE_PLAY_SCOPES,
        )
        _google_play_session = AuthorizedSession(creds)
        return _google_play_session
    except Exception as e:
        print("[subscriptions] could not create Google Play session:", e)
        return None
        
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
        print("[subscriptions] App Store Server API error:", e)
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
                print("[subscriptions] bad expiresDate in tx:", e, decoded_tx)
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
    external_id: str,
    receipt: Optional[str],
) -> tuple[bool, dt.datetime, bool]:
    """
    Validate an Android subscription using Google Play Developer API.

    external_id = purchaseToken.

    Returns: (is_active, expires_at, auto_renew)
    """
    now = dt.datetime.now(dt.timezone.utc)
    if not GOOGLE_PLAY_PACKAGE_NAME:
        # We can't verify without package name; fail closed.
        return False, now, False

    session = _get_google_play_session()
    if session is None:
        return False, now, False

    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3"
        f"/applications/{GOOGLE_PLAY_PACKAGE_NAME}"
        f"/purchases/subscriptionsv2/tokens/{external_id}"
    )

    try:
        resp = session.get(url, timeout=10)
        if resp.status_code != 200:
            print("[subscriptions] Google Play verify status:", resp.status_code, resp.text)
            return False, now, False
        data = resp.json()
    except Exception as e:
        print("[subscriptions] verify_android_subscription error:", e)
        return False, now, False

    line_items = data.get("lineItems") or []
    if not line_items:
        return False, now, False

    item = line_items[0]
    expiry_time = item.get("expiryTime")
    if not expiry_time:
        return False, now, False

    try:
        # expiryTime is RFC3339
        expires_at = dt.datetime.fromisoformat(expiry_time.replace("Z", "+00:00"))
    except Exception:
        expires_at = now

    is_active = expires_at > now

    state = data.get("subscriptionState")
    auto_renew = state not in {"CANCELED", "EXPIRED", "PAUSED"}

    return is_active, expires_at, auto_renew

# AUTO CHECK FOR SUBSCRIPTION UPDATES FOR ALL USERS
def run_subscription_sync(db: Session):
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
    print("ROWS FETCHED FOR SUBSCRIPTION SYNC:", len(rows))
    now = dt.datetime.now(dt.timezone.utc)

    for r in rows:
        uid = r["id"]
        platform = r["subscription_platform"]
        ext_id = r["subscription_external_id"]
        receipt = r["subscription_receipt"]

        if not platform or not ext_id:
            continue

        if platform == "ios":
            ok, new_end, auto_renew = verify_ios_subscription(
                IOS_PRO_PRODUCT_ID, 
                ext_id
            )
        else:
            ok, new_end, auto_renew = verify_android_subscription(
                ANDROID_PRO_PRODUCT_ID, 
                ext_id,
                receipt
            )

        # If verification fails or it's no longer active
        if not ok or new_end <= now:
            db.execute(
                text("""
                    UPDATE users
                    SET subscription_end_at      = NULL,
                        subscription_auto_renew  = FALSE,
                        subscription_platform    = NULL,
                        subscription_external_id = NULL,
                        subscription_receipt     = NULL,
                        plan                     = 'Free'
                    WHERE id = :id
                """),
                {"id": uid},
            )
            continue

        # Still active, update expiry and keep Pro
        db.execute(
            text("""
                UPDATE users
                SET subscription_end_at         = :end_at,
                    subscription_auto_renew     = :auto_renew,
                    subscription_last_checked_at = :checked_at,
                    plan                         = 'Pro'
                WHERE id = :id
            """),
            {
                "end_at": new_end.isoformat(),
                "auto_renew": auto_renew,
                "checked_at": now_iso(),
                "id": uid,
            },
        )

    db.commit()