"""One session/entitlement path for password and federated sign-in."""
import uuid
from sqlalchemy import text
from fastapi import HTTPException
from api_module.utilities import normalize_lang, now_iso, user_row_to_dict

from api_module.subscription_access import reconcile_subscription


def authenticated_session(db, user_id, language=None, *, allow_email_restore=False):
    user = reconcile_subscription(db, user_id, allow_email_restore=allow_email_restore)
    if not user:
        raise HTTPException(401, 'Account unavailable')
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
