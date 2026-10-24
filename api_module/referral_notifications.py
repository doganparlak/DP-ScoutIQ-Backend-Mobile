"""Authenticated store notifications for referral analytics only.

These handlers never activate, revoke or downgrade a user's subscription.
"""
import base64
import json
import os
from jwt import InvalidTokenError
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from appstoreserverlibrary.signed_data_verifier import VerificationException, VerificationStatus
from api_module.database import SessionLocal
from api_module.referral_billing import record_events, load_snapshot, freeze_attribution, refresh_monthly_reports, track_subscription
from api_module.referral_store_events import verify_apple_notification, apple_events, _payload

router = APIRouter(prefix='/billing/notifications', tags=['Billing notifications'])

class AppleNotification(BaseModel):
    signedPayload: str = Field(min_length=1, max_length=200000)

class GoogleMessage(BaseModel):
    data: str = Field(min_length=1, max_length=100000)
    messageId: str | None = None

class GoogleNotification(BaseModel):
    message: GoogleMessage
    subscription: str | None = None


@router.post('/apple')
def apple_notification(body: AppleNotification):
    try:
        payload, verifier = verify_apple_notification(body.signedPayload)
        signed = (payload.get('data') or {}).get('signedTransactionInfo')
        if not signed:
            return {'ok': True}  # Apple's signed TEST/control notifications.
        verifier.verify_and_decode_signed_transaction(signed)
        tx = _payload(signed)
        events = apple_events(tx)
    except VerificationException as exc:
        # The SDK uses VERIFICATION_FAILURE for unavailable online certificate
        # checks too. Return a retryable response rather than dropping the event.
        status = 503 if exc.status == VerificationStatus.VERIFICATION_FAILURE else 400
        raise HTTPException(status, 'Apple notification verification failed') from None
    except (InvalidTokenError, ValueError, KeyError):
        raise HTTPException(400, 'Invalid Apple notification') from None
    except Exception:
        raise HTTPException(503, 'Apple notification verification unavailable') from None
    external_id = str(tx.get('originalTransactionId') or '')
    try:
        with SessionLocal() as db:
            freeze_attribution(db, 'ios', external_id)
            snapshot = load_snapshot(db, 'ios', external_id)
            # Unknown purchases may precede app activation. Request redelivery;
            # activation + daily sync also reconcile their full history.
            if not snapshot:
                raise HTTPException(503, 'Subscription attribution not available yet')
            changed = record_events(db, dict(snapshot), events)
            if changed:
                refresh_monthly_reports(db)
            db.commit()
        return {'ok': True}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Referral analytics unavailable; retry notification') from None


@router.post('/google')
def google_notification(body: GoogleNotification, authorization: str | None = Header(default=None)):
    audience = os.getenv('GOOGLE_RTDN_AUDIENCE', '').strip()
    email = os.getenv('GOOGLE_RTDN_SERVICE_ACCOUNT_EMAIL', '').strip()
    if not audience or not email:
        raise HTTPException(503, 'Google notification authentication is not configured')
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Google notification authentication required')
    try:
        claims = id_token.verify_oauth2_token(authorization[7:], Request(), audience=audience)
        if claims.get('email') != email or claims.get('email_verified') is not True:
            raise ValueError('Unexpected sender')
        payload = json.loads(base64.b64decode(body.message.data, validate=True))
        if payload.get('packageName') != os.environ.get('GOOGLE_PLAY_PACKAGE_NAME'):
            raise ValueError('Unexpected package')
        if payload.get('testNotification'):
            return {'ok': True}
        notification = payload.get('subscriptionNotification')
        if not notification:
            # Voided purchases include refunds for an older charge. Its order
            # is fetched separately by the tracking collector when supplied.
            notification = payload.get('voidedPurchaseNotification') or {}
        token = notification.get('purchaseToken')
        if not isinstance(token, str) or not token:
            return {'ok': True}
    except Exception:
        raise HTTPException(401, 'Invalid Google notification') from None
    try:
        with SessionLocal() as db:
            known = load_snapshot(db, 'android', token)
        if not known:
            raise HTTPException(503, 'Subscription attribution not available yet')
        track_subscription('android', token)
        return {'ok': True}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Referral analytics unavailable; retry notification') from None
