"""One-time password-reset proof; supports JSON tokens and legacy native cookies."""
import hashlib
import hmac
import re
import secrets
from fastapi import HTTPException
from sqlalchemy import text

COOKIE_NAME = 'scoutwise_password_reset'
COOKIE_PATH = '/auth/set_new_password'


def ready(db):
    return bool(db.execute(text("""SELECT EXISTS(SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='email_codes' AND column_name='reset_proof_hash')""")).scalar())


def verify_code(db, email, code):
    if not ready(db):
        raise HTTPException(503, 'Password recovery is temporarily unavailable. Please try again later.')
    row = db.execute(text("""SELECT id,code,reset_attempts FROM email_codes
        WHERE lower(email)=lower(:email) AND purpose='reset' AND used=FALSE
          AND created_at>NOW()-INTERVAL '10 minutes'
        ORDER BY id DESC LIMIT 1 FOR UPDATE"""), {'email': email}).mappings().first()
    if not row or row['reset_attempts'] >= 5:
        raise HTTPException(400, 'Invalid or expired code')
    db.execute(text('UPDATE email_codes SET reset_attempts=reset_attempts+1 WHERE id=:id'), {'id': row['id']})
    if not re.fullmatch(r'\d{6}', code) or not hmac.compare_digest(str(row['code']), code):
        db.commit()
        raise HTTPException(400, 'Invalid or expired code')
    proof = secrets.token_urlsafe(48)
    db.execute(text('UPDATE email_codes SET used=TRUE,reset_proof_hash=:proof WHERE id=:id'), {
        'id': row['id'], 'proof': hashlib.sha256(proof.encode()).hexdigest(),
    })
    db.commit()
    return proof


def require_proof(db, email, proof):
    if not ready(db) or not proof or not 40 <= len(proof) <= 128:
        raise HTTPException(400, 'Please verify your reset code again.')
    row = db.execute(text("""SELECT id FROM email_codes WHERE lower(email)=lower(:email)
        AND purpose='reset' AND used=TRUE AND reset_proof_hash=:proof
        AND created_at>NOW()-INTERVAL '10 minutes' ORDER BY id DESC LIMIT 1 FOR UPDATE"""), {
        'email': email, 'proof': hashlib.sha256(proof.encode()).hexdigest(),
    }).mappings().first()
    if not row:
        raise HTTPException(400, 'Please verify your reset code again.')
    return row['id']
