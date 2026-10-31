"""Shared pre-kickoff match forecasts: small fitted Poisson models, no LLM calls."""
from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from threading import Lock
from time import perf_counter
from itertools import groupby

import numpy as np
import requests
from sqlalchemy import text

from api_module.database import engine
from .fixtures import SPORTMONKS_BASE_URL, get_fixture

LOG = logging.getLogger(__name__)
VERSION = 2
CACHE_REVISION = 1
WINDOW = 10
MIN_HISTORY = 5
MIN_TRAINING = 120
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='match-forecast')
_PENDING: set[int] = set()
_LOCK = Lock()
_SCHEMA_READY = False
# Standardized inputs; weights are learned from completed historical fixtures.
TRAINING_LEAGUES = (600, 603, 8, 564, 82, 384, 301, 2, 5, 2286)
STAT_NAMES = ('Shots On Target', 'Shots Total', 'Shots Insidebox', 'Big Chances Created', 'Ball Possession %', 'Expected Goals')


def utc(value):
    date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)


def _schema():
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _LOCK:
        if _SCHEMA_READY:
            return
        with engine.begin() as db:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('mobile_forecast_schema',0))"))
            db.execute(text('''CREATE TABLE IF NOT EXISTS mobile_match_forecast_cache (
                cache_key text PRIMARY KEY, payload jsonb NOT NULL,
                expires_at timestamptz NOT NULL, updated_at timestamptz NOT NULL DEFAULT NOW()
            )'''))
            db.execute(text("""CREATE TABLE IF NOT EXISTS mobile_match_forecast_models (
                id bigserial PRIMARY KEY, model_version integer NOT NULL,
                trained_at timestamptz NOT NULL DEFAULT NOW(), training_cutoff timestamptz NOT NULL,
                training_matches integer NOT NULL, validation_matches integer NOT NULL,
                competitions jsonb NOT NULL, validation_metrics jsonb NOT NULL,
                artifact jsonb NOT NULL, is_active boolean NOT NULL DEFAULT FALSE
            )"""))
            db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS mobile_forecast_one_active_model ON mobile_match_forecast_models(is_active) WHERE is_active"))
            db.execute(text('''CREATE TABLE IF NOT EXISTS mobile_match_forecast_snapshots (
                fixture_id bigint PRIMARY KEY, payload jsonb NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT NOW())'''))
            # Preserve genuine pre-kickoff forecasts, including expired cache
            # entries. Never turn a post-match reconstruction into a prediction.
            db.execute(text('''INSERT INTO mobile_match_forecast_snapshots(fixture_id,payload)
                SELECT DISTINCT ON ((payload->'forecast'->>'fixtureId')::bigint)
                    (payload->'forecast'->>'fixtureId')::bigint,payload
                FROM mobile_match_forecast_cache WHERE payload->>'status'='ready'
                AND (payload->'forecast'->>'generatedAt')::timestamptz < (payload->'forecast'->>'startingAt')::timestamptz
                AND (payload->'forecast'->>'asOf')::timestamptz < (payload->'forecast'->>'startingAt')::timestamptz
                ORDER BY (payload->'forecast'->>'fixtureId')::bigint,
                    (payload->'forecast'->>'generatedAt')::timestamptz DESC
                ON CONFLICT(fixture_id) DO NOTHING'''))
            for table in ('mobile_match_forecast_models', 'mobile_match_forecast_cache', 'mobile_match_forecast_snapshots'):
                enabled = db.execute(text('SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass(:table)'), {'table': table}).scalar()
                if not enabled:
                    db.execute(text(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY'))
        _SCHEMA_READY = True


def _read(key):
    with engine.connect() as db:
        return db.execute(text('SELECT payload FROM mobile_match_forecast_cache WHERE cache_key=:key AND expires_at>NOW()'), {'key': key}).scalar()


def _cache_key(fixture_id):
    return f'fixture:{fixture_id}:v{VERSION}:r{CACHE_REVISION}'


def _write(key, payload, expires):
    with engine.begin() as db:
        _save_snapshot(db, payload)
        db.execute(text('''INSERT INTO mobile_match_forecast_cache(cache_key,payload,expires_at)
            VALUES(:key,CAST(:payload AS jsonb),:expires) ON CONFLICT(cache_key) DO UPDATE
            SET payload=EXCLUDED.payload,expires_at=EXCLUDED.expires_at,updated_at=NOW()'''),
            {'key': key, 'payload': json.dumps(payload, allow_nan=False), 'expires': expires})


def _save_snapshot(db, payload):
    forecast = payload.get('forecast') or {}
    if payload.get('status') != 'ready' or utc(forecast['generatedAt']) >= utc(forecast['startingAt']) or utc(forecast['asOf']) >= utc(forecast['startingAt']):
        return
    db.execute(text('''INSERT INTO mobile_match_forecast_snapshots(fixture_id,payload)
        VALUES(:id,CAST(:payload AS jsonb)) ON CONFLICT(fixture_id) DO UPDATE
        SET payload=EXCLUDED.payload,updated_at=NOW()
        WHERE (EXCLUDED.payload->'forecast'->>'startingAt')::timestamptz>NOW()
        AND (EXCLUDED.payload->'forecast'->>'generatedAt')::timestamptz
          > (mobile_match_forecast_snapshots.payload->'forecast'->>'generatedAt')::timestamptz'''),
        {'id': forecast['fixtureId'], 'payload': json.dumps(payload, allow_nan=False)})


def save_forecast_snapshot(payload):
    _schema()
    with engine.begin() as db:
        _save_snapshot(db, payload)


def get_forecast_review(fixture_id):
    _schema()
    with engine.connect() as db:
        saved = db.execute(text('SELECT payload FROM mobile_match_forecast_snapshots WHERE fixture_id=:id'), {'id': fixture_id}).scalar()
    fixture = get_fixture(fixture_id)
    if not fixture or str((fixture.get('state') or {}).get('code', '')).upper() not in {'FT', 'AET', 'PEN'}:
        return {'status': 'unavailable', 'reason': 'match_not_completed'}
    if saved:
        return {**saved, 'reviewSource': 'saved'}
    # Keep reconstructed reviews separate from genuine pre-kickoff snapshots.
    # Both use exactly the same forecasting function and UI. The recorded
    # generation time stays truthful, and no post-kickoff model may participate.
    key = 'review:' + _cache_key(fixture_id)
    cached = _read(key)
    if cached:
        return cached
    kickoff = utc(fixture['startingAt'])
    model = load_saved_model(kickoff)
    if not model:
        return {'status': 'unavailable', 'reason': 'model_unavailable'}
    if utc(model['asOf']) >= kickoff:
        return {'status': 'unavailable', 'reason': 'model_unavailable'}
    result = {**build_forecast(fixture, kickoff, model), 'reviewSource': 'reconstructed'}
    if result['status'] == 'ready':
        _write(key, result, max(datetime.now(timezone.utc), kickoff) + timedelta(days=730))
    return result


def _request(path, params):
    token = os.getenv('SPORTMONKS_API_KEY')
    if not token:
        raise RuntimeError('Forecast data service unavailable')
    try:
        response = requests.get(f'{SPORTMONKS_BASE_URL}/{path}', params={'api_token': token, **params}, timeout=40)
    except requests.RequestException:
        raise RuntimeError('Forecast data request failed') from None
    if response.status_code != 200:
        raise RuntimeError(f'Forecast data service HTTP {response.status_code}')
    return response.json()


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None


def _compact(fixture):
    if (fixture.get('state') or {}).get('short_name') != 'FT':
        return None  # All targets and inputs refer to normal-time goals.
    sides = {p.get('meta', {}).get('location'): p for p in fixture.get('participants') or []}
    if not all(sides.get(side, {}).get('id') for side in ('home', 'away')):
        return None
    scores = {s.get('score', {}).get('participant'): s.get('score', {}).get('goals')
              for s in fixture.get('scores') or [] if s.get('description') == 'CURRENT'}
    if any(_number(scores.get(side)) is None for side in ('home', 'away')):
        return None
    first_half = {s.get('score', {}).get('participant'): _number(s.get('score', {}).get('goals'))
                  for s in fixture.get('scores') or [] if s.get('description') == '1ST_HALF'}
    valid_half = all(first_half.get(side) is not None and first_half[side].is_integer()
                     and first_half[side] <= float(scores[side]) for side in ('home', 'away'))
    stats = defaultdict(dict)
    for row in [*(fixture.get('statistics') or []), *(fixture.get('xgfixture') or [])]:
        name = str((row.get('type') or {}).get('name') or '')
        if name in {'Expected Goals (xG)', 'Expected Goals'}:
            name = 'Expected Goals'
        if name not in STAT_NAMES:
            continue
        data = row.get('data') or {}
        value = _number(data.get('value') if isinstance(data, dict) else data)
        if value is not None:
            stats[str(row.get('participant_id'))][name] = value
    return {'id': int(fixture['id']), 'at': fixture['starting_at'],
            'home': int(sides['home']['id']), 'away': int(sides['away']['id']),
            'hg': int(scores['home']), 'ag': int(scores['away']), 'stats': dict(stats),
            'hhg': int(first_half['home']) if valid_half else None,
            'hag': int(first_half['away']) if valid_half else None,
            'league': int(fixture.get('league_id') or 0), 'season': int(fixture.get('season_id') or 0)}


def _history(league_id, cutoff, season):
    # Date windows respect SportMonks' maximum of 100 days; paginate each one.
    windows = []
    start = utc(season['starting_at']).date()
    while start <= cutoff.date():
        end = min(start + timedelta(days=99), cutoff.date())
        windows.append((start, end))
        start = end + timedelta(days=1)

    def window(bounds):
        start, end = bounds
        rows = []
        for page in range(1, 31):
            payload = _request(f'fixtures/between/{start}/{end}', {
                'include': 'participants;state;scores;statistics.type;xGFixture.type',
                'filters': f'fixtureLeagues:{league_id}', 'per_page': 100, 'page': page})
            for fixture in payload.get('data') or []:
                if int(fixture.get('season_id') or 0) == int(season['id']) and int(fixture.get('league_id') or 0) == league_id and utc(fixture['starting_at']) < cutoff:
                    value = _compact(fixture)
                    if value:
                        rows.append(value)
            if not (payload.get('pagination') or {}).get('has_more'):
                return rows
        raise RuntimeError('Forecast history pagination exceeded')

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = [row for batch in pool.map(window, windows) for row in batch]
    return sorted({r['id']: r for r in rows}.values(), key=lambda r: (r['at'], r['id']))


def _summary(history):
    rows = history[-WINDOW:]
    result = {}
    if not rows:
        return result
    for key in ('gf', 'ga'):
        result[key] = sum(r[key] for r in rows) / len(rows)
    for side in ('own', 'opponent'):
        for name in STAT_NAMES:
            values = [r[side][name] for r in rows if name in r[side]]
            # Missing statistics remain missing rather than becoming zeros.
            result[f'{side}:{name}'] = sum(values) / len(values) if len(values) >= max(3, math.ceil(len(rows) * .6)) else None
    recent = rows[-3:]
    result['recent_gf'] = sum(r['gf'] for r in recent) / len(recent)
    result['recent_ga'] = sum(r['ga'] for r in recent) / len(recent)
    return result


BASE_FEATURES = ('gf', 'ga', 'recent_gf', 'recent_ga', 'opp_gf', 'opp_ga', 'opp_recent_gf', 'opp_recent_ga', 'home', 'league_home_goals', 'league_away_goals', 'rating', 'opponent_rating')
FEATURES = BASE_FEATURES + tuple(f'{side}:{name}' for side in ('own', 'opponent', 'opp_own', 'opp_opponent') for name in STAT_NAMES)


def _features(own, opponent, home, context=None):
    a, b = _summary(own), _summary(opponent)
    values = {**a, **{f'opp_{k}': v for k, v in b.items()}, 'home': float(home), **(context or {})}
    return [values.get(name) for name in FEATURES]


def _append(histories, match):
    for side, other, gf, ga in [('home', 'away', 'hg', 'ag'), ('away', 'home', 'ag', 'hg')]:
        team_id, opponent = match[side], match[other]
        histories[team_id].append({'id': match['id'], 'at': match['at'], 'gf': match[gf], 'ga': match[ga],
            'hgf': match.get('h' + gf), 'hga': match.get('h' + ga),
            'own': match['stats'].get(str(team_id), {}), 'opponent': match['stats'].get(str(opponent), {})})


def _update_half_totals(match, totals):
    if match.get('hhg') is None or match.get('hag') is None:
        return
    row = totals.setdefault(match['league'], [0, 0, 0, 0, 0])
    for index, value in enumerate((1, match['hg'], match['hhg'], match['ag'], match['hag'])):
        row[index] += value


def _half_prior(totals, league, home):
    full, first = (1, 2) if home else (3, 4)
    global_full = sum(row[full] for row in totals.values())
    global_first = sum(row[first] for row in totals.values())
    pooled = (global_first + 1) / (global_full + 2)
    row = totals.get(league, [0, 0, 0, 0, 0])
    return (row[first] + 5 * pooled) / (row[full] + 5)


def _half_evidence(history, conceded=False):
    rows = history[-WINDOW:]
    first, full = ('hga', 'ga') if conceded else ('hgf', 'gf')
    available = [row for row in rows if row.get(first) is not None]
    if len(available) < max(3, math.ceil(len(rows) * .6)):
        return 0, 0
    return sum(row[first] for row in available), sum(row[full] for row in available)


def _half_share(own, opponent, prior, strength):
    if strength is None:
        return prior  # A learned league-only candidate, not a fixed 50/50 split.
    scored_first, scored_full = _half_evidence(own)
    conceded_first, conceded_full = _half_evidence(opponent, conceded=True)
    return (scored_first + conceded_first + strength * prior) / (scored_full + conceded_full + strength)


def fit_half_model(matches):
    """Learn the temporal split without changing the full-match goal model."""
    histories, totals, examples, times = defaultdict(list), {}, [], []
    for _, simultaneous in groupby(matches, key=lambda match: utc(match['at'])):
        batch = list(simultaneous)
        for match in batch:
            own, opponent = histories[match['home']], histories[match['away']]
            if min(len(own), len(opponent)) < MIN_HISTORY or match.get('hhg') is None or match.get('hag') is None:
                continue
            pair = []
            for a, b, home, first, full in ((own, opponent, True, 'hhg', 'hg'), (opponent, own, False, 'hag', 'ag')):
                prior = _half_prior(totals, match['league'], home)
                pair.append({'shares': [_half_share(a, b, prior, strength) for strength in (5., 15., 30., 60., None)],
                             'first': match[first], 'full': match[full]})
            examples.append(pair); times.append(match['at'])
        for match in batch:
            _append(histories, match); _update_half_totals(match, totals)
    if len(examples) < MIN_TRAINING:
        return None
    boundary = int(len(examples) * .8)
    while boundary > 1 and times[boundary - 1] == times[boundary]:
        boundary -= 1
    validation = [row for pair in examples[boundary:] for row in pair]
    goals = sum(row['full'] for row in validation)
    if not goals:
        return None
    losses = []
    for index in range(5):
        loss = sum(-row['first'] * math.log(max(row['shares'][index], 1e-12))
                   - (row['full'] - row['first']) * math.log(max(1 - row['shares'][index], 1e-12))
                   for row in validation) / goals
        losses.append(loss)
    best = min(range(5), key=losses.__getitem__)
    return {'priorStrength': (5., 15., 30., 60., None)[best],
            'leagueTotals': {str(key): value for key, value in totals.items()},
            'inputMatches': sum(row[0] for row in totals.values()),
            'trainingMatches': len(examples), 'validationMatches': len(examples) - boundary,
            'validationGoalLogLoss': losses[best], 'leagueBaselineGoalLogLoss': losses[-1]}


def half_scenarios(home_rate, away_rate, own, opponent, league, model):
    if not model:
        return None
    totals = {int(key): value for key, value in model['leagueTotals'].items()}
    home_share = _half_share(own, opponent, _half_prior(totals, league, True), model['priorStrength'])
    away_share = _half_share(opponent, own, _half_prior(totals, league, False), model['priorStrength'])
    first_home, first_away = home_rate * home_share, away_rate * away_share
    comparison = score_distribution(first_home + first_away, home_rate + away_rate - first_home - first_away)['outcomes']
    return {'moreGoalsHalf': {'first': comparison['home'], 'equal': comparison['draw'], 'second': comparison['away']},
            'firstHalfBothTeamsScore': (1 - math.exp(-first_home)) * (1 - math.exp(-first_away))}


def _fit(x, y, penalty):
    beta = np.zeros(x.shape[1])
    beta[0] = math.log(max(float(y.mean()), .1))
    ridge = np.eye(x.shape[1]) * penalty
    ridge[0, 0] = .000001
    for _ in range(30):
        eta = np.clip(x @ beta, -5, 3)
        mu = np.exp(eta)
        gradient = x.T @ (mu - y) + ridge @ beta
        hessian = (x.T * mu) @ x + ridge
        step = np.linalg.solve(hessian, gradient)
        # Damping prevents a sparse feature from producing an extreme rate.
        factor = min(1., 1. / max(float(np.max(np.abs(step))), 1.))
        beta -= factor * step
        if np.max(np.abs(factor * step)) < 1e-6:
            break
    return beta


def _prepare(raw, fit_end, columns):
    selected = raw[:, columns]
    train = selected[:fit_end]
    medians = np.array([np.median(col[np.isfinite(col)]) if np.isfinite(col).any() else 0. for col in train.T])
    imputed = np.where(np.isfinite(selected), selected, medians)
    mean = imputed[:fit_end].mean(axis=0)
    scale = np.maximum(imputed[:fit_end].std(axis=0), .1)
    x = np.column_stack([np.ones(len(raw)), (imputed - mean) / scale])
    # Missingness indicators allow the model to learn availability differences.
    x = np.column_stack([x, (~np.isfinite(selected)).astype(float)])
    return x, medians, mean, scale


def _context(totals, ratings, league, team, opponent):
    count, hg, ag = totals.get(league, [0, 0., 0.])
    all_count = sum(v[0] for v in totals.values())
    global_home = sum(v[1] for v in totals.values()) / all_count if all_count else 1.4
    global_away = sum(v[2] for v in totals.values()) / all_count if all_count else 1.1
    return {'league_home_goals': (hg + 5 * global_home) / (count + 5),
            'league_away_goals': (ag + 5 * global_away) / (count + 5),
            'rating': ratings.get(team, 1500.), 'opponent_rating': ratings.get(opponent, 1500.)}


def _update_context(match, totals, ratings):
    league = totals.setdefault(match['league'], [0, 0., 0.])
    league[0] += 1; league[1] += match['hg']; league[2] += match['ag']
    home, away = match['home'], match['away']
    a, b = ratings.get(home, 1500.), ratings.get(away, 1500.)
    expected = 1 / (1 + 10 ** ((b - a - 60) / 400))
    outcome = 1. if match['hg'] > match['ag'] else .5 if match['hg'] == match['ag'] else 0.
    change = 20 * (outcome - expected)
    ratings[home], ratings[away] = a + change, b - change


def fit_model(matches):
    histories, totals, ratings = defaultdict(list), {}, {}
    features, targets, times, leagues = [], [], [], []
    matches = sorted(matches, key=lambda m: (utc(m['at']), m['id']))
    # Matches at the same kickoff cannot expose each other's results to inputs.
    for _, simultaneous in groupby(matches, key=lambda m: utc(m['at'])):
        batch = list(simultaneous)
        for match in batch:
            home, away = histories[match['home']], histories[match['away']]
            if len(home) >= MIN_HISTORY and len(away) >= MIN_HISTORY:
                features.extend([_features(home, away, True, _context(totals, ratings, match['league'], match['home'], match['away'])),
                                 _features(away, home, False, _context(totals, ratings, match['league'], match['away'], match['home']))])
                targets.extend([match['hg'], match['ag']]); times.append(match['at']); leagues.append(match['league'])
        for match in batch:
            _append(histories, match); _update_context(match, totals, ratings)
    if len(targets) < MIN_TRAINING * 2:
        return None
    raw, y = np.array(features, dtype=float), np.array(targets, dtype=float)
    boundary = int(len(times) * .8)
    while boundary > 1 and times[boundary - 1] == times[boundary]:
        boundary -= 1
    split = boundary * 2
    available = [i for i in range(len(BASE_FEATURES), len(FEATURES)) if np.isfinite(raw[:split, i]).mean() >= .6]
    candidates = []
    groups = [list(range(len(BASE_FEATURES)))]
    if available:
        groups.append(groups[0] + available)
    for columns in groups:
        x, _, _, _ = _prepare(raw, split, columns)
        for penalty in (5., 20., 80.):
            beta = _fit(x[:split], y[:split], penalty)
            prediction = np.exp(np.clip(x[split:] @ beta, -5, 3))
            loss = float(np.mean(prediction - y[split:] * np.log(prediction)))
            candidates.append((loss, columns, penalty, prediction))
    loss, columns, penalty, prediction = min(candidates, key=lambda c: c[0])
    per_competition = {}
    for league in set(leagues):
        indices = [i for i, value in enumerate(leagues[boundary:]) if value == league]
        if indices:
            positions = np.array([p for i in indices for p in (i * 2, i * 2 + 1)])
            per_competition[str(league)] = {'matches': len(indices),
                'poissonLoss': float(np.mean(prediction[positions] - y[split:][positions] * np.log(prediction[positions])))}
    # Outcome calibration measurements on the chronological holdout.
    observed = np.array([[float(a > b), float(a == b), float(a < b)] for a, b in y[split:].reshape(-1, 2)])
    probabilities = np.array([[d['outcomes'][k] for k in ('home', 'draw', 'away')]
        for a, b in prediction.reshape(-1, 2) for d in [score_distribution(float(a), float(b))]])
    brier = float(np.mean(np.sum((probabilities - observed) ** 2, axis=1)))
    x, medians, mean, scale = _prepare(raw, len(raw), columns)
    beta = _fit(x, y, penalty)
    half_model = fit_half_model(matches)
    return {'columns': columns, 'medians': medians.tolist(), 'mean': mean.tolist(), 'scale': scale.tolist(),
            'beta': beta.tolist(), 'trainingMatches': len(y) // 2, 'validationMatches': len(y[split:]) // 2,
            'inputMatches': len(matches), 'trainingByCompetition': {str(k): v for k, v in Counter(leagues).items()}, 'validationLoss': loss, 'validationBrier': brier,
            'baselineValidationLoss': min(c[0] for c in candidates if len(c[1]) == len(BASE_FEATURES)),
            'validationByCompetition': per_competition, 'regularization': penalty,
            'usesTeamStats': any(i >= len(BASE_FEATURES) for i in columns),
            'features': [FEATURES[i] for i in columns],
            'halfTimeModel': half_model, 'validationHalfTime': half_model,
            'leagueTotals': {str(k): v for k, v in totals.items()}, 'ratings': {str(k): v for k, v in ratings.items()},
            'histories': {str(k): v[-WINDOW:] for k, v in histories.items()}}


def _rate(model, own, opponent, home, context=None):
    raw = np.array(_features(own, opponent, home, context), dtype=float)[model['columns']]
    missing = ~np.isfinite(raw)
    raw = np.where(missing, model['medians'], raw)
    x = np.concatenate([[1.], (raw - model['mean']) / model['scale'], missing.astype(float)])
    return float(np.exp(np.clip(x @ np.array(model['beta']), math.log(.05), math.log(8.))))


def score_distribution(home_rate, away_rate):
    # Adaptive tail makes the visible grid a subset of a complete distribution.
    def goals(rate):
        probabilities = [math.exp(-rate)]
        k = 0
        while 1 - sum(probabilities) > 1e-12:
            k += 1
            probabilities.append(probabilities[-1] * rate / k)
            if k > 100:
                raise ValueError('Invalid goal distribution')
        return probabilities
    home, away = goals(home_rate), goals(away_rate)
    total = sum(home) * sum(away)
    scores = [{'home': h, 'away': a, 'probability': p * q / total}
              for h, p in enumerate(home) for a, q in enumerate(away)]
    return {'outcomes': {'home': sum(r['probability'] for r in scores if r['home'] > r['away']),
                         'draw': sum(r['probability'] for r in scores if r['home'] == r['away']),
                         'away': sum(r['probability'] for r in scores if r['home'] < r['away'])},
            'bothTeamsScore': sum(r['probability'] for r in scores if r['home'] and r['away']),
            'cleanSheet': {'home': sum(r['probability'] for r in scores if r['away'] == 0),
                           'away': sum(r['probability'] for r in scores if r['home'] == 0)},
            'over25': sum(r['probability'] for r in scores if r['home'] + r['away'] >= 3),
            'topScores': sorted(scores, key=lambda r: r['probability'], reverse=True)[:3],
            'grid': [r for r in scores if r['home'] <= 5 and r['away'] <= 5],
            'gridRemainder': sum(r['probability'] for r in scores if r['home'] > 5 or r['away'] > 5),
            'goalDistribution': {'home': home[:6] + [max(0., 1 - sum(home[:6]))],
                                  'away': away[:6] + [max(0., 1 - sum(away[:6]))]}}


def build_forecast(fixture, cutoff, model):
    home, away = fixture['homeTeam'], fixture['awayTeam']
    season = str((fixture.get('season') or {}).get('name') or '').replace(' ', '').replace('/', '')
    trained_seasons = {str(c['seasonName']).replace(' ', '').replace('/', '') for c in model.get('competitions', [])}
    if season and season not in trained_seasons:
        return {'status': 'unavailable', 'reason': 'season_mismatch'}
    own, opponent = model['histories'].get(str(home['id']), []), model['histories'].get(str(away['id']), [])
    if min(len(own), len(opponent)) < MIN_HISTORY:
        return {'status': 'unavailable', 'reason': 'insufficient_history'}
    # Even cached histories are bounded strictly by the requested forecast cutoff.
    if any(utc(r['at']) >= cutoff for r in own + opponent):
        raise ValueError('Forecast history crosses kickoff')
    totals = {int(k): v for k, v in model['leagueTotals'].items()}
    ratings = {int(k): v for k, v in model['ratings'].items()}
    league = fixture['league']['id']
    a = _rate(model, own, opponent, True, _context(totals, ratings, league, home['id'], away['id']))
    b = _rate(model, opponent, own, False, _context(totals, ratings, league, away['id'], home['id']))
    return {'status': 'ready', 'forecast': {'fixtureId': fixture['fixtureId'], 'modelVersion': VERSION,
            'startingAt': fixture['startingAt'], 'asOf': model.get('asOf', cutoff.isoformat()), 'generatedAt': datetime.now(timezone.utc).isoformat(),
            'scope': 'current_season_last_10', 'modelId': model.get('modelId'), 'trainingCompetitions': model.get('competitions', []), 'regulationMinutes': 90,
            'home': {'id': home['id'], 'name': home['name'], 'imageUrl': home.get('imageUrl'), 'matches': len(own), 'projectedGoals': a},
            'away': {'id': away['id'], 'name': away['name'], 'imageUrl': away.get('imageUrl'), 'matches': len(opponent), 'projectedGoals': b},
            'trainingMatches': model['trainingMatches'], 'validationMatches': model['validationMatches'],
            'usesTeamStats': model['usesTeamStats'], 'features': model['features'],
            'halfScenarios': half_scenarios(a, b, own, opponent, league, model.get('halfTimeModel')),
            **score_distribution(a, b)}}


def load_saved_model(cutoff):
    with engine.connect() as db:
        row = db.execute(text("""SELECT id,artifact FROM mobile_match_forecast_models
            WHERE model_version=:version AND training_cutoff<:cutoff
            ORDER BY is_active DESC,training_cutoff DESC LIMIT 1"""),
            {'version': VERSION, 'cutoff': cutoff}).mappings().first()
    return {**row['artifact'], 'modelId': row['id']} if row else None


def train_shared_model(refresh_key=None):
    """Explicit offline operation. Never invoked by report endpoints."""
    _schema()
    cutoff = datetime.now(timezone.utc)
    with engine.begin() as lock:
        lock.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('train_shared_match_forecast',0))"))
        if refresh_key:
            previous = lock.execute(text("""SELECT id FROM mobile_match_forecast_models
                WHERE model_version=:version AND artifact->>'refreshKey'=:key
                ORDER BY id DESC LIMIT 1"""), {'version': VERSION, 'key': refresh_key}).scalar()
            if previous:
                return {'modelId': previous, 'reused': True}
        def collect(league_id):
            league = _request(f'leagues/{league_id}', {'include': 'currentSeason'}).get('data') or {}
            season = league.get('currentseason') or {}
            if not season.get('id') or not season.get('starting_at'):
                raise ValueError(f'Current season missing for competition {league_id}')
            matches = _history(league_id, cutoff, season)
            if not matches:
                raise ValueError(f'Current season fixtures missing for competition {league_id}')
            return {'leagueId': league_id, 'name': league['name'], 'seasonId': season['id'],
                    'seasonName': season['name'], 'startsAt': season['starting_at'], 'matches': len(matches)}, matches
        started = perf_counter()
        with ThreadPoolExecutor(max_workers=3) as pool:
            batches = list(pool.map(collect, TRAINING_LEAGUES))
        competitions = [b[0] for b in batches]
        matches = list({m['id']: m for _, rows in batches for m in rows}.values())
        model = fit_model(matches)
        if not model:
            raise ValueError('Insufficient current-season data for a shared model')
        model.update(asOf=cutoff.isoformat(), competitions=competitions, version=VERSION)
        if refresh_key:
            model['refreshKey'] = refresh_key
        validation = {k: v for k, v in model.items() if k.startswith('validation') or k == 'baselineValidationLoss'}
        with engine.begin() as db:
            db.execute(text('UPDATE mobile_match_forecast_models SET is_active=FALSE WHERE is_active'))
            db.execute(text("""DELETE FROM mobile_match_forecast_cache
                WHERE (cache_key LIKE :pattern OR cache_key LIKE :legacy) AND
                (payload->>'status' <> 'ready' OR (payload->'forecast'->>'startingAt')::timestamptz>NOW())"""),
                {'pattern': f'fixture:%:v{VERSION}:%', 'legacy': f'fixture:%:v{VERSION}'})
            model_id = db.execute(text("""INSERT INTO mobile_match_forecast_models
                (model_version,training_cutoff,training_matches,validation_matches,competitions,validation_metrics,artifact,is_active)
                VALUES(:version,:cutoff,:training,:validation,CAST(:competitions AS jsonb),CAST(:metrics AS jsonb),CAST(:artifact AS jsonb),TRUE)
                RETURNING id"""), {'version': VERSION, 'cutoff': cutoff, 'training': model['trainingMatches'],
                'validation': model['validationMatches'], 'competitions': json.dumps(competitions),
                'metrics': json.dumps(validation, allow_nan=False), 'artifact': json.dumps(model, allow_nan=False)}).scalar_one()
        return {'modelId': model_id, 'inputMatches': len(matches), 'trainingMatches': model['trainingMatches'],
                'validationMatches': model['validationMatches'], 'usesTeamStats': model['usesTeamStats'],
                'competitions': [{**c, 'trainingExamples': model['trainingByCompetition'].get(str(c['leagueId']), 0)} for c in competitions], 'metrics': validation, 'elapsedSeconds': round(perf_counter() - started, 2)}


def _run(fixture_id):
    key = _cache_key(fixture_id)
    try:
        with engine.begin() as owner:
            locked = owner.execute(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))'), {'key': 'forecast:' + key}).scalar()
            if not locked:
                return
            fixture = get_fixture(fixture_id)
            if not fixture or not fixture.get('league', {}).get('id'):
                raise ValueError('Fixture unavailable')
            kickoff = utc(fixture['startingAt'])
            cutoff = min(datetime.now(timezone.utc), kickoff)
            # Select a model trained strictly before kickoff; never retrain here.
            model = load_saved_model(kickoff)
            result = build_forecast(fixture, cutoff, model) if model else {'status': 'unavailable', 'reason': 'model_unavailable'}
            _write(key, result, kickoff + timedelta(days=730) if kickoff <= datetime.now(timezone.utc) else min(kickoff, datetime.now(timezone.utc) + timedelta(hours=1)))
    except Exception as exc:
        LOG.warning('Forecast fixture=%s failed (%s)', fixture_id, type(exc).__name__)
        _write(key, {'status': 'failed', 'reason': 'data_unavailable'}, datetime.now(timezone.utc) + timedelta(minutes=1))
    finally:
        with _LOCK:
            _PENDING.discard(fixture_id)


def get_forecast(fixture_id, retry=False):
    _schema()
    key = _cache_key(fixture_id)
    cached = _read(key)
    if cached and retry and cached.get('status') in {'failed', 'unavailable'}:
        with engine.begin() as db:
            db.execute(text("DELETE FROM mobile_match_forecast_cache WHERE cache_key=:key AND payload->>'status' IN ('failed','unavailable')"), {'key': key})
        cached = None
    if cached:
        return cached
    with _LOCK:
        if fixture_id not in _PENDING:
            _PENDING.add(fixture_id)
            _POOL.submit(_run, fixture_id)
    return {'status': 'processing'}


def forecast_for_tier(result, tier):
    """Scope the shared cache per request; never cache one user's entitlement."""
    tier = tier if tier in ('free', 'plus', 'pro') else 'free'
    scoped = {**result, 'accessTier': tier}
    forecast = result.get('forecast')
    if tier == 'free' and isinstance(forecast, dict):
        fields = ('fixtureId', 'modelVersion', 'startingAt', 'regulationMinutes',
                  'home', 'away', 'outcomes', 'topScores', 'grid', 'gridRemainder')
        scoped['forecast'] = {key: forecast[key] for key in fields if key in forecast}
    return scoped
