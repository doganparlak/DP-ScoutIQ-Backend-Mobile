"""Read-only store transaction collection. Never changes subscription access."""
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote
import datetime as dt
import os
import re

UTC = dt.timezone.utc
PLANS = {
 'scoutwise_no_ads_monthly_ios': 'plus_monthly', 'scoutwise_no_ads_monthly_android': 'plus_monthly',
 'scoutwise_pro_monthly_ios': 'pro_monthly', 'scoutwise_pro_monthly_android': 'pro_monthly',
 'scoutwise_pro_yearly_ios': 'pro_yearly', 'scoutwise_pro_yearly_android': 'pro_yearly',
}

@dataclass(frozen=True)
class StoreEvent:
    transaction_id: str
    subscription_id: str
    product_id: str
    occurred_at: dt.datetime
    period_start: dt.datetime | None = None
    period_end: dt.datetime | None = None
    amount: Decimal | None = None
    currency: str | None = None
    kind: str = 'unknown'
    event_type: str = 'subscription_charge'
    is_test: bool = False
    metadata: dict = field(default_factory=dict)


def timestamp(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = (dt.datetime.fromtimestamp(float(value) / 1000, UTC)
                  if isinstance(value, (int, float)) else dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')))
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def money(value, divisor=1):
    try:
        result = Decimal(str(value)) / divisor
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def google_money(value):
    if not isinstance(value, dict) or not re.fullmatch(r'[A-Z]{3}', value.get('currencyCode', '')):
        return None, None
    try:
        result = Decimal(str(value.get('units', '0'))) + Decimal(str(value.get('nanos', 0))) / 1000000000
        if not result.is_finite() or result < 0:
            return None, None
        return result, value['currencyCode']
    except (InvalidOperation, TypeError, ValueError):
        return None, None


def apple_verifier(environment):
    from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier
    from appstoreserverlibrary.models.Environment import Environment
    roots = [p.read_bytes() for p in sorted((Path(__file__).parent / 'certificates').glob('*.cer'))]
    if not roots:
        raise RuntimeError('Apple verification roots unavailable')
    return SignedDataVerifier(roots, True, Environment(environment), os.environ['APPLE_BUNDLE_ID'],
                              int(os.getenv('APPLE_APP_ID', '6755595849')))


def _payload(value):
    """Retain new Apple fields even when the installed SDK predates them."""
    import jwt
    return jwt.decode(value, options={'verify_signature': False, 'verify_exp': False, 'verify_aud': False}, algorithms=['ES256'])


def verify_apple_notification(signed):
    # Read only the environment hint first; no trusted data leaves this function
    # until Apple's certificate chain, signature and app identity are verified.
    hint = _payload(signed)
    environment = (hint.get('data') or {}).get('environment')
    if environment not in ('Production', 'Sandbox'):
        raise ValueError('Invalid Apple notification environment')
    verifier = apple_verifier(environment)
    verifier.verify_and_decode_notification(signed)
    return hint, verifier


def apple_events(tx):
    product = tx.get('productId')
    start, end = timestamp(tx.get('purchaseDate')), timestamp(tx.get('expiresDate'))
    if product not in PLANS or not start or not end or end <= start or not tx.get('transactionId') or not tx.get('originalTransactionId'):
        return []
    # Family-shared access is not a second payment by the recipient.
    if tx.get('inAppOwnershipType') == 'FAMILY_SHARED':
        return []
    currency = tx.get('currency')
    amount = money(tx.get('price'), 1000)
    if amount is None or not re.fullmatch(r'[A-Z]{3}', currency or ''):
        amount, currency = None, None
    reason = tx.get('transactionReason')
    kind = 'renewal' if reason == 'RENEWAL' else 'first_purchase' if reason == 'PURCHASE' else 'unknown'
    env = tx.get('environment')
    if env not in ('Production', 'Sandbox'):
        raise ValueError('Invalid Apple transaction environment')
    event = StoreEvent(str(tx['transactionId']), str(tx['originalTransactionId']), product, start,
                       start, end, amount, currency, kind, is_test=env == 'Sandbox',
                       metadata={'source': 'apple_verified_transaction', 'offer_identifier': tx.get('offerIdentifier'),
                                 'offer_type': tx.get('offerType'), 'ownership': tx.get('inAppOwnershipType'),
                                 'apple_signed_at': tx.get('signedDate', 0),
                                 'revocation_status': 'refunded' if tx.get('revocationDate') else 'none'})
    events = [event]
    revoked = timestamp(tx.get('revocationDate'))
    if revoked and tx.get('revocationType') not in ('FAMILY_REVOKE', 'ASSIGNMENT_REVOKE'):
        # Newer payloads can describe partial refunds. Unsupported/missing prices
        # remain unpriced, rather than inventing the refunded amount.
        percent = tx.get('revocationPercentage')
        if percent is None and tx.get('revocationType') != 'REFUND_PRORATED':
            percent = 100000
        fraction = money(percent, 100000)
        refunded = amount * fraction if amount is not None and fraction is not None and fraction <= 1 else None
        events.append(replace(event, event_type='subscription_refund', kind='unknown', occurred_at=revoked,
                              amount=refunded, currency=currency if refunded is not None else None,
                              transaction_id=event.transaction_id + ':refund:' + str(tx['revocationDate'])))
    return events


def collect_apple(external_id, since):
    from appstoreserverlibrary.api_client import APIException, APIError, GetTransactionHistoryVersion
    from appstoreserverlibrary.models.TransactionHistoryRequest import TransactionHistoryRequest, Order
    from api_module.payment_utilities import app_store_client, fallback_app_store_client
    client = app_store_client
    request = TransactionHistoryRequest(sort=Order.ASCENDING)
    revision, events, verifier = None, [], None
    for page in range(100):
        try:
            history = client.get_transaction_history(external_id, revision, request, GetTransactionHistoryVersion.V2)
        except APIException as exc:
            if page == 0 and exc.http_status_code == 404 and exc.api_error in (APIError.ORIGINAL_TRANSACTION_ID_NOT_FOUND, APIError.TRANSACTION_ID_NOT_FOUND):
                client = fallback_app_store_client
                history = client.get_transaction_history(external_id, revision, request, GetTransactionHistoryVersion.V2)
            else:
                raise
        for signed in history.signedTransactions or []:
            tx = _payload(signed)
            if str(tx.get('originalTransactionId')) != external_id:
                continue
            if verifier is None:
                verifier = apple_verifier(tx.get('environment'))
            verifier.verify_and_decode_signed_transaction(signed)
            events.extend(apple_events(tx))
        if not history.hasMore:
            return events, None
        if not history.revision or history.revision == revision:
            raise RuntimeError('Apple transaction pagination did not advance')
        revision = history.revision
    raise RuntimeError('Apple transaction pagination limit reached')


def google_session():
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    from api_module.payment_utilities import GOOGLE_PLAY_SERVICE_ACCOUNT_JSON, GOOGLE_PLAY_SCOPES
    credentials = service_account.Credentials.from_service_account_file(GOOGLE_PLAY_SERVICE_ACCOUNT_JSON, scopes=GOOGLE_PLAY_SCOPES)
    return AuthorizedSession(credentials)


def google_get(session, path):
    package = os.environ.get('GOOGLE_PLAY_PACKAGE_NAME', '')
    if not package:
        raise RuntimeError('Google package name unavailable')
    response = session.get('https://androidpublisher.googleapis.com/androidpublisher/v3/applications/'
                           + quote(package, safe='') + '/' + path, timeout=(3, 10))
    response.raise_for_status()
    return response.json()


def google_events(order, subscription, external_id):
    # Only Orders API success/refund states establish a settled transaction.
    if order.get('state') not in ('PROCESSED', 'PARTIALLY_REFUNDED', 'REFUNDED', 'PENDING_REFUND'):
        return []
    history = order.get('orderHistory') or {}
    at = timestamp((history.get('processedEvent') or {}).get('eventTime'))
    if order.get('purchaseToken') != external_id:
        raise ValueError('Google order purchase token mismatch')
    if not at:
        raise ValueError('Successful Google order has no processing timestamp')
    items = [item for item in order.get('lineItems', []) if item.get('productId') in PLANS and item.get('subscriptionDetails')]
    if len(items) != 1:
        return []  # Do not allocate a multi-product amount speculatively.
    item = items[0]
    detail = item['subscriptionDetails']
    start, end = timestamp(detail.get('servicePeriodStartTime')), timestamp(detail.get('servicePeriodEndTime'))
    if start and end and end <= start:
        return []
    order_id = str(order.get('orderId') or '')
    if not order_id:
        return []
    amount, currency = google_money(item.get('total'))
    is_test = isinstance(subscription.get('testPurchase'), dict)
    renewal = re.fullmatch(r'(.+)\.\.(\d+)', order_id)
    kind = 'renewal' if renewal else 'plan_change' if subscription.get('linkedPurchaseToken') else 'first_purchase'
    event = StoreEvent(order_id, external_id, item['productId'], at, start, end, amount, currency, kind,
                       is_test=is_test, metadata={'source': 'google_orders_api', 'base_plan_id': detail.get('basePlanId'),
                                                 'offer_id': detail.get('offerId')})
    events = [event]
    refunds = []
    for refund in history.get('partialRefundEvents', []):
        if refund.get('state') == 'PROCESSED_SUCCESSFULLY':
            refunds.append((refund.get('processTime'), refund.get('refundDetails')))
    if history.get('refundEvent'):
        refund = history['refundEvent']
        refunds.append((refund.get('eventTime'), refund.get('refundDetails')))
    for when, details in refunds:
        occurred = timestamp(when)
        if not occurred:
            continue
        refunded, refund_currency = google_money((details or {}).get('total'))
        events.append(replace(event, event_type='subscription_refund', kind='unknown', occurred_at=occurred,
                              transaction_id=order_id + ':refund:' + str(when), amount=refunded, currency=refund_currency))
    return events


def collect_google(external_id, since):
    with google_session() as session:
        sub = google_get(session, 'purchases/subscriptionsv2/tokens/' + quote(external_id, safe=''))
        if sub.get('subscriptionState') in ('SUBSCRIPTION_STATE_PENDING', 'SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED'):
            return [], sub.get('linkedPurchaseToken')
        events, seen = [], set()
        # Each recurring charge has its own order ID. Walk verified orders back
        # to the attribution boundary so a missed daily run loses no renewals.
        for item in sub.get('lineItems', []):
            latest = item.get('latestSuccessfulOrderId')
            if not latest:
                continue  # latestOrderId can name a pending/declined charge.
            match = re.fullmatch(r'(.+)\.\.(\d+)', latest)
            maximum = int(os.getenv('REFERRAL_MAX_GOOGLE_ORDERS', '120'))
            suffix = int(match[2]) if match else None
            ids = ([match[1] + '..' + str(i) for i in range(suffix, max(-1, suffix-maximum), -1)]
                   if match else [latest])
            if match and suffix < maximum:
                ids.append(match[1])
            reached_boundary = False
            for order_id in ids:
                if order_id in seen:
                    continue
                seen.add(order_id)
                order = google_get(session, 'orders/' + quote(order_id, safe=''))
                batch = google_events(order, sub, external_id)
                if batch and min(e.occurred_at for e in batch) < since:
                    # Keep refunds that happened after the attribution boundary;
                    # the ledger rejects them if their charge was not attributed.
                    events.extend(batch)
                    reached_boundary = True
                    break
                events.extend(batch)
            if match and suffix >= maximum and not reached_boundary:
                raise RuntimeError('Google order history limit reached; increase REFERRAL_MAX_GOOGLE_ORDERS')
        return events, sub.get('linkedPurchaseToken')
