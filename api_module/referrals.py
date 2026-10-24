"""Private referral attribution and per-plan store offer configuration.

Only the authenticated user's mapping is returned; there is no public code list.
Store billing remains the authority for eligibility, pricing and entitlements.
"""
import datetime as dt
import logging
import os
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from api_module.database import get_db
from api_module.utilities import require_auth
from api_module.apple_offers import get_apple_offer

router = APIRouter(tags=['referrals'])
logger = logging.getLogger(__name__)
PRODUCTS = {
    'plus_monthly': {'ios': 'scoutwise_no_ads_monthly_ios', 'android': 'scoutwise_no_ads_monthly_android'},
    'pro_monthly': {'ios': 'scoutwise_pro_monthly_ios', 'android': 'scoutwise_pro_monthly_android'},
    'pro_yearly': {'ios': 'scoutwise_pro_yearly_ios', 'android': 'scoutwise_pro_yearly_android'},
}


class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class StoreOffer(BaseModel):
    model_config = ConfigDict(extra='ignore')
    enabled: bool = True
    sandbox_only: bool = False
    product_id: str
    starts_at: dt.datetime | None = None
    ends_at: dt.datetime | None = None
    countries: list[str] = Field(default_factory=list)
    redemption_code: str | None = Field(default=None, pattern=r'^[A-Za-z0-9]{1,64}$')
    base_plan_id: str | None = None
    offer_id: str | None = None
    offer_code_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9-]{1,128}$')


def normalize_code(value):
    code = (value or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{1,32}', code):
        raise HTTPException(400, 'Invalid referral code')
    return code


def active_code(db, value):
    code = normalize_code(value)
    row = db.execute(text('SELECT code, offer_config FROM referral_codes WHERE code=:code AND active=TRUE'), {'code': code}).mappings().first()
    if not row:
        raise HTTPException(400, 'Invalid or inactive referral code')
    return row


def attach_referral(db, user_id, value):
    if not value or not value.strip():
        return
    row = active_code(db, value)
    user = db.execute(text('SELECT referral_code FROM users WHERE id=:uid FOR UPDATE'), {'uid': user_id}).mappings().one()
    if user['referral_code'] and user['referral_code'] != row['code']:
        raise HTTPException(409, 'An account can only have one referral code')
    db.execute(text('UPDATE users SET referral_code=:code,referral_code_set_at=NOW() WHERE id=:uid AND referral_code IS NULL'), {'code': row['code'], 'uid': user_id})


def public_offers(config, platform, storefront, store_environment='production'):
    result = {}
    now = dt.datetime.now(dt.timezone.utc)
    for plan, products in PRODUCTS.items():
        raw = config.get(plan, {}).get(platform) if isinstance(config.get(plan), dict) else None
        if not isinstance(raw, dict):
            continue
        try:
            offer = StoreOffer.model_validate(raw)
        except ValidationError:
            logger.warning('Invalid referral offer configuration for %s/%s', plan, platform)
            continue
        if not offer.enabled or offer.product_id != products[platform]:
            continue
        if any(value and value.tzinfo is None for value in (offer.starts_at, offer.ends_at)):
            continue
        if offer.starts_at and now < offer.starts_at or offer.ends_at and now >= offer.ends_at:
            continue
        if offer.countries and storefront not in offer.countries:
            continue
        if platform == 'ios':
            sandbox = offer.sandbox_only
            if sandbox and (store_environment != 'sandbox'
                            or os.getenv('APPLE_OFFER_SANDBOX_ENABLED', '').lower() != 'true'):
                continue
            if not offer.offer_code_id or (not sandbox and not offer.redemption_code):
                continue
            apple = get_apple_offer(offer.offer_code_id)
            if not apple or apple['product_id'] != offer.product_id:
                continue
            if not sandbox:
                code = apple.get('custom_codes', {}).get(offer.redemption_code)
                if not code or not code['active']:
                    continue
                if code.get('expires_at') and now >= dt.datetime.fromisoformat(code['expires_at']):
                    continue
            # A country with no Apple price is outside the offer's availability.
            if storefront and storefront not in apple['prices']:
                continue
        if platform == 'android' and (not offer.base_plan_id or not offer.offer_id):
            continue
        # Do not expose other storefront prices or unrelated partner metadata.
        data = {'productId': offer.product_id}
        if platform == 'ios':
            data.update(period=apple['period'],
                        cycles=apple['cycles'], price=apple['prices'].get(storefront))
            if sandbox:
                data['sandbox'] = True
            else:
                data['redemptionCode'] = offer.redemption_code
        else:
            data.update(basePlanId=offer.base_plan_id, offerId=offer.offer_id)
        result[plan] = data
    return result


@router.post('/auth/referral/validate')
def validate_code(body: CodeIn, db: Session = Depends(get_db)):
    return {'code': active_code(db, body.code)['code']}


@router.post('/me/referral')
def set_referral(body: CodeIn, user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    attach_referral(db, user_id, body.code)
    db.commit()
    return {'code': normalize_code(body.code)}


@router.get('/me/referral')
def get_referral(platform: Literal['ios', 'android'], storefront: str = '',
                 store_environment: Literal['production', 'sandbox'] = 'production',
                 user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    row = db.execute(text('''SELECT u.referral_code, r.active, r.offer_config
        FROM users u LEFT JOIN referral_codes r ON r.code=u.referral_code WHERE u.id=:uid'''), {'uid': user_id}).mappings().first()
    if not row:
        raise HTTPException(404, 'Account unavailable')
    config = row['offer_config'] if row['active'] and isinstance(row['offer_config'], dict) else {}
    return {'code': row['referral_code'], 'offers': public_offers(config, platform, storefront.strip().upper(), store_environment)}
