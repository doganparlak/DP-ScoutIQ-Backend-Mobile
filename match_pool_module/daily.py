"""Date browsing with shared, bounded fixture and live-score caches."""
from collections import OrderedDict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as daytime, timedelta, timezone
from threading import Event, RLock, Thread
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import logging
import os

import requests
from sqlalchemy import text
from api_module import shared_match_cache as shared_cache
from sqlalchemy.exc import SQLAlchemyError

from .fixtures import SPORTMONKS_BASE_URL, SportMonksError, _fixture_out

_CACHE = OrderedDict()
_LOCK = RLock()
_FLIGHTS = {}
_REFRESHES = set()
_RETRY_AT = OrderedDict()
_REFRESH_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix='daily-refresh')
_WARM_STOP = Event()
_WARM_THREAD = None
_WARM_ZONES = OrderedDict()
_LOG = logging.getLogger(__name__)
_PROVIDER_RETRY_AT = 0
_INCLUDES = 'participants;league.country;season;state;scores;periods'
LIVE_REFRESH_SECONDS = 20

LIVE_STATES = {'INPLAY_1ST_HALF', 'INPLAY_2ND_HALF', 'HT', 'INPLAY_ET', 'ET', 'BREAK', 'PEN_LIVE', 'LIVE', '1ST', '2ND', 'INT', 'SUSP'}


def is_live(fixture):
    return str(fixture['state']['code']).upper() in LIVE_STATES


