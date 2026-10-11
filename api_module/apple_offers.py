"""Read-only App Store Connect offer-code metadata, cached across users.

Private API credentials stay on the backend. This module never creates, redeems
or edits an offer, and never changes a user's subscription.
"""
from collections import OrderedDict
from decimal import Decimal
import datetime as dt
import logging
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import jwt
import requests

logger = logging.getLogger(__name__)
BASE = 'https://api.appstoreconnect.apple.com/v1'
TTL = 600
_cache = OrderedDict()
_lock = threading.Lock()
DURATIONS = {
    'THREE_DAYS': 'P3D', 'ONE_WEEK': 'P1W', 'TWO_WEEKS': 'P2W',
    'ONE_MONTH': 'P1M', 'TWO_MONTHS': 'P2M', 'THREE_MONTHS': 'P3M',
    'SIX_MONTHS': 'P6M', 'ONE_YEAR': 'P1Y',
}


def _credentials():
    key_id = os.getenv('APPLE_ASC_KEY_ID', '').strip()
    issuer = os.getenv('APPLE_ASC_ISSUER_ID', '').strip()
    key = os.getenv('APPLE_ASC_PRIVATE_KEY', '').strip()
    path = os.getenv('APPLE_ASC_PRIVATE_KEY_PATH', '').strip()
    if not key and path:
        key = Path(path).read_text()
    if not (key_id and issuer and key):
        raise ValueError('App Store Connect credentials unavailable')
    return key_id, issuer, key.replace('\\n', '\n')


def _read(session, url, params=None):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.netloc != 'api.appstoreconnect.apple.com' or not parsed.path.startswith('/v1/'):
        raise ValueError('Invalid Apple pagination URL')
    response = session.get(url, params=params, timeout=(3, 6), allow_redirects=False)
    response.raise_for_status()
    return response.json()


def _fetch(offer_id, credentials):
    key_id, issuer, key = credentials
    now = int(time.time())
    token = jwt.encode({'iss': issuer, 'iat': now, 'exp': now + 120,
                        'aud': 'appstoreconnect-v1'}, key, algorithm='ES256',
                       headers={'kid': key_id, 'typ': 'JWT'})
    with requests.Session() as session:
        session.headers['Authorization'] = 'Bearer ' + token
        document = _read(session, f'{BASE}/subscriptionOfferCodes/{offer_id}', {'include': 'subscription'})
        resource = document['data']
        attributes = resource['attributes']
        subscription_id = resource['relationships']['subscription']['data']['id']
        subscription = next(item for item in document.get('included', [])
                            if item['type'] == 'subscriptions' and item['id'] == subscription_id)
        period = DURATIONS.get(attributes.get('duration'))
        if not period or not attributes.get('active'):
            return None
        # These offers match the current UI: discounted periods followed by normal renewal.
        # An introductory offer stacked ahead of this offer needs a separate phase display.
        if not attributes.get('autoRenewEnabled') or attributes.get('offerEligibility') != 'REPLACE_INTRO_OFFERS':
            return None
        if attributes.get('offerMode') not in ('PAY_AS_YOU_GO', 'PAY_UP_FRONT', 'FREE'):
            return None
        cycles = int(attributes['numberOfPeriods'])
        if not 1 <= cycles <= 120:
            return None
        prices = {}
        url = f'{BASE}/subscriptionOfferCodes/{offer_id}/prices'
        params = {'include': 'subscriptionPricePoint,territory', 'limit': 200}
        for _ in range(20):
            page = _read(session, url, params)
            included = {(item['type'], item['id']): item.get('attributes', {})
                        for item in page.get('included', [])}
            for price in page['data']:
                relationships = price['relationships']
                country = relationships['territory']['data']['id']
                point = relationships['subscriptionPricePoint']['data']['id']
                amount = Decimal(included[('subscriptionPricePoints', point)]['customerPrice'])
                currency = included[('territories', country)]['currency']
                if not amount.is_finite() or amount < 0:
                    raise ValueError('Invalid Apple offer price')
                prices[country] = {'amount': float(amount), 'currency': currency}
            url = page.get('links', {}).get('next')
            params = None
            if not url:
                break
        else:
            raise ValueError('Apple pagination limit exceeded')
        custom_codes = {}
        url = f'{BASE}/subscriptionOfferCodes/{offer_id}/customCodes'
        for _ in range(20):
            page = _read(session, url)
            for code in page.get('data', []):
                info = code['attributes']
                expiration = info.get('expirationDate')
                expires_at = None
                if expiration:
                    # Apple codes expire at midnight Pacific on the specified date.
                    expires_at = dt.datetime.combine(
                        dt.date.fromisoformat(expiration), dt.time(),
                        tzinfo=ZoneInfo('America/Los_Angeles'),
                    ).astimezone(dt.timezone.utc).isoformat()
                custom_codes[info['customCode']] = {
                    'active': bool(info.get('active')), 'expires_at': expires_at,
                }
            url = page.get('links', {}).get('next')
            if not url:
                break
        else:
            raise ValueError('Apple custom code pagination limit exceeded')
        return {'product_id': subscription['attributes']['productId'],
                'period': period, 'cycles': cycles, 'prices': prices,
                'custom_codes': custom_codes}


def get_apple_offer(offer_id):
    """Return Apple-authoritative prices/duration, or hide the offer on failure.

Failures are cached briefly to avoid repeated requests while Apple is unavailable.
Expired cached prices are not served as current offers.
"""
    cache_key = (os.getenv('APPLE_ASC_KEY_ID'), os.getenv('APPLE_ASC_ISSUER_ID'), offer_id)
    with _lock:
        cached = _cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            result = _fetch(offer_id, _credentials())
            lifetime = TTL
        except Exception as exc:
            # No request headers, tokens, private keys or response bodies in logs.
            logger.warning('Apple offer metadata unavailable (%s)', type(exc).__name__)
            result, lifetime = None, 30
        _cache[cache_key] = (time.monotonic() + lifetime, result)
        _cache.move_to_end(cache_key)
        while len(_cache) > 64:
            _cache.popitem(last=False)
        return result
