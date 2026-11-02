"""Daily, shared input refresh without refitting forecast coefficients.

Immutable dated snapshots preserve pre-kickoff review cutoffs. Provider work is
background-only; report requests read cached snapshots and never fetch histories.
"""
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import copy
import logging
from threading import Event, Lock, Thread

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

LOG = logging.getLogger(__name__)
_LOCK = Lock()
_READY = False
_CACHE = OrderedDict()
_STOP = Event()
_THREAD = None


def _schema():
    from .forecast import engine, _schema as model_schema
    global _READY
    if _READY:
        return
    model_schema()
    with _LOCK:
        if _READY:
            return
        with engine.begin() as db:
            db.execute(text('''CREATE TABLE IF NOT EXISTS mobile_match_forecast_histories (
                model_id bigint NOT NULL REFERENCES mobile_match_forecast_models(id) ON DELETE CASCADE,
                as_of timestamptz NOT NULL, data_cutoff timestamptz NOT NULL, payload jsonb NOT NULL,
                PRIMARY KEY(model_id,as_of))'''))
            db.execute(text('ALTER TABLE mobile_match_forecast_histories ENABLE ROW LEVEL SECURITY'))
        _READY = True


def daily_boundary(now):
    """Refresh at 04:00 UTC, after most previous-day fixtures have settled."""
    boundary = now.astimezone(timezone.utc).replace(hour=4, minute=0, second=0, microsecond=0)
    return boundary if now >= boundary else boundary - timedelta(days=1)


def apply_matches(model, matches, as_of):
    """Pure transformation also used by chronological backtests."""
    from . import forecast as f
    from .forecast_learning import append_match
    from .forecast_refined import restore_state, pack_state
    refined = model.get('refinedEnsemble')
    refined_state = restore_state(refined['state']) if refined else None
    histories = defaultdict(list, {int(team): list(rows) for team, rows in model['histories'].items()})
    totals = {int(k): list(v) for k, v in model['leagueTotals'].items()}
    ratings = {int(k): v for k, v in model['ratings'].items()}
    half = model.get('halfTimeModel')
    half_totals = {int(k): list(v) for k, v in (half or {}).get('leagueTotals', {}).items()}
    seen = {row['id'] for rows in histories.values() for row in rows}
    if refined_state is not None: seen.update(refined_state.seen)
    offsets = copy.deepcopy(model.get('leagueOffsets'))
    added = 0
    for match in sorted(matches, key=lambda m: (f.utc(m['at']), m['id'])):
        if match['id'] in seen or f.utc(match['at']) + timedelta(hours=3) > as_of:
            continue
        if offsets:
            from .forecast_league_offsets import observe_match
            observe_match(offsets, match, ratings)
        if model.get('featureSchema') == 3:
            append_match(histories, match, ratings)
        else:
            f._append(histories, match)
        f._update_context(match, totals, ratings)
        f._update_half_totals(match, half_totals)
        if refined_state is not None:
            refined_state.consume(match)
        seen.add(match['id']); added += 1
    selected = {name.split(':', 1)[-1] for name in model['features'] if ':' in name}
    compact = {str(team): [{**row, **{side: {key: value for key, value in row[side].items() if key in selected}
               for side in ('own', 'opponent')}} for row in sorted(rows, key=lambda r: (f.utc(r['at']), r['id']))[-f.WINDOW:]] for team, rows in histories.items()}
    if offsets:
        from .forecast_league_offsets import refresh_offsets
        offsets = refresh_offsets(offsets, as_of)
    offset_update = {'leagueOffsets': offsets} if offsets else {}
    refined_update = {'refinedEnsemble': {**refined, 'state': pack_state(refined_state)}} if refined else {}
    return {**model, **refined_update, **offset_update, 'histories': compact, 'leagueTotals': {str(k): v for k, v in totals.items()},
            'ratings': {str(k): v for k, v in ratings.items()},
            'halfTimeModel': {**half, 'leagueTotals': {str(k): v for k, v in half_totals.items()}} if half else None,
            'historyAsOf': as_of.isoformat(), 'historyMatchesAdded': added}


def attach_snapshot(model, cutoff):
    from .forecast import engine
    if not model or not model.get('modelId'):
        return model
    try:
        with engine.connect() as db:
            stamp = db.execute(text('''SELECT as_of FROM mobile_match_forecast_histories
                WHERE model_id=:id AND as_of<:cutoff ORDER BY as_of DESC LIMIT 1'''),
                {'id': model['modelId'], 'cutoff': cutoff}).scalar()
            if stamp is None:
                return model
            key = (model['modelId'], stamp)
            with _LOCK:
                payload = _CACHE.get(key)
            if payload is None:
                payload = db.execute(text('''SELECT payload FROM mobile_match_forecast_histories
                    WHERE model_id=:id AND as_of=:stamp'''), {'id': model['modelId'], 'stamp': stamp}).scalar_one()
                with _LOCK:
                    _CACHE[key] = payload
                    _CACHE.move_to_end(key)
                    while len(_CACHE) > 4:
                        _CACHE.popitem(last=False)
            return {**model, **payload}
    except SQLAlchemyError:
        # Initial startup or unavailable snapshot storage keeps the trained
        # artifact usable. No provider work or fitting occurs on this path.
        return model


