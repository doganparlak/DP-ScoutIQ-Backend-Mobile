"""Provider verification. Never accept an email or subject supplied by the client."""
import base64
import hashlib
import hmac
import os
import ssl
import time
from functools import lru_cache

import jwt
import requests
import certifi
from cryptography.fernet import Fernet
from fastapi import HTTPException


def setting(name):
    return os.getenv(name, '').strip()


def apple_client_secret(client_id):
    now = int(time.time())
    return jwt.encode({'iss': setting('APPLE_SIGNIN_TEAM_ID'), 'iat': now, 'exp': now + 300,
                       'aud': 'https://appleid.apple.com', 'sub': client_id},
                      setting('APPLE_SIGNIN_PRIVATE_KEY').replace('\\n', '\n'), algorithm='ES256',
                      headers={'kid': setting('APPLE_SIGNIN_KEY_ID')})


def cipher():
    secret = setting('SOCIAL_AUTH_ENCRYPTION_KEY')
    if len(secret) < 32:
        raise HTTPException(503, 'Social sign-in is not configured')
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


@lru_cache(maxsize=2)
def public_keys(provider):
    url = ('https://appleid.apple.com/auth/keys' if provider == 'apple'
           else 'https://www.googleapis.com/oauth2/v3/certs')
    return jwt.PyJWKClient(url, cache_jwk_set=True, lifespan=3600, timeout=8,
                           ssl_context=ssl.create_default_context(cafile=certifi.where()))


def verify_identity(provider, token, audience, nonce=None):
    try:
        key = public_keys(provider).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=['RS256'], audience=audience,
                            issuer=('https://appleid.apple.com' if provider == 'apple'
                                    else ['accounts.google.com', 'https://accounts.google.com']),
                            options={'require': ['sub', 'iss', 'aud', 'exp', 'iat']}, leeway=30)
        if nonce is not None and not hmac.compare_digest(str(claims.get('nonce', '')), nonce):
            raise ValueError('Nonce mismatch')
        if not isinstance(claims['sub'], str) or not claims['sub'] or len(claims['sub']) > 255:
            raise ValueError('Invalid subject')
        verified = claims.get('email_verified') in (True, 'true')
        return {'subject': claims['sub'], 'email': claims.get('email') if verified else None}
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(503, 'Sign-in provider is temporarily unavailable. Please try again.') from exc
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(401, 'Provider verification failed. Please sign in again.') from exc
    except Exception as exc:
        raise HTTPException(503, 'Sign-in provider is temporarily unavailable. Please try again.') from exc


def apple_exchange(code, client_id, nonce, redirect_uri=None):
    try:
        secret = apple_client_secret(client_id)
    except Exception as exc:
        raise HTTPException(503, 'Apple sign-in is not configured correctly') from exc
    data = {'client_id': client_id, 'client_secret': secret,
            'code': code, 'grant_type': 'authorization_code'}
    if redirect_uri:
        data['redirect_uri'] = redirect_uri
    try:
        response = requests.post('https://appleid.apple.com/auth/token', data=data, timeout=10)
        if response.status_code >= 500:
            raise HTTPException(503, 'Apple is temporarily unavailable')
        if response.status_code != 200:
            raise HTTPException(401, 'Apple authorization expired. Please sign in again.')
        tokens = response.json()
        identity = verify_identity('apple', tokens['id_token'], client_id, nonce)
        if not tokens.get('refresh_token'):
            raise HTTPException(401, 'Apple authorization is incomplete. Please sign in again.')
        identity['refresh_token_encrypted'] = cipher().encrypt(tokens['refresh_token'].encode()).decode()
        identity['apple_client_id'] = client_id
        return identity
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise HTTPException(503, 'Apple is temporarily unavailable. Please try again.') from exc


def revoke_apple_identity(identity):
    encrypted = identity.get('apple_refresh_token_encrypted')
    if not encrypted:
        return
    try:
        response = requests.post('https://appleid.apple.com/auth/revoke', data={
            'client_id': identity['apple_client_id'],
            'client_secret': apple_client_secret(identity['apple_client_id']),
            'token': cipher().decrypt(encrypted.encode()).decode(), 'token_type_hint': 'refresh_token',
        }, timeout=10)
        if response.status_code != 200:
            raise ValueError('Revocation unsuccessful')
    except Exception as exc:
        raise HTTPException(503, 'Apple connection could not be revoked. Please retry account deletion.') from exc
