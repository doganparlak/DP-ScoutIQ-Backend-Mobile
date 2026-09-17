"""One-time chat allowance, with reservations and replay-safe responses."""
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy import text


def reserve_message(db, user_id, request_id, message, session_id, strategy):
    fingerprint = hashlib.sha256(json.dumps(
        [message, session_id, strategy], ensure_ascii=False
    ).encode()).hexdigest()
    # Serialize this user's reservation/replay decisions, not the AI generation.
    user = db.execute(text("""
        SELECT plan, free_chat_messages_remaining FROM users
        WHERE id = :uid FOR UPDATE
    """), {"uid": user_id}).mappings().first()
    if not user:
        db.rollback()
        raise HTTPException(401, "User not found")
    existing = db.execute(text("""
        SELECT request_hash, response_json FROM free_chat_requests
        WHERE user_id = :uid AND request_id = :rid
    """), {"uid": user_id, "rid": request_id}).mappings().first()
    if existing:
        db.rollback()
        if existing['request_hash'] != fingerprint:
            raise HTTPException(409, "Chat request ID was reused for a different message")
        if existing['response_json'] is None:
            raise HTTPException(409, "CHAT_REQUEST_PENDING")
        response = existing['response_json']
        if isinstance(response, str):
            response = json.loads(response)
        return {**response, 'freeChatMessagesRemaining': user['free_chat_messages_remaining']}
    if user['plan'] not in ('Free', 'No Ads Monthly') or user['free_chat_messages_remaining'] <= 0:
        db.rollback()
        raise HTTPException(403, "CHAT_TRIAL_EXHAUSTED")
    db.execute(text("""
        UPDATE users SET free_chat_messages_remaining = free_chat_messages_remaining - 1
        WHERE id = :uid
    """), {"uid": user_id})
    db.execute(text("""
        INSERT INTO free_chat_requests(user_id, request_id, request_hash)
        VALUES (:uid, :rid, :hash)
    """), {"uid": user_id, "rid": request_id, "hash": fingerprint})
    db.commit()
    return None


def finish_message(db, user_id, request_id, response):
    remaining = db.execute(text("SELECT free_chat_messages_remaining FROM users WHERE id = :uid"),
                           {"uid": user_id}).scalar_one()
    response = {**response, 'freeChatMessagesRemaining': remaining}
    db.execute(text("""
        UPDATE free_chat_requests SET response_json = CAST(:response AS jsonb)
        WHERE user_id = :uid AND request_id = :rid
    """), {"uid": user_id, "rid": request_id, "response": json.dumps(response)})
    db.commit()
    return response


def refund_message(db, user_id, request_id):
    db.rollback()
    removed = db.execute(text("""
        DELETE FROM free_chat_requests
        WHERE user_id = :uid AND request_id = :rid AND response_json IS NULL
        RETURNING user_id
    """), {"uid": user_id, "rid": request_id}).first()
    if removed:
        db.execute(text("""
            UPDATE users SET free_chat_messages_remaining = LEAST(5, free_chat_messages_remaining + 1)
            WHERE id = :uid
        """), {"uid": user_id})
    db.commit()
