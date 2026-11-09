"""Shared public match snapshots and refresh leases for mobile and Enterprise.

Both independently deployed backends carry this module and use the same database
table, key format and provider account namespace. Never store user data here.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import logging
import os
from threading import Event, Lock, Thread
from time import monotonic, sleep
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api_module.database import engine

_READY = False
_SCHEMA_LOCK = Lock()
_KINDS = {'calendar', 'day', 'base', 'live', 'standings'}
_LOG = logging.getLogger(__name__)


def supported(key):
    return bool(key) and key[0] in _KINDS


def _identity(key):
    account = sha256(os.getenv('SPORTMONKS_API_KEY', '').encode()).hexdigest()
    raw = json.dumps(['match-snapshot-v1', account, key], separators=(',', ':'))
    return sha256(raw.encode()).hexdigest()


def _schema():
    global _READY
    if _READY:
        return
    with _SCHEMA_LOCK:
        if _READY:
            return
        with engine.begin() as db:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('shared_match_snapshots_schema_v1', 0))"))
            db.execute(text("""CREATE TABLE IF NOT EXISTS public.shared_match_snapshots (
                cache_key text PRIMARY KEY,
                payload jsonb,
                expires_at timestamptz,
                updated_at timestamptz,
                refresh_owner text,
                refresh_until timestamptz
            )"""))
            db.execute(text('ALTER TABLE public.shared_match_snapshots ENABLE ROW LEVEL SECURITY'))
        _READY = True


def read(key):
    """Return a saved payload and remaining freshness, even after expiry."""
    _schema()
    with engine.connect() as db:
        row = db.execute(text("""SELECT payload,
            EXTRACT(EPOCH FROM (expires_at - clock_timestamp())) AS remaining
            FROM public.shared_match_snapshots
            WHERE cache_key=:key AND payload IS NOT NULL"""),
            {'key': _identity(key)}).mappings().first()
    if row is None:
        return None
    payload = row['payload']
    if key[0] == 'base':
        # JSON object keys are strings; fixture merging requires integer IDs.
        payload = {int(fid): item for fid, item in payload.items()}
    return payload, float(row['remaining'] or 0)


def write(key, payload, seconds):
    _schema()
    with engine.begin() as db:
        db.execute(text("""INSERT INTO public.shared_match_snapshots
            (cache_key,payload,expires_at,updated_at)
            VALUES (:key,CAST(:payload AS jsonb),
                    clock_timestamp()+make_interval(secs => :seconds),clock_timestamp())
            ON CONFLICT (cache_key) DO UPDATE SET
                payload=EXCLUDED.payload,expires_at=EXCLUDED.expires_at,
                updated_at=EXCLUDED.updated_at"""),
            {'key': _identity(key), 'payload': json.dumps(payload, allow_nan=False),
             'seconds': float(seconds)})


def provider_cooldown():
    _schema()
    with engine.connect() as db:
        remaining = db.execute(text("""SELECT EXTRACT(EPOCH FROM
            (refresh_until-clock_timestamp())) FROM public.shared_match_snapshots
            WHERE cache_key=:key"""), {'key': _identity(('provider-cooldown',))}).scalar()
    return max(0, float(remaining or 0))


def defer_provider(seconds):
    _schema()
    with engine.begin() as db:
        db.execute(text("""INSERT INTO public.shared_match_snapshots (cache_key,refresh_until)
            VALUES (:key,clock_timestamp()+make_interval(secs => :seconds))
            ON CONFLICT (cache_key) DO UPDATE SET
                refresh_until=GREATEST(shared_match_snapshots.refresh_until,EXCLUDED.refresh_until)"""),
            {'key': _identity(('provider-cooldown',)), 'seconds': float(seconds)})


@contextmanager
def refresh_lock(key):
    """Coordinate refreshes without holding a DB connection during API calls.

    Expiring leases recover after a killed process. Waiting callers can use an
    already refreshed snapshot; older snapshots are served by the daily route
    before entering this context.
    """
    _schema()
    identity = _identity(key)
    owner = str(uuid4())
    deadline = monotonic() + 30
    acquired = False
    while True:
        with engine.begin() as db:
            acquired = db.execute(text("""INSERT INTO public.shared_match_snapshots
                (cache_key,refresh_owner,refresh_until)
                VALUES (:key,:owner,clock_timestamp()+INTERVAL '5 minutes')
                ON CONFLICT (cache_key) DO UPDATE SET
                    refresh_owner=EXCLUDED.refresh_owner,
                    refresh_until=EXCLUDED.refresh_until
                WHERE shared_match_snapshots.refresh_until IS NULL
                   OR shared_match_snapshots.refresh_until<=clock_timestamp()
                RETURNING cache_key"""), {'key': identity, 'owner': owner}).scalar() is not None
        if acquired:
            break
        snapshot = read(key)
        if snapshot is not None and snapshot[1] > 0:
            # The guarded loader rechecks freshness before doing provider work.
            break
        if monotonic() >= deadline:
            raise TimeoutError('Shared match refresh is already running')
        sleep(0.25)
    stop = Event()
    heartbeat = None
    if acquired:
        def renew():
            while not stop.wait(60):
                try:
                    with engine.begin() as db:
                        db.execute(text("""UPDATE public.shared_match_snapshots
                            SET refresh_until=clock_timestamp()+INTERVAL '5 minutes'
                            WHERE cache_key=:key AND refresh_owner=:owner"""),
                            {'key': identity, 'owner': owner})
                except SQLAlchemyError:
                    _LOG.warning('Shared match refresh lease renewal failed')
        heartbeat = Thread(target=renew, name='match-cache-lease', daemon=True)
        heartbeat.start()
    try:
        yield
    finally:
        stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=1)
        if acquired:
            with engine.begin() as db:
                db.execute(text("""UPDATE public.shared_match_snapshots
                    SET refresh_owner=NULL,refresh_until=NULL
                    WHERE cache_key=:key AND refresh_owner=:owner"""),
                    {'key': identity, 'owner': owner})