def refresh_active_history(now=None):
    from . import forecast as f
    _schema()
    now = now or datetime.now(timezone.utc)
    with f.engine.begin() as owner:
        locked = owner.execute(text("SELECT pg_try_advisory_xact_lock(hashtextextended('daily_forecast_history',0))")).scalar()
        if not locked:
            return {'status': 'busy'}
        model_row = owner.execute(text('SELECT id,model_version FROM mobile_match_forecast_models WHERE is_active')).mappings().first()
        if not model_row:
            return {'status': 'no_model'}
        if model_row['model_version'] not in (2, 3, 4, f.VERSION):
            return {'status': 'unsupported_model'}
        latest = owner.execute(text('SELECT MAX(as_of) FROM mobile_match_forecast_histories WHERE model_id=:id'), {'id': model_row['id']}).scalar()
        if latest and latest >= daily_boundary(now):
            # Poll only small metadata. Never download the full artifact every
            # five minutes; warm a new process once, then reuse parsed objects.
            with f._MODEL_LOCK:
                artifact_cached = model_row['id'] in f._MODEL_ARTIFACTS
            with _LOCK:
                snapshot_cached = (model_row['id'], latest) in _CACHE
            if not artifact_cached or not snapshot_cached:
                f.load_saved_model(now)
            return {'status': 'fresh', 'modelId': model_row['id']}
        model = f.load_saved_model(now)
        if not model or model['modelId'] != model_row['id']:
            return {'status': 'model_changed'}
        since = f.utc(model.get('historyInputCutoff') or model.get('dataCutoff') or model['asOf'])
        if not model.get('historyInputCutoff') and not model.get('dataCutoff'):
            since -= timedelta(hours=3)  # Legacy artifacts may include unfinished kickoff windows.
        data_cutoff = now - timedelta(hours=3)
        def collect(competition):
            return f._history(competition['leagueId'], data_cutoff,
                {'id': competition['seasonId'], 'starting_at': competition['startsAt']}, since=since,
                include_events=bool(model.get('refinedEnsemble')))
        with ThreadPoolExecutor(max_workers=3) as pool:
            matches = list({m['id']: m for rows in pool.map(collect, model['competitions']) for m in rows}.values())
        matches = [m for m in matches if f.utc(m['at']) >= since]
        as_of = datetime.now(timezone.utc)
        refreshed = apply_matches(model, matches, now)
        fields = ('histories', 'leagueTotals', 'ratings', 'halfTimeModel', 'historyMatchesAdded')
        if refreshed.get('refinedEnsemble'):
            fields += ('refinedEnsemble',)
        if refreshed.get('leagueOffsets'):
            fields += ('leagueOffsets',)
        payload = {**{key: refreshed[key] for key in fields}, 'historyAsOf': as_of.isoformat(), 'historyInputCutoff': data_cutoff.isoformat()}
        owner.execute(text('''INSERT INTO mobile_match_forecast_histories(model_id,as_of,data_cutoff,payload)
            VALUES(:id,:stamp,:cutoff,CAST(:payload AS jsonb))'''),
            {'id': model['modelId'], 'stamp': as_of, 'cutoff': data_cutoff, 'payload': json.dumps(payload, allow_nan=False)})
        with _LOCK:
            _CACHE[(model['modelId'], as_of)] = payload
            _CACHE.move_to_end((model['modelId'], as_of))
            while len(_CACHE) > 4:
                _CACHE.popitem(last=False)
        # Forecast snapshots and purchased forecasts remain intact; only future
        # shared responses are refreshed using the new inputs.
        owner.execute(text("""DELETE FROM mobile_match_forecast_cache WHERE cache_key LIKE 'fixture:%'
            AND (payload->'forecast'->>'startingAt')::timestamptz>NOW()"""))
        return {'status': 'refreshed', 'modelId': model['modelId'], 'matchesAdded': refreshed['historyMatchesAdded'], 'asOf': as_of.isoformat()}


def start_history_refresh():
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        def work():
            while not _STOP.is_set():
                try:
                    refresh_active_history()
                except Exception as exc:
                    LOG.warning('Forecast history refresh failed (%s); keeping prior inputs', type(exc).__name__)
                _STOP.wait(300)
        _THREAD = Thread(target=work, name='forecast-history-refresh', daemon=True)
        _THREAD.start()


def stop_history_refresh():
    _STOP.set()
    if _THREAD is not None:
        _THREAD.join(timeout=1.)
