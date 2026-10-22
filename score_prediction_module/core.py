"""Small shared weekly fixture pools; no AI or user-specific upstream requests."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import random
from zoneinfo import ZoneInfo

import requests

from match_pool_module.fixtures import SPORTMONKS_BASE_URL, SportMonksError, _fixture_out

LEAGUES = {8: ('Premier League', 'England'), 564: ('La Liga', 'Spain'),
           301: ('Ligue 1', 'France'), 384: ('Serie A', 'Italy'),
           82: ('Bundesliga', 'Germany'), 600: ('Super Lig', 'Turkey')}
# SportMonks team IDs: Galatasaray, Fenerbahçe, Beşiktaş, Trabzonspor, Başakşehir.
SUPER_LIG_PRIORITY_TEAMS = (34, 88, 554, 688, 3702)
ZONE = ZoneInfo('Europe/Istanbul')
EXCLUDED = {'POSTP', 'POSTPONED', 'CANC', 'CANCELLED', 'ABAN', 'ABANDONED',
            'SUSP', 'SUSPENDED', 'INT', 'INTERRUPTED', 'WO', 'AWARDED', 'AP'}
COMPLETED = {'FT', 'AET', 'PEN'}


def utcnow():
    return datetime.now(timezone.utc)


def kickoff(fixture):
    value = fixture.get('startingAt')
    if not value:
        raise ValueError('Fixture kickoff is missing')
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def week_start(now=None):
    day = (now or utcnow()).astimezone(ZONE).date()
    # Monday belongs to the previous Friday–Monday competition. The new
    # competition becomes available on Tuesday, using its Monday as its ID.
    return day - timedelta(days=day.weekday() + (7 if day.weekday() == 0 else 0))


def fixture_window(start):
    """Friday inclusive through the following Tuesday exclusive (Istanbul)."""
    return start + timedelta(days=4), start + timedelta(days=8)


def draw_window(start, now=None):
    day = (now or utcnow()).astimezone(ZONE).date()
    return start + timedelta(days=1) <= day < start + timedelta(days=4)


def head_start(tier):
    # Fixed weekly starting points, never a percentage of match points.
    return {'plus': 2, 'pro': 4}.get(tier, 0)


def phase(fixture):
    if fixture.get('excluded'):
        return 'excluded'
    state = fixture.get('state') or {}
    code = str(state.get('code') or '').upper().replace(' ', '_')
    name = str(state.get('name') or '').casefold()
    if code in EXCLUDED or any(word in name for word in ('postpon', 'cancel', 'abandon', 'suspend', 'interrupt', 'walkover', 'awarded')):
        return 'excluded'
    if code in COMPLETED or name in {'full time', 'finished', 'completed', 'after extra time', 'after penalties'}:
        return 'finished'
    return 'pending'


def select_fixtures(fixtures, now=None, rng=None, competition_week=None):
    now, rng = now or utcnow(), rng or random.SystemRandom()
    start, end = fixture_window(competition_week or week_start(now))
    pools = {league: [] for league in LEAGUES}
    seen = set()
    for item in fixtures:
        league = (item.get('league') or {}).get('id')
        if league not in pools or item.get('fixtureId') in seen:
            continue
        try:
            date = kickoff(item).astimezone(ZONE).date()
            future = kickoff(item) > now and start <= date < end
        except (ValueError, TypeError):
            continue
        if not future or phase(item) != 'pending' or str((item.get('state') or {}).get('code', '')).upper() not in {'NS', 'TBA'}:
            continue
        seen.add(item['fixtureId'])
        pools[league].append(item)
    # A round needs exactly three Turkish and seven foreign fixtures. Never
    # exceed the Turkish cap to compensate for missing foreign fixtures.
    if len(pools[600]) < 3 or sum(len(items) for lid, items in pools.items() if lid != 600) < 7:
        return []
    selected = []
    selected_ids = set()
    for team_id in SUPER_LIG_PRIORITY_TEAMS:
        if len(selected) == 3:
            break
        matches = [item for item in pools[600]
                   if team_id in {item['homeTeam']['id'], item['awayTeam']['id']}]
        if any(item['fixtureId'] in selected_ids for item in matches):
            continue  # A derby already covers both priority teams.
        if matches:
            chosen = min(matches, key=kickoff)
            selected.append(chosen)
            selected_ids.add(chosen['fixtureId'])
    turkey = [item for item in pools[600] if item['fixtureId'] not in selected_ids]
    selected.extend(rng.sample(turkey, 3 - len(selected)))
    remaining = []
    for lid, items in pools.items():
        if lid == 600 or not items:
            continue
        chosen = rng.choice(items)
        selected.append(chosen)
        remaining.extend(item for item in items if item['fixtureId'] != chosen['fixtureId'])
    # With all five foreign leagues present this leaves two random matches.
    selected.extend(rng.sample(remaining, 10 - len(selected)))
    return sorted(selected, key=kickoff)


def fetch_week_fixtures(start):
    token = os.getenv('SPORTMONKS_API_KEY')
    if not token:
        raise SportMonksError('Fixture service is not configured')
    # One batched league query, paginated only if needed; never six scans per user.
    start, end = fixture_window(start)
    result = []
    for page in range(1, 6):
        response = requests.get(f'{SPORTMONKS_BASE_URL}/fixtures/between/{start-timedelta(days=1)}/{end}', params={
            'api_token': token, 'include': 'participants;league.country;state;scores',
            'filters': 'fixtureLeagues:' + ','.join(map(str, LEAGUES)),
            'per_page': 50, 'page': page, 'order': 'asc'}, timeout=30)
        if response.status_code != 200:
            raise SportMonksError(f'Weekly fixture service returned HTTP {response.status_code}')
        payload = response.json()
        for raw in payload.get('data') or []:
            item = _fixture_out(raw)
            if item and kickoff(item).astimezone(ZONE).date() >= start and kickoff(item).astimezone(ZONE).date() < end:
                result.append(item)
        pagination = payload.get('pagination') or (payload.get('meta') or {}).get('pagination') or {}
        if not pagination.get('has_more'):
            break
    else:
        raise SportMonksError('Weekly fixture pagination was incomplete')
    return result


def points(pick, home, away):
    ph, pa = pick['home'], pick['away']
    if (ph, pa) == (home, away):
        return 5
    sign = lambda value: (value > 0) - (value < 0)
    return (2 if sign(ph-pa) == sign(home-away) else 0) + int(ph-pa == home-away) + int(ph == home) + int(pa == away)


def score_entry(picks, fixtures, tier):
    details, exact = {}, 0
    for fixture in fixtures:
        key = str(fixture['fixtureId'])
        pick = picks.get(key)
        if not pick or phase(fixture) != 'finished':
            continue
        home, away = fixture['homeTeam'].get('score'), fixture['awayTeam'].get('score')
        if not isinstance(home, int) or not isinstance(away, int):
            continue
        details[key] = points(pick, home, away)
        exact += int((pick['home'], pick['away']) == (home, away))
    base = sum(details.values())
    bonus = head_start(tier)
    return {'base': base, 'bonus': Decimal(bonus), 'total': Decimal(base + bonus), 'exact': exact, 'details': details}


def plan_bonus(user, now=None):
    expires = user.get('subscription_end_at')
    if not expires or expires <= (now or utcnow()):
        return 'free', 0
    if user['plan'] in {'Pro Monthly', 'Pro Yearly'}:
        return 'pro', head_start('pro')
    if user['plan'] == 'No Ads Monthly':
        return 'plus', head_start('plus')
    return 'free', 0