@contextmanager
def _singleflight(key):
    with _LOCK:
        entry = _FLIGHTS.setdefault(key, [RLock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            if shared_cache.supported(key):
                try:
                    with shared_cache.refresh_lock(key):
                        yield
                except TimeoutError:
                    raise SportMonksError('Match refresh is temporarily busy') from None
            else:
                yield
    finally:
        with _LOCK:
            entry[1] -= 1
            if not entry[1]:
                del _FLIGHTS[key]


def _remember(key, value, seconds):
    if shared_cache.supported(key):
        shared_cache.write(key, value, seconds)
    with _LOCK:
        _CACHE[key] = (monotonic() + seconds, value)
        _CACHE.move_to_end(key)
        while len(_CACHE) > 128:
            _CACHE.popitem(last=False)
    return value


def _cached(key):
    with _LOCK:
        item = _CACHE.get(key)
        if item and item[0] > monotonic():
            _CACHE.move_to_end(key)
            return item[1]
    if shared_cache.supported(key):
        saved = shared_cache.read(key)
        if saved is not None and saved[1] > 0:
            value, remaining = saved
            with _LOCK:
                _CACHE[key] = (monotonic() + remaining, value)
                _CACHE.move_to_end(key)
                while len(_CACHE) > 128:
                    _CACHE.popitem(last=False)
            return value
    return None


def _previous(key):
    if shared_cache.supported(key):
        saved = shared_cache.read(key)
        if saved is not None:
            return saved[0]
    with _LOCK:
        entry = _CACHE.get(key)
        return entry[1] if entry else None


def _queue_refresh(key, refresh):
    with _LOCK:
        if key in _REFRESHES or max(_RETRY_AT.get(key, 0), _PROVIDER_RETRY_AT) > monotonic() or len(_REFRESHES) >= 32:
            return False
        _REFRESHES.add(key)

    def run():
        try:
            refresh()
            with _LOCK:
                _RETRY_AT.pop(key, None)
        except Exception:
            # Never log provider URLs or credentials on connection errors.
            _LOG.warning('Daily fixture background refresh failed; retaining cached data')
            with _LOCK:
                _RETRY_AT[key] = max(monotonic() + 30, _PROVIDER_RETRY_AT)
                while len(_RETRY_AT) > 128:
                    _RETRY_AT.popitem(last=False)
        finally:
            with _LOCK:
                _REFRESHES.discard(key)

    _REFRESH_POOL.submit(run)
    return True


def _covered_leagues(db):
    key = ('covered-leagues',)
    with _singleflight(key):
        cached = _cached(key)
        if cached is None:
            cached = tuple(db.execute(text('SELECT DISTINCT league_id FROM player_comp_data WHERE league_id IS NOT NULL ORDER BY league_id')).scalars())
            _remember(key, cached, 900)
        return cached


def _request_json(path, params):
    global _PROVIDER_RETRY_AT
    with _LOCK:
        remaining = _PROVIDER_RETRY_AT - monotonic()
    if remaining > 0:
        raise SportMonksError('Fixture data provider rate limit reached; waiting before retrying')
    remaining = shared_cache.provider_cooldown()
    if remaining > 0:
        with _LOCK:
            _PROVIDER_RETRY_AT = max(_PROVIDER_RETRY_AT, monotonic() + remaining)
        raise SportMonksError('Fixture data provider rate limit reached; waiting before retrying')
    try:
        response = requests.get(f'{SPORTMONKS_BASE_URL}/{path}', params=params, timeout=25)
        if response.status_code == 429:
            try:
                delay = max(1, float(response.headers.get('Retry-After', '60')))
            except (TypeError, ValueError):
                delay = 60
            with _LOCK:
                _PROVIDER_RETRY_AT = max(_PROVIDER_RETRY_AT, monotonic() + delay)
            shared_cache.defer_provider(delay)
            raise SportMonksError('Fixture data provider rate limit reached; waiting before retrying')
        if response.status_code != 200:
            raise SportMonksError(f'Fixture data request failed (HTTP {response.status_code})')
        return response.json()
    except (requests.RequestException, ValueError):
        raise SportMonksError('Fixture data service is temporarily unavailable') from None


def _pages(path, leagues, timezone_name=None):
    # Never download the unrestricted feed when coverage is empty.
    if not leagues:
        return []
    token = os.getenv('SPORTMONKS_API_KEY')
    if not token:
        raise SportMonksError('Fixture data service is not configured')

    covered = set(leagues)

    def fetch(ids=None):
        rows = []
        for page in range(1, 101):
            payload = _request_json(path, {
                'api_token': token, 'include': _INCLUDES,
                'per_page': 100, 'page': page,
                **({'filters': 'fixtureLeagues:' + ','.join(map(str, ids))} if ids else {}),
                **({'timezone': timezone_name} if timezone_name else {}),
            })
            # Keep only compact covered fixtures between pages. Raw provider
            # payloads include nested teams, scores and periods and otherwise
            # accumulate across every page and concurrent league batch.
            for raw in payload.get('data') or []:
                if (raw.get('league') or {}).get('id') not in covered:
                    continue
                item = _compact(raw, timezone_name or 'UTC')
                if item is not None:
                    rows.append(item)
            has_more = (payload.get('pagination') or {}).get('has_more')
            del payload
            if not has_more:
                return rows
        raise SportMonksError('Fixture pagination exceeded the supported range')

    # Date feeds can contain far more subscribed leagues than we expose.
    # Sportmonks accepts at most 50 league IDs per filter, so fetch bounded
    # batches concurrently instead of downloading and discarding the whole feed.
    if path.startswith('fixtures/date/') and leagues:
        chunks = [leagues[index:index + 50] for index in range(0, len(leagues), 50)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            rows = [row for batch in pool.map(fetch, chunks) for row in batch]
        return list({row['fixtureId']: row for row in rows}.values())
    return fetch()


def _compact(raw, timezone_name='UTC'):
    item = _fixture_out(raw)
    if not item or not item.get('startingAt'):
        return None
    # Date queries return local starting_at strings; always expose UTC to the app.
    stamp = raw.get('starting_at_timestamp')
    if isinstance(stamp, (int, float)):
        item['startingAt'] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    else:
        start = datetime.fromisoformat(item['startingAt'].replace('Z', '+00:00'))
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZoneInfo(timezone_name))
        item['startingAt'] = start.astimezone(timezone.utc).isoformat()
    periods = [p for p in raw.get('periods') or [] if p.get('started') and not p.get('ended')]
    active = max(periods, key=lambda p: p.get('sort_order') or 0, default={})
    item['minute'] = active.get('minutes')
    return item


def _kickoff(item):
    value = datetime.fromisoformat(item['startingAt'].replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Invalid timezone') from None


def _refresh_calendar(timezone_name, leagues):
    zone = _zone(timezone_name)
    key = ('calendar', timezone_name, leagues)
    with _singleflight(key):
        cached = _cached(key)
        if cached is not None:
            return cached
        token = os.getenv('SPORTMONKS_API_KEY')
        if not token:
            raise SportMonksError('Fixture data service is not configured')
        jobs = [(leagues[index:index + 50], order) for index in range(0, len(leagues), 50) for order in ('asc', 'desc')]

        def extreme(job):
            ids, order = job
            # Sorting is by actual kickoff, not fixture ID or season dates.
            for page in range(1, 101):
                payload = _request_json('fixtures', {
                    'api_token': token, 'filters': 'fixtureLeagues:' + ','.join(map(str, ids)),
                    'sortBy': 'starting_at', 'order': order, 'per_page': 25, 'page': page,
                })
                for fixture in payload.get('data') or []:
                    if fixture.get('starting_at') and fixture.get('league_id') in ids:
                        return _kickoff({'startingAt': fixture['starting_at']})
                if not (payload.get('pagination') or {}).get('has_more'):
                    return None
            raise SportMonksError('Calendar pagination exceeded the supported range')

        with ThreadPoolExecutor(max_workers=3) as pool:
            times = [value for value in pool.map(extreme, jobs) if value is not None]
        today = datetime.now(zone).date().isoformat()
        result = {'minDate': min(times).astimezone(zone).date().isoformat() if times else today,
                  'maxDate': max(times).astimezone(zone).date().isoformat() if times else today}
        return _remember(key, result, 21600)



def calendar_bounds(db, timezone_name, leagues=None):
    _zone(timezone_name)
    leagues = _covered_leagues(db) if leagues is None else leagues
    key = ('calendar', timezone_name, leagues)
    cached = _cached(key)
    if cached is not None:
        return cached
    previous = _previous(key)
    if previous is not None:
        _queue_refresh(key, lambda: _refresh_calendar(timezone_name, leagues))
        return previous
    return _refresh_calendar(timezone_name, leagues)


def _forecast_capabilities(result):
    from .forecast import load_saved_model, forecast_coverage_reason
    now = datetime.now(timezone.utc)
    model_key = ('forecast-model',)
    with _singleflight(model_key):
        cached = _cached(model_key)
        if cached is None:
            try:
                model = load_saved_model(now)
            except SQLAlchemyError:
                model = None
            cached = _remember(model_key, {'model': model}, 30)
    model = cached['model']
    fixtures = []
    for fixture in result['fixtures']:
        kickoff = _kickoff(fixture)
        # A postponed fixture with a past kickoff is not forecastable yet.
        upcoming = str(fixture['state']['code']).upper() in {'NS', 'TBA', 'POST', 'POSTP', 'DELAYED'} and kickoff > now
        available = upcoming and forecast_coverage_reason(fixture, now, model) is None
        fixtures.append({**fixture, 'forecastAvailable': bool(available)})
    return {**result, 'fixtures': fixtures}


def _refresh_day(selected: date, timezone_name: str, leagues):
    zone = _zone(timezone_name)
    today = datetime.now(zone).date()
    key = ('day', selected.isoformat(), timezone_name, leagues)
    # Share cold refreshes of the same date without blocking other dates.
    with _singleflight(key):
        cached = _cached(key)
        if cached is not None:
            return _forecast_capabilities(cached)
        base_key = ('base', selected.isoformat(), timezone_name, leagues)
        fixtures = _cached(base_key)
        if fixtures is None:
            first = datetime.combine(selected, daytime.min, zone).astimezone(timezone.utc)
            last = datetime.combine(selected + timedelta(days=1), daytime.min, zone).astimezone(timezone.utc)
            rows = _pages(f'fixtures/date/{selected.isoformat()}', leagues, timezone_name)
            fixtures = {item['fixtureId']: item for item in rows
                        if item['league']['id'] in leagues and first <= _kickoff(item) < last}
            _remember(base_key, fixtures, 300 if abs((selected - today).days) <= 1 else 1800)
        fixtures = dict(fixtures)
        if selected == today or any(is_live(item) for item in fixtures.values()):
            live_key = ('live', leagues)
            with _singleflight(live_key):
                live = _cached(live_key)
                if live is None:
                    live = _pages('livescores', leagues)
                    _remember(live_key, live, LIVE_REFRESH_SECONDS)
            # This feed also includes games finished in the last 15 minutes,
            # so final scores replace in-play rows rather than disappearing.
            for item in live:
                if item['league']['id'] in leagues and _kickoff(item).astimezone(zone).date() == selected:
                    fixtures[item['fixtureId']] = item
        ordered = sorted(fixtures.values(), key=lambda item: (_kickoff(item), item['fixtureId']))
        has_live = any(is_live(item) for item in ordered)
        seconds = LIVE_REFRESH_SECONDS if has_live else 300
        result = {'date': selected.isoformat(), 'fixtures': ordered,
                  'refreshAfter': seconds if selected == today or has_live else None, 'stale': False}
        _remember(key, result, seconds if abs((selected - today).days) <= 1 else 1800)
        return _forecast_capabilities(result)



def daily_fixtures(db, selected: date, timezone_name: str):
    zone = _zone(timezone_name)
    today = datetime.now(zone).date()
    with _LOCK:
        _WARM_ZONES[timezone_name] = monotonic()
        _WARM_ZONES.move_to_end(timezone_name)
        while len(_WARM_ZONES) > 8:
            _WARM_ZONES.popitem(last=False)
    leagues = _covered_leagues(db)
    if not leagues:
        return {'date': selected.isoformat(), 'fixtures': [], 'refreshAfter': 300 if selected == today else None, 'stale': False}
    key = ('day', selected.isoformat(), timezone_name, leagues)
    cached = _cached(key)
    if cached is not None:
        return _forecast_capabilities(cached)
    previous = _previous(key)
    if previous is not None:
        _queue_refresh(key, lambda: _refresh_day(selected, timezone_name, leagues))
        # Serve the persisted snapshot even if the calendar/provider is down.
        # Provider failures stay internal; clients keep polling silently.
        return _forecast_capabilities({**previous, 'refreshAfter': LIVE_REFRESH_SECONDS, 'stale': False})
    bounds = calendar_bounds(db, timezone_name, leagues)
    if not bounds['minDate'] <= selected.isoformat() <= bounds['maxDate']:
        raise ValueError('Choose a date within the available fixture calendar')
    return _refresh_day(selected, timezone_name, leagues)


def start_daily_warmer():
    """Warm today's public fixtures without blocking server startup or requests."""
    global _WARM_THREAD
    if _WARM_THREAD and _WARM_THREAD.is_alive():
        return
    _WARM_STOP.clear()

    def warm():
        from api_module.database import SessionLocal
        while not _WARM_STOP.is_set():
            try:
                with SessionLocal() as db:
                    leagues = _covered_leagues(db)
                with _LOCK:
                    zones = ['Europe/Istanbul', *[name for name, seen in _WARM_ZONES.items()
                             if name != 'Europe/Istanbul' and monotonic() - seen < 86400]]
                for name in zones:
                    selected = datetime.now(_zone(name)).date()
                    calendar_key = ('calendar', name, leagues)
                    if _cached(calendar_key) is None:
                        _queue_refresh(calendar_key, lambda name=name: _refresh_calendar(name, leagues))
                    key = ('day', selected.isoformat(), name, leagues)
                    # Keep the shared 8/300-second provider cadence; callers
                    # can still read the old snapshot while this job refreshes.
                    with _LOCK:
                        entry = _CACHE.get(key)
                        due = entry is None or entry[0] <= monotonic()
                    if leagues and due:
                        _queue_refresh(key, lambda selected=selected, name=name: _refresh_day(selected, name, leagues))
            except Exception:
                _LOG.warning('Daily fixture cache warm-up failed; retrying later')
            _WARM_STOP.wait(2)

    _WARM_THREAD = Thread(target=warm, name='daily-warmer', daemon=True)
    _WARM_THREAD.start()


def stop_daily_warmer():
    _WARM_STOP.set()
    if _WARM_THREAD:
        _WARM_THREAD.join(timeout=1)
