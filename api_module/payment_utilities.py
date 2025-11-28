import requests
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession
import os
from typing import Optional
import datetime as dt
import json

APPLE_SHARED_SECRET = os.environ.get("APPLE_IAP_SHARED_SECRET", "")
APPLE_USE_SANDBOX = os.environ.get("APPLE_IAP_USE_SANDBOX", "false").lower() == "true"
APPLE_VERIFY_RECEIPT_URL = "https://buy.itunes.apple.com/verifyReceipt"
APPLE_VERIFY_RECEIPT_SANDBOX_URL = "https://sandbox.itunes.apple.com/verifyReceipt"

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

def verify_ios_subscription(
    product_id: str,
    receipt: Optional[str],
) -> tuple[bool, dt.datetime, bool]:
    """
    Validate an iOS subscription using Apple's /verifyReceipt endpoint.

    Returns: (is_active, expires_at, auto_renew)
    """
    now = dt.datetime.now(dt.timezone.utc)
    print(receipt)
    print(product_id)
    if not receipt or not APPLE_SHARED_SECRET:
        # Without shared secret we can't really verify; fail closed.
        return False, now, False

    payload = {
        "receipt-data": receipt,
        "password": APPLE_SHARED_SECRET,
        "exclude-old-transactions": True,
    }

    url = APPLE_VERIFY_RECEIPT_URL
    if APPLE_USE_SANDBOX:
        print(APPLE_USE_SANDBOX)
        url = APPLE_VERIFY_RECEIPT_SANDBOX_URL
        print(url)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        print(data)
    except Exception as e:
        print("[subscriptions] verify_ios_subscription request error:", e)
        return False, now, False

    # If production endpoint says "sandbox receipt", retry sandbox
    if data.get("status") == 21007 and not APPLE_USE_SANDBOX:
        print(data.get("status"))
        try:
            resp = requests.post(APPLE_VERIFY_RECEIPT_SANDBOX_URL, json=payload, timeout=10)
            print(resp)
            data = resp.json()
        except Exception as e:
            print("[subscriptions] sandbox retry error:", e)
            return False, now, False

    if data.get("status") != 0:
        print("[subscriptions] Apple verify failed, status:", data.get("status"))
        return False, now, False

    latest = data.get("latest_receipt_info") or data.get("receipt", {}).get("in_app", [])
    print(latest)
    if not latest:
        return False, now, False

    # Filter for this product_id
    filtered = [item for item in latest if item.get("product_id") == product_id]
    print(filtered)
    if not filtered:
        return False, now, False

    # Pick the latest by expires_date_ms
    try:
        item = sorted(
            filtered,
            key=lambda x: int(x.get("expires_date_ms", "0")),
        )[-1]
    except Exception:
        item = filtered[-1]

    expires_ms = int(item.get("expires_date_ms", "0") or "0")
    expires_at = dt.datetime.fromtimestamp(expires_ms / 1000.0, tz=dt.timezone.utc)
    print(expires_at)
    is_active = expires_at > now
    print(is_active)
    # auto-renew status
    auto_renew = True
    for r in data.get("pending_renewal_info") or []:
        if r.get("product_id") == product_id:
            if str(r.get("auto_renew_status")) == "0":
                auto_renew = False
            break

    return is_active, expires_at, auto_renew

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

