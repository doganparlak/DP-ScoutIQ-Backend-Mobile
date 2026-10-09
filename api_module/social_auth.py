"""Apple/Google login without changing ScoutWise user IDs or purchase ownership."""
import datetime as dt
import hashlib
import hmac
import json
import secrets
from typing import Literal
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api_module.auth_session import authenticated_session
from api_module.database import get_db
from api_module.social_tokens import apple_exchange, setting, verify_identity
from api_module.utilities import hash_pw, require_auth

router = APIRouter(tags=['social-auth'])
RETURN_URL = 'dpscoutiq://auth/apple'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def available(db):
    return bool(db.execute(text("SELECT to_regclass('public.user_identities') IS NOT NULL AND to_regclass('public.social_auth_flows') IS NOT NULL")).scalar())


def config(db):
    ready = setting('SOCIAL_AUTH_ENABLED').lower() == 'true' and available(db)
    apple = ready and all(setting(k) for k in ('APPLE_SIGNIN_TEAM_ID', 'APPLE_SIGNIN_KEY_ID', 'APPLE_SIGNIN_PRIVATE_KEY')) and len(setting('SOCIAL_AUTH_ENCRYPTION_KEY')) >= 32
    google = ready and bool(setting('GOOGLE_SIGNIN_WEB_CLIENT_ID'))
    return {'appleIOS': bool(apple and setting('APPLE_SIGNIN_IOS_CLIENT_ID')),
            'appleAndroid': bool(apple and setting('APPLE_SIGNIN_SERVICE_ID') and setting('APPLE_SIGNIN_CALLBACK_URL').startswith('https://')),
            'googleAndroid': bool(google), 'googleIOS': bool(google and setting('GOOGLE_SIGNIN_IOS_CLIENT_ID')),
            'googleWebClientId': setting('GOOGLE_SIGNIN_WEB_CLIENT_ID') if google else '',
            'googleIOSClientId': setting('GOOGLE_SIGNIN_IOS_CLIENT_ID') if google else ''}


@router.get('/auth/social/config')
def get_config(db: Session = Depends(get_db)):
    return config(db)


class StartIn(BaseModel):
    provider: Literal['apple', 'google']
    platform: Literal['ios', 'android']
    intent: Literal['signin', 'connect'] = 'signin'


class ChallengeIn(BaseModel):
    challenge: str = Field(min_length=40, max_length=128)


class ExchangeIn(ChallengeIn):
    idToken: str = Field(min_length=10, max_length=20000)
    authorizationCode: str | None = Field(default=None, max_length=4000)
    uiLanguage: Literal['en', 'tr'] = 'en'


class RegisterIn(ChallengeIn):
    dob: dt.date | None = None
    country: str = Field(min_length=1, max_length=100)
    privacyAccepted: bool
    termsAccepted: bool
    dataUsageAccepted: bool
    newsletter: bool = False
    uiLanguage: Literal['en', 'tr'] = 'en'


class LinkIn(ChallengeIn):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    uiLanguage: Literal['en', 'tr'] = 'en'


def flow_row(db, token, *, verified=False):
    if not available(db):
        raise HTTPException(503, 'Social sign-in is not enabled yet')
    row = db.execute(text('''SELECT * FROM social_auth_flows WHERE token_hash=:hash
        AND expires_at>NOW() FOR UPDATE'''), {'hash': digest(token)}).mappings().first()
    if not row or row['attempts'] >= 5 or (verified and not row['identity']):
        raise HTTPException(401, 'This sign-in request expired. Please start again.')
    return row


def attempt(db, token):
    row = flow_row(db, token)
    db.execute(text('UPDATE social_auth_flows SET attempts=attempts+1 WHERE token_hash=:hash'), {'hash': digest(token)})
    db.commit()  # Failed provider/password attempts count too.
    return row


def lock_identity(db, provider, subject):
    # Serialize identity creation/linking across workers without changing user IDs.
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': provider + ':' + subject})


def consume(db, token):
    db.execute(text('DELETE FROM social_auth_flows WHERE token_hash=:hash'), {'hash': digest(token)})


def attach(db, user_id, provider, identity):
    lock_identity(db, provider, identity['subject'])
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': f'user-provider:{user_id}:{provider}'})
    existing = db.execute(text('SELECT user_id FROM user_identities WHERE provider=:p AND provider_subject=:sub'), {'p': provider, 'sub': identity['subject']}).scalar()
    if existing is not None and int(existing) != int(user_id):
        raise HTTPException(409, 'This provider account is already connected to another ScoutWise account.')
    other = db.execute(text('SELECT provider_subject FROM user_identities WHERE user_id=:uid AND provider=:p'), {'uid': user_id, 'p': provider}).scalar()
    if other is not None and other != identity['subject']:
        raise HTTPException(409, 'A different account from this provider is already connected.')
    db.execute(text('''INSERT INTO user_identities(provider,provider_subject,user_id,provider_email,apple_refresh_token_encrypted,apple_client_id)
        VALUES(:p,:sub,:uid,:email,:refresh,:client)
        ON CONFLICT(provider,provider_subject) DO UPDATE SET
          last_login_at=NOW(), provider_email=COALESCE(EXCLUDED.provider_email,user_identities.provider_email),
          apple_refresh_token_encrypted=COALESCE(EXCLUDED.apple_refresh_token_encrypted,user_identities.apple_refresh_token_encrypted),
          apple_client_id=COALESCE(EXCLUDED.apple_client_id,user_identities.apple_client_id)'''), {
        'p': provider, 'sub': identity['subject'], 'uid': user_id, 'email': identity.get('email'),
        'refresh': identity.get('refresh_token_encrypted'), 'client': identity.get('apple_client_id'),
    })


