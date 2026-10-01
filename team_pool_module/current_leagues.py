"""Current domestic membership for Team Pool, separate from historical stats."""
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
import os
import threading
import time
import unicodedata

import requests
from sqlalchemy import text

CACHE_LIMIT = 512
CACHE_TTL = 6 * 60 * 60
EMPTY_TTL = 60
_cache = OrderedDict()
_cache_lock = threading.Lock()
_request_locks = [threading.Lock() for _ in range(16)]


def entity_id(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return int(number) if number.is_finite() and number == number.to_integral_value() and 0 < number <= 9223372036854775807 else None
    except (ValueError, InvalidOperation):
        return None


def fold(value):
    value = str(value or '').strip().casefold().translate(str.maketrans({'ı': 'i', 'ß': 'ss'}))
    return ''.join(c for c in unicodedata.normalize('NFD', value) if not unicodedata.combining(c))


def cached_league(team_id):
    with _cache_lock:
        item = _cache.get(team_id)
        if item and item[0] > time.monotonic():
            _cache.move_to_end(team_id)
            return True, item[1]
        _cache.pop(team_id, None)
        return False, None


def fetch_current_league(team_id):
    # Fixed striped locks prevent duplicate lookups without an unbounded lock map.
    with _request_locks[team_id % len(_request_locks)]:
        hit, value = cached_league(team_id)
        if hit:
            return value
        token = os.getenv('SPORTMONKS_API_KEY')
        if not token:
            raise RuntimeError('ScoutWise data service is not configured')
        base = os.getenv('SPORTMONKS_BASE_URL', 'https://api.sportmonks.com/v3/football').rstrip('/')
        leagues = {}
        for page in range(1, 6):
            try:
                response = requests.get(f'{base}/leagues/teams/{team_id}/current', headers={'Authorization': token}, params={'include': 'country', 'per_page': 50, 'page': page}, timeout=20)
            except requests.RequestException:
                raise RuntimeError('Current team league request failed') from None
            if response.status_code != 200:
                raise RuntimeError(f'Current team league request failed (HTTP {response.status_code})')
            try:
                payload = response.json()
            except ValueError:
                raise RuntimeError('Current team league response was invalid') from None
            for row in payload.get('data') or []:
                if row.get('type') == 'league' and row.get('sub_type') == 'domestic':
                    leagues[int(row['id'])] = {'league_id': int(row['id']), 'league_name': row['name'], 'league_country_name': (row.get('country') or {}).get('name') or ''}
            if not (payload.get('pagination') or {}).get('has_more'):
                break
        else:
            raise RuntimeError('Current team league response exceeded page limit')
        value = next(iter(leagues.values())) if len(leagues) == 1 else None
        with _cache_lock:
            _cache[team_id] = (time.monotonic() + (CACHE_TTL if value else EMPTY_TTL), value)
            _cache.move_to_end(team_id)
            while len(_cache) > CACHE_LIMIT:
                _cache.popitem(last=False)
        return value


def load_current_teams(db):
    # Only compact identity fields: no player stats, embeddings or upstream calls.
    contexts = db.execute(text('''SELECT DISTINCT team_id, team_name, league_id,
        league_name, league_country_name, league_type, league_sub_type
        FROM player_comp_data WHERE team_id IS NOT NULL AND COALESCE(team_name, '') <> ''
    ''')).mappings().all()
    current = db.execute(text('''SELECT metadata->>'team_id' team_id,
        COALESCE(NULLIF(metadata->>'team_current_league_name', ''),
          CASE WHEN metadata->>'league_source' = 'team_current' THEN metadata->>'league_name' END) league_name
        FROM player_data
        WHERE COALESCE(metadata->>'team_id', '') <> ''
        GROUP BY 1, 2
    ''')).mappings().all()
    names = {}
    for row in current:
        tid = entity_id(row['team_id'])
        if tid and fold(row['league_name']):
            names.setdefault(tid, set()).add(fold(row['league_name']))
    teams, catalogue, memberships = {}, {}, {}
    for row in contexts:
        tid = int(row['team_id'])
        team = teams.setdefault(tid, {'team_id': tid, 'team_name': row['team_name']})
        team['team_name'] = max(team['team_name'], row['team_name'])
        if row['league_type'] != 'league' or row['league_sub_type'] != 'domestic':
            continue
        league = {key: row[key] for key in ('league_id', 'league_name', 'league_country_name')}
        catalogue[row['league_id']] = league
        memberships.setdefault(tid, {})[row['league_id']] = league
    for tid, team in teams.items():
        current_names = names.get(tid, set())
        known = memberships.get(tid, {})
        countries = {row['league_country_name'] for row in known.values() if row['league_country_name']}
        team.update(league_id=None, league_name='', league_country_name=next(iter(countries)) if len(countries) == 1 else '')
        if len(current_names) == 1:
            name = next(iter(current_names))
            matches = [row for row in known.values() if fold(row['league_name']) == name]
            if not matches:
                matches = [row for row in catalogue.values() if fold(row['league_name']) == name and (not countries or row['league_country_name'] in countries)]
            if len(matches) == 1:
                team.update(matches[0])
        # Fresh imported membership always wins over a cached fallback.
        if team['league_id'] is None:
            hit, league = cached_league(tid)
            if hit and league:
                team.update(league)
    return sorted(teams.values(), key=lambda row: (row['team_name'], row['team_id']))


def get_team_filter_options(db, country=None, league=None):
    country, league = str(country or '').strip(), str(league or '').strip()
    teams = load_current_teams(db)
    return {
        'countries': sorted({t['league_country_name'] for t in teams if t['league_country_name'] and (not league or t['league_name'] == league)}),
        'leagues': sorted({t['league_name'] for t in teams if t['league_name'] and (not country or t['league_country_name'] == country)}),
        'teams': sorted({t['team_name'] for t in teams if (not country or t['league_country_name'] == country) and (not league or t['league_name'] == league)}),
    }
