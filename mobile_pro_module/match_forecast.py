"""Atomic, per-user match forecast unlocks using the existing Pro credit pool."""
import json
from datetime import datetime, timezone
from threading import Lock

from fastapi import HTTPException
from sqlalchemy import text

from api_module.database import engine
from match_pool_module.forecast import get_forecast, utc

_READY = False
_LOCK = Lock()


def _schema():
    global _READY
    if _READY:
        return
    with _LOCK:
        if _READY:
            return
        with engine.begin() as db:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('pro_forecast_unlock_schema',0))"))
            db.execute(text('''CREATE TABLE IF NOT EXISTS mobile_pro_forecast_unlocks (
                user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                fixture_id bigint NOT NULL, payload jsonb NOT NULL,
                credits_spent integer NOT NULL CHECK(credits_spent IN (0,1)),
                created_at timestamptz NOT NULL DEFAULT NOW(),
                PRIMARY KEY(user_id,fixture_id))'''))
            db.execute(text('ALTER TABLE mobile_pro_forecast_unlocks ENABLE ROW LEVEL SECURITY'))
        _READY = True


def _account(db, user_id, lock=False):
    row = db.execute(text('''SELECT plan,free_chat_messages_remaining,
        COALESCE(plan IN ('Pro Monthly','Pro Yearly') AND subscription_end_at>NOW(),FALSE) AS unlimited
        FROM users WHERE id=:uid''' + (' FOR UPDATE' if lock else '')), {'uid': user_id}).mappings().first()
    if not row:
        raise HTTPException(401, 'User not found')
    return row


def _opened(db, user_id, fixture_id):
    return db.execute(text('''SELECT payload FROM mobile_pro_forecast_unlocks
        WHERE user_id=:uid AND fixture_id=:fid'''), {'uid': user_id, 'fid': fixture_id}).scalar()


def _public(result, account, unlocked=False):
    return {**result, 'unlocked': unlocked, 'creditCost': 1,
            'accessTier': 'paid' if unlocked else 'free',
            'freeChatMessagesRemaining': None if account['unlimited'] else account['free_chat_messages_remaining']}


def forecast_access(db, user_id, fixture_id, unlock=False):
    _schema()
    account = _account(db, user_id)
    saved = _opened(db, user_id, fixture_id)
    if saved:
        return _public(saved, account, True)
    if not unlock:
        return _public({'status': 'locked'}, account)
    if not account['unlimited'] and (account['plan'] not in ('Free', 'No Ads Monthly') or account['free_chat_messages_remaining'] <= 0):
        raise HTTPException(403, 'CHAT_TRIAL_EXHAUSTED')
    # All provider work happens before the short credit transaction. Failed,
    # unavailable and still-processing forecasts never consume a credit.
    result = get_forecast(fixture_id)
    if result['status'] != 'ready':
        return _public(result, account)
    if utc(result['forecast']['startingAt']) <= datetime.now(timezone.utc):
        raise HTTPException(409, 'FORECAST_MATCH_STARTED')
    try:
        # Serialize against every other action spending the same user's credits.
        account = _account(db, user_id, lock=True)
        saved = _opened(db, user_id, fixture_id)
        if saved:
            db.rollback()
            return _public(saved, account, True)
        if not account['unlimited'] and (account['plan'] not in ('Free', 'No Ads Monthly') or account['free_chat_messages_remaining'] <= 0):
            raise HTTPException(403, 'CHAT_TRIAL_EXHAUSTED')
        # A forecast becoming ready exactly at kickoff cannot be purchased.
        if utc(result['forecast']['startingAt']) <= datetime.now(timezone.utc):
            raise HTTPException(409, 'FORECAST_MATCH_STARTED')
        spent = 0 if account['unlimited'] else 1
        db.execute(text('''INSERT INTO mobile_pro_forecast_unlocks(user_id,fixture_id,payload,credits_spent)
            VALUES(:uid,:fid,CAST(:payload AS jsonb),:spent)'''),
            {'uid': user_id, 'fid': fixture_id, 'payload': json.dumps(result, allow_nan=False), 'spent': spent})
        if spent:
            db.execute(text('UPDATE users SET free_chat_messages_remaining=free_chat_messages_remaining-1 WHERE id=:uid'), {'uid': user_id})
        db.commit()
        return _public(result, {**account, 'free_chat_messages_remaining': account['free_chat_messages_remaining'] - spent}, True)
    except BaseException:
        db.rollback()
        raise
