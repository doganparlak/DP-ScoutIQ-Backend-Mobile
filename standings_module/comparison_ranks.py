"""Full competition-season ranks for pre-match metrics; recent values stay intact."""
from collections import OrderedDict
from datetime import datetime, timezone
import logging
import os
from threading import RLock
from time import monotonic
import requests
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from api_module.database import engine
from .metrics import Aggregate, LOWER_BETTER, COMPLETED_STATES, TEAM_METRIC_CATEGORY, kickoff, normalize_fixture, numeric
from .stages import EUROPEAN_LEAGUES, stage_scope

_ALIASES = {name.casefold(): canonical for name, (_, canonical) in TEAM_METRIC_CATEGORY.items()}
_ALIASES.update({
    'expected goals on target (xgot)': 'Expected Goals On Target',
    'long balls won percentage': 'Long Balls Won (%)',
    'aerials won percentage': 'Aerials Won (%)',
    'duels won percentage': 'Duels Won (%)',
    'successful crosses percentage': 'Accurate Crosses (%)',
    'successful crosses (%)': 'Accurate Crosses (%)',
    'accurate passes percentage': 'Accurate Passes (%)',
    'tackles won percentage': 'Tackles Won (%)',
    'tacles won percentage': 'Tackles Won (%)',
})

def canonical_metric_name(name):
    clean = ' '.join(str(name).split())
    return _ALIASES.get(clean.casefold(), clean)


_LOG = logging.getLogger(__name__)
_CACHE = OrderedDict()
_LOCK = RLock()


def rank_metrics(teams):
    """Rank recorded full-season per-match values, with shared competition ranks."""
    values = {}
    for tid, team in teams.items():
        if not team.get('matches'):
            continue
        for name, metric in team.get('metrics', {}).items():
            value = numeric(metric.get('perMatch'))
            if value is not None and metric.get('matchesCovered', 0) > 0:
                values.setdefault(canonical_metric_name(name), {})[str(tid)] = (value, metric)
    result = {}
    for name, entries in values.items():
        for tid, (value, metric) in entries.items():
            better = sum(other < value if name in LOWER_BETTER else other > value for other, _ in entries.values())
            result.setdefault(tid, {})[name] = {
                'position': better + 1, 'total_teams': len(entries),
                'season_value': value, 'matches_played': teams[tid]['matches'],
                'matches_covered': metric['matchesCovered'],
            }
    return result


def _provider_rows(league, season, cutoff, scope):
    """Fallback for a competition not yet opened in League Analysis Center."""
    token = os.getenv('SPORTMONKS_API_KEY')
    if not token:
        return []
    rows = []
    base = os.getenv('SPORTMONKS_BASE_URL', 'https://api.sportmonks.com/v3/football').rstrip('/')
    for page in range(1, 101):
        response = requests.get(base + '/fixtures', params={
            'api_token': token, 'filters': f'fixtureSeasons:{season};fixtureStates:5,7,8',
            'include': 'participants;statistics.type;xGFixture.type;stage', 'per_page': 100, 'page': page,
        }, timeout=45)
        if response.status_code != 200:
            raise RuntimeError('Competition ranks are temporarily unavailable')
        payload = response.json()
        for fixture in payload.get('data') or []:
            if fixture.get('league_id') != league or fixture.get('season_id') != season:
                raise ValueError('Competition rank season mismatch')
            if kickoff(fixture['starting_at']) >= cutoff or fixture.get('state_id') not in COMPLETED_STATES:
                continue
            if league in EUROPEAN_LEAGUES:
                phase = stage_scope(fixture.get('stage') or {})
                if (scope == 'qualifying' and phase != 'qualifying') or (scope == 'overall' and phase not in ('league', 'knockout')):
                    continue
            rows.append(normalize_fixture(fixture))
        if not (payload.get('pagination') or {}).get('has_more'):
            return rows
    raise RuntimeError('Competition rank pagination limit exceeded')


def competition_ranks(league, season, cutoff, scope='overall'):
    key = (league, season, cutoff.isoformat()[:13], scope)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and monotonic() - cached[0] < 300:
            return cached[1]
    # Use the exact same contributions as League Analysis Center. Filtering by
    # kickoff prevents later season matches leaking into an earlier report.
    with engine.connect() as db:
        rows = db.execute(text('''SELECT team_contributions, kickoff_at, included_in_aggregation
            FROM league_match_contributions WHERE league_id=:league AND season_id=:season
            AND included_in_aggregation=TRUE AND kickoff_at<:cutoff
            AND (:european=FALSE OR
                (:scope='qualifying' AND coverage->>'scope'='qualifying') OR
                (:scope='overall' AND coverage->>'scope' IN ('league','knockout')))
            ORDER BY kickoff_at,fixture_id'''), {
                'league': league, 'season': season, 'cutoff': cutoff,
                'european': league in EUROPEAN_LEAGUES, 'scope': scope,
            }).mappings().all()
    if not rows:
        rows = _provider_rows(league, season, cutoff, scope)
    aggregate = Aggregate()
    for row in rows:
        aggregate.add({**row, 'player_contributions': []})
    result = rank_metrics(aggregate.finish()['team_metrics'])
    with _LOCK:
        _CACHE[key] = (monotonic(), result)
        _CACHE.move_to_end(key)
        while len(_CACHE) > 64:
            _CACHE.popitem(last=False)
    return result


def enrich_comparison_ranks(usage, fixture, now=None):
    """Add optional metric ranks to a copy of a current pre-match sample."""
    league = fixture.get('league') or {}
    season = fixture.get('season') or {}
    if not league.get('id') or not season.get('id'):
        return usage
    try:
        current = now or datetime.now(timezone.utc)
        start = kickoff(fixture.get('startingAt') or fixture.get('starting_at'))
        if start <= current:  # Preserve saved historical reports.
            return usage
        scope = 'qualifying' if league['id'] in EUROPEAN_LEAGUES and stage_scope(fixture.get('stage') or {}) == 'qualifying' else 'overall'
        ranks = competition_ranks(int(league['id']), int(season['id']), current, scope)
        teams = []
        added = False
        for team in usage.get('teams') or []:
            by_metric = ranks.get(str(team['team_id']), {})
            categories = {}
            for category, metrics in (team.get('team_comparison') or {}).items():
                categories[category] = []
                for metric in metrics:
                    value = by_metric.get(canonical_metric_name(metric['name']))
                    clean = {k: v for k, v in metric.items() if k != 'competition_rank'}
                    if value and numeric(metric.get('value')) is not None:
                        clean['competition_rank'] = value
                        added = True
                    categories[category].append(clean)
            teams.append({**team, 'team_comparison': categories})
        result = {k: v for k, v in usage.items() if k != 'competition_ranking'}
        result['teams'] = teams
        if added:
            result['competition_ranking'] = {
                'league_id': int(league['id']), 'competition_name': league.get('name') or '',
                'season_id': int(season['id']), 'season_name': season.get('name') or '',
                'basis': 'per_match', 'scope': scope,
            }
        return result
    except (requests.RequestException, SQLAlchemyError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        _LOG.warning('Competition comparison ranks unavailable (%s)', type(exc).__name__)
        return usage


def enrich_report_comparison_ranks(content, fixture):
    if content.get('report_type') != 'pre_match' or not content.get('recent_squad_usage'):
        return content
    return {**content, 'recent_squad_usage': enrich_comparison_ranks(content['recent_squad_usage'], fixture)}
