import requests
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession
import os
from typing import Optional
import datetime as dt
import json
import base64
from appstoreserverlibrary.api_client import AppStoreServerAPIClient, APIException
from appstoreserverlibrary.models.Environment import Environment
from pathlib import Path

def _load_apple_private_key_from_env() -> bytes:
    """
    Load APPLE_IAP_PRIVATE_KEY from env and normalize it so it works in:
    - local .env with escaped '\n'
    - Render dashboard with real multi-line PEM
    """
    raw = os.environ["APPLE_IAP_PRIVATE_KEY"]

    # Sometimes dotenv keeps surrounding quotes, just in case:
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]

    # Case 1: local .env like "-----BEGIN...-----\nABC\nDEF\n-----END...-----\n"
    # -> convert literal '\n' substrings to real newlines.
    if "\\n" in raw and "BEGIN PRIVATE KEY" in raw:
        raw = raw.replace("\\r\\n", "\\n")  # normalize CRLF escapes just in case
        raw = raw.replace("\\n", "\n")

    # Optional debug while testing (remove later if you want)
    first_line = raw.splitlines()[0] if raw.splitlines() else ""
    last_line = raw.splitlines()[-1] if raw.splitlines() else ""
    print("APPLE_IAP_PRIVATE_KEY first line:", first_line)
    print("APPLE_IAP_PRIVATE_KEY last line:", last_line)

    return raw.encode("utf-8")



APPLE_IAP_KEY_ID = os.environ["APPLE_IAP_KEY_ID"]
APPLE_IAP_ISSUER_ID = os.environ["APPLE_IAP_ISSUER_ID"]
APPLE_IAP_PRIVATE_KEY = os.environ["APPLE_IAP_PRIVATE_KEY"]
APPLE_BUNDLE_ID = os.environ["APPLE_BUNDLE_ID"]
APPLE_USE_SANDBOX = os.environ.get("APPLE_IAP_USE_SANDBOX", "false").lower() == "true"

environment = Environment.SANDBOX if APPLE_USE_SANDBOX else Environment.PRODUCTION
signing_key_bytes = _load_apple_private_key_from_env()

print("Len(APPLE_IAP_PRIVATE_KEY):", len(APPLE_IAP_PRIVATE_KEY))

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
    
def add_month(dt_: dt.datetime) -> dt.datetime:
    year = dt_.year
    month = dt_.month + 1
    if month > 12:
        month = 1
        year += 1
    day = min(dt_.day, 28)
    return dt.datetime(year, month, day, tzinfo=dt_.tzinfo or dt.timezone.utc)

def _decode_jws_payload(jws: str) -> dict:
    """
    Decode the payload of a JWS string WITHOUT verifying its signature.
    This is enough to read productId / expiresDate for your own DB logic.
    """
    try:
        parts = jws.split(".")
        if len(parts) != 3:
            return {}

        payload_b64 = parts[1]
        # Add padding if needed
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        return json.loads(payload_bytes.decode("utf-8"))
    except Exception as e:
        print("[subscriptions] Failed to decode JWS payload:", e)
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
    print("verify_ios_subscription original_transaction_id:", original_transaction_id)
    print("verify_ios_subscription product_id:", product_id)
    if not original_transaction_id:
        return False, now, False

    try:
        # Ask Apple for all subscription statuses for this original transaction
        status_response = app_store_client.get_all_subscription_statuses(
            original_transaction_id
        )
        # --- DEBUG LOG ---
        try:
            print("\n=== APPLE SUBSCRIPTION STATUS RESPONSE ===")
            print(json.dumps(status_response.to_dict(), indent=2))
            print("=== END APPLE RESPONSE ===\n")
        except Exception as e:
            print("[debug] Could not dump status_response:", e)
            print(status_response)

    except APIException as e:
        print("[subscriptions] App Store Server API error:", e)
        return False, now, False

    latest_expires_at = None
    is_active = False
    will_auto_renew = False

    for subscription_group in status_response.data.subscriptions or []:
        for status in subscription_group.status or []:
            for latest_tx in status.latestTransactions or []:
                signed_tx = latest_tx.signedTransactionInfo  # JWS string
                decoded = _decode_jws_payload(signed_tx)
                if not decoded:
                    continue

                tx_product_id = decoded.get("productId")
                if tx_product_id != product_id:
                    continue

                expires_ms = decoded.get("expiresDate")
                if not expires_ms:
                    continue

                expires_at = dt.datetime.fromtimestamp(
                    expires_ms / 1000.0, tz=dt.timezone.utc
                )

                if latest_expires_at is None or expires_at > latest_expires_at:
                    latest_expires_at = expires_at

                    in_ownership = decoded.get("inAppOwnershipType") == "PURCHASED"
                    not_revoked = decoded.get("revocationReason") is None

                    is_active = in_ownership and not_revoked and expires_at > now
                    will_auto_renew = not_revoked and not decoded.get("isUpgraded", False)

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