def complete(db, token, language='en'):
    flow = flow_row(db, token, verified=True)
    identity = flow['identity']
    lock_identity(db, flow['provider'], identity['subject'])
    if flow['connect_user_id'] is not None:
        attach(db, flow['connect_user_id'], flow['provider'], identity)
        consume(db, token)
        db.commit()
        return {'status': 'connected'}
    user_id = db.execute(text('SELECT user_id FROM user_identities WHERE provider=:p AND provider_subject=:sub'), {'p': flow['provider'], 'sub': identity['subject']}).scalar()
    if user_id is None:
        db.commit()
        # Never infer account ownership from provider email, even when verified.
        return {'status': 'pending', 'challenge': token, 'provider': flow['provider'], 'email': identity.get('email')}
    attach(db, user_id, flow['provider'], identity)
    result = authenticated_session(db, user_id, language)
    consume(db, token)
    db.commit()
    return {'status': 'authenticated', **result}


@router.post('/auth/social/start')
def start(body: StartIn, request: Request, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    enabled = config(db)
    if not enabled[body.provider + ('IOS' if body.platform == 'ios' else 'Android')]:
        raise HTTPException(503, 'This sign-in option is not configured yet')
    user_id = require_auth(authorization) if body.intent == 'connect' else None
    token = secrets.token_urlsafe(48)
    nonce = digest(secrets.token_urlsafe(32))
    browser = body.provider == 'apple' and body.platform == 'android'
    client = (setting('APPLE_SIGNIN_SERVICE_ID') if browser else setting('APPLE_SIGNIN_IOS_CLIENT_ID')) if body.provider == 'apple' else setting('GOOGLE_SIGNIN_WEB_CLIENT_ID')
    ip_hash = digest((request.client.host if request.client else 'unknown') + ':social')
    db.execute(text('DELETE FROM social_auth_flows WHERE expires_at<NOW()'))
    # Bound unauthenticated provider requests, and retain failures until expiry.
    count = db.execute(text('SELECT count(*) FROM social_auth_flows WHERE ip_hash=:ip AND created_at>NOW()-INTERVAL \'15 minutes\''), {'ip': ip_hash}).scalar()
    if count >= 40:
        raise HTTPException(429, 'Too many sign-in attempts. Please try again later.')
    db.execute(text('''INSERT INTO social_auth_flows(token_hash,provider,client_id,nonce,connect_user_id,browser,ip_hash)
        VALUES(:hash,:p,:client,:nonce,:uid,:browser,:ip)'''), {'hash': digest(token), 'p': body.provider, 'client': client, 'nonce': nonce, 'uid': user_id, 'browser': browser, 'ip': ip_hash})
    db.commit()
    result = {'challenge': token, 'nonce': nonce}
    if browser:
        result['authorizationUrl'] = 'https://appleid.apple.com/auth/authorize?' + urlencode({
            'client_id': client, 'redirect_uri': setting('APPLE_SIGNIN_CALLBACK_URL'),
            'response_type': 'code', 'response_mode': 'form_post', 'scope': 'name email',
            'state': token, 'nonce': nonce,
        })
    return result


@router.post('/auth/social/exchange')
def exchange(body: ExchangeIn, db: Session = Depends(get_db)):
    flow = attempt(db, body.challenge)
    if flow['browser'] or flow['identity'] is not None:
        raise HTTPException(400, 'Invalid sign-in flow')
    identity = verify_identity(flow['provider'], body.idToken, flow['client_id'], flow['nonce'] if flow['provider'] == 'apple' else None)
    if flow['provider'] == 'apple':
        if not body.authorizationCode:
            raise HTTPException(400, 'Apple authorization is required')
        exchanged = apple_exchange(body.authorizationCode, flow['client_id'], flow['nonce'])
        if exchanged['subject'] != identity['subject']:
            raise HTTPException(401, 'Apple identity mismatch')
        identity = exchanged
    # Re-lock after network verification and reject simultaneous/replayed exchanges.
    flow = flow_row(db, body.challenge)
    if flow['identity'] is not None:
        raise HTTPException(409, 'Sign-in already processed')
    db.execute(text('UPDATE social_auth_flows SET identity=CAST(:identity AS jsonb) WHERE token_hash=:hash'), {'hash': digest(body.challenge), 'identity': json.dumps(identity)})
    db.commit()
    return complete(db, body.challenge, body.uiLanguage)


@router.post('/auth/social/apple/callback')
async def apple_callback(request: Request, db: Session = Depends(get_db)):
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > 24000:
            raise HTTPException(413, 'Invalid callback')
        payload.extend(chunk)
    try:
        form = parse_qs(payload.decode('utf-8'))
    except UnicodeDecodeError as exc:
        raise HTTPException(400, 'Invalid callback') from exc
    token = form.get('state', [''])[0]
    if not 40 <= len(token) <= 128:
        raise HTTPException(400, 'Invalid callback state')
    flow = attempt(db, token)
    if flow['provider'] != 'apple' or not flow['browser'] or flow['identity'] is not None:
        raise HTTPException(400, 'Invalid callback')
    if form.get('error'):
        consume(db, token)
        db.commit()
        return RedirectResponse(RETURN_URL + '?cancelled=1', status_code=303)
    try:
        identity = apple_exchange(form.get('code', [''])[0], flow['client_id'], flow['nonce'], setting('APPLE_SIGNIN_CALLBACK_URL'))
    except HTTPException:
        consume(db, token)
        db.commit()
        return RedirectResponse(RETURN_URL + '?failed=1', status_code=303)
    flow = flow_row(db, token)
    if flow['identity'] is not None:
        raise HTTPException(409, 'Sign-in already processed')
    db.execute(text('UPDATE social_auth_flows SET identity=CAST(:i AS jsonb) WHERE token_hash=:h'), {'h': digest(token), 'i': json.dumps(identity)})
    db.commit()
    # No credential, user email or session token is placed in the deep link.
    return RedirectResponse(RETURN_URL + '?completed=1', status_code=303)


class FinishIn(ChallengeIn):
    uiLanguage: Literal['en', 'tr'] = 'en'


@router.post('/auth/social/finish')
def finish(body: FinishIn, db: Session = Depends(get_db)):
    return complete(db, body.challenge, body.uiLanguage)


@router.post('/auth/social/link')
def link_existing(body: LinkIn, db: Session = Depends(get_db)):
    flow = attempt(db, body.challenge)
    if not flow['identity'] or flow['connect_user_id'] is not None:
        raise HTTPException(400, 'Invalid link request')
    user = db.execute(text('SELECT id,password_hash,salt FROM users WHERE lower(email)=lower(:email)'), {'email': body.email.strip()}).mappings().first()
    if not user or not user['password_hash'] or not user['salt'] or not hmac.compare_digest(hash_pw(body.password, user['salt']), user['password_hash']):
        raise HTTPException(401, 'Invalid email or password')
    flow = flow_row(db, body.challenge, verified=True)
    attach(db, user['id'], flow['provider'], flow['identity'])
    result = authenticated_session(db, user['id'], body.uiLanguage, allow_email_restore=True)
    consume(db, body.challenge)
    db.commit()
    return {'status': 'authenticated', **result}


@router.post('/auth/social/register')
def register(body: RegisterIn, db: Session = Depends(get_db)):
    flow = flow_row(db, body.challenge, verified=True)
    if flow['connect_user_id'] is not None:
        raise HTTPException(400, 'Invalid registration request')
    if not (body.privacyAccepted and body.termsAccepted and body.dataUsageAccepted):
        raise HTTPException(400, 'Please accept the privacy policy, terms and data usage information')
    if body.dob and (body.dob > dt.date.today() or body.dob.year < 1900):
        raise HTTPException(400, 'Invalid birth date')
    identity = flow['identity']
    email = str(identity.get('email') or '').strip()
    if not email or len(email) > 320 or '@' not in email:
        raise HTTPException(400, 'No verified email was provided. Link an existing account or use email signup.')
    lock_identity(db, flow['provider'], identity['subject'])
    # The identity may have been connected on another device since verification.
    if db.execute(text('SELECT 1 FROM user_identities WHERE provider=:p AND provider_subject=:s'), {'p': flow['provider'], 's': identity['subject']}).first():
        return complete(db, body.challenge, body.uiLanguage)
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:email,0))'), {'email': email.lower()})
    if db.execute(text('SELECT 1 FROM users WHERE lower(email)=lower(:email)'), {'email': email}).first():
        raise HTTPException(409, 'An account already uses this email. Link your existing ScoutWise account.')
    try:
        user_id = db.execute(text('''INSERT INTO users(email,password_hash,salt,dob,country,plan,favorites_json,created_at,language,newsletter,consent)
            VALUES(:email,NULL,NULL,:dob,:country,'Free','[]'::jsonb,NOW(),:lang,:newsletter,TRUE) RETURNING id'''), {
            'email': email, 'dob': body.dob, 'country': body.country.strip() or 'Unknown', 'lang': body.uiLanguage, 'newsletter': body.newsletter,
        }).scalar_one()
        attach(db, user_id, flow['provider'], identity)
        result = authenticated_session(db, user_id, body.uiLanguage)
        consume(db, body.challenge)
        db.commit()
        return {'status': 'authenticated', **result}
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, 'This account already exists. Please link or sign in again.') from exc


@router.get('/me/identities')
def identities(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    if not available(db):
        return {'providers': []}
    rows = db.execute(text('SELECT provider FROM user_identities WHERE user_id=:uid ORDER BY provider'), {'uid': user_id}).scalars().all()
    return {'providers': list(rows)}
