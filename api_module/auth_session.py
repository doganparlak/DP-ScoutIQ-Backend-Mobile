"""One session/entitlement path for password and federated sign-in."""
import uuid
from sqlalchemy import text
from fastapi import HTTPException
from api_module.utilities import normalize_lang, now_iso, plan_from_product_id, user_row_to_dict


def authenticated_session(db, user_id, language=None, *, allow_email_restore=False):
    user = db.execute(text('SELECT * FROM users WHERE id=:uid FOR UPDATE'), {'uid': user_id}).mappings().first()
    if not user:
        raise HTTPException(401, 'Account unavailable')
    # An entitlement owned by another user must never be restored by matching email.
    entitlement = db.execute(text('''
        SELECT * FROM subscription_entitlements
        WHERE expires_at > NOW() AND (
            last_seen_user_id=:uid OR
            (:email_restore AND last_seen_user_id IS NULL AND lower(last_seen_email)=lower(:email))
        ) ORDER BY (last_seen_user_id=:uid) DESC NULLS LAST, expires_at DESC LIMIT 1
        FOR UPDATE
    '''), {'uid': user_id, 'email': user['email'], 'email_restore': allow_email_restore}).mappings().first()
    if entitlement:
        db.execute(text('''UPDATE users SET plan=:plan, subscription_end_at=:expiry,
            subscription_auto_renew=:renew, subscription_platform=:platform,
            subscription_external_id=:external WHERE id=:uid'''), {
            'uid': user_id, 'plan': plan_from_product_id(entitlement['product_id']),
            'expiry': entitlement['expires_at'], 'renew': entitlement['auto_renew'],
            'platform': entitlement['platform'], 'external': entitlement['external_id'],
        })
        db.execute(text('''UPDATE subscription_entitlements SET last_seen_user_id=:uid, updated_at=NOW()
            WHERE platform=:platform AND external_id=:external AND last_seen_user_id IS NULL'''), {
            'uid': user_id, 'platform': entitlement['platform'], 'external': entitlement['external_id'],
        })
    preferred = normalize_lang(language)
    if preferred:
        db.execute(text('UPDATE users SET language=:lang WHERE id=:uid'), {'uid': user_id, 'lang': preferred})
    user = db.execute(text('SELECT * FROM users WHERE id=:uid'), {'uid': user_id}).mappings().one()
    token = uuid.uuid4().hex
    db.execute(text('''INSERT INTO sessions(token,user_id,language,created_at,ended_at)
        VALUES(:token,:uid,:lang,:created,NULL)'''), {
        'token': token, 'uid': user_id, 'lang': normalize_lang(user.get('language')) or 'en', 'created': now_iso(),
    })
    # Caller commits the identity operation and session atomically.
    return {'token': token, 'user': user_row_to_dict(user)}
