"""Offline feature engineering and selection for the shared forecast model.

Only completed matches before each kickoff enter features. Prediction uses the
same stored schema and transformation as training; no provider calls are needed.
"""
from collections import defaultdict
from datetime import timedelta
import math
import numpy as np

LEGACY_STATS = ('Shots On Target', 'Shots Total', 'Shots Insidebox', 'Big Chances Created', 'Ball Possession %', 'Expected Goals')
ADVANCED_STATS = ('Expected Goals On Target', 'Expected Goals Non Penalty Goals (npxG)',
    'Expected Goals Open Play (xGOP)', 'Expected Goals Set Play (xGSP)', 'Expected Goals Corners (xGC)',
    'Big Chances Missed', 'Dangerous Attacks', 'Attacks', 'Corners', 'Shots Off Target', 'Shots Blocked',
    'Shots Outsidebox', 'Successful Passes Percentage', 'Passes', 'Key Passes', 'Long Passes',
    'Successful Long Passes Percentage', 'Total Crosses', 'Accurate Crosses', 'Tackles', 'Tackles Won',
    'Interceptions', 'Successful Dribbles', 'Duels Won', 'Saves', 'Fouls', 'Yellowcards', 'Redcards')
PRESSURE_STATS = ('Pressure Net', 'Pressure Dominance', 'Pressure Late Net')
STAT_NAMES = LEGACY_STATS + ADVANCED_STATS + PRESSURE_STATS
ALIASES = {'Expected Goals (xG)': 'Expected Goals', 'Expected Goals on Target (xGoT)': 'Expected Goals On Target',
           'Expected Goals On Target (xGoT)': 'Expected Goals On Target', 'Shots Inside Box': 'Shots Insidebox',
           'Accurate Passes Percentage': 'Successful Passes Percentage', 'Red Cards': 'Redcards'}
CORE = ('gf', 'ga', 'recent_gf', 'recent_ga', 'opp_gf', 'opp_ga', 'opp_recent_gf', 'opp_recent_ga',
        'home', 'league_home_goals', 'league_away_goals', 'rating', 'opponent_rating')
VENUE = ('venue_gf', 'venue_ga', 'opp_venue_gf', 'opp_venue_ga')
SCHEDULE = ('rest_days', 'matches_7_days', 'matches_14_days', 'opp_rest_days', 'opp_matches_7_days', 'opp_matches_14_days')
STRATEGIES = ('legacy', 'recent', 'opponent', 'venue', 'schedule')


def canonical_stats(fixture):
    """Deduplicate provider aliases by canonical name and participant, prefer xG feed."""
    from .forecast import _number
    result = defaultdict(dict)
    for row in [*(fixture.get('statistics') or []), *(fixture.get('xgfixture') or [])]:
        name = str((row.get('type') or {}).get('name') or '')
        name = ALIASES.get(name, name)
        if name not in STAT_NAMES or not row.get('participant_id'):
            continue
        data = row.get('data') or {}
        value = _number(data.get('value') if isinstance(data, dict) else data)
        if value is not None:
            result[str(row['participant_id'])][name] = value
    sides = {p.get('meta', {}).get('location'): str(p.get('id')) for p in fixture.get('participants') or []}
    home, away = sides.get('home'), sides.get('away')
    points = defaultdict(lambda: defaultdict(list))
    for row in fixture.get('pressure') or []:
        minute, value = _number(row.get('minute')), _number(row.get('pressure'))
        team = str(row.get('participant_id'))
        if team in (home, away) and minute is not None and 1 <= minute <= 90 and value is not None:
            points[team][int(minute)].append(value)
    shared = sorted(set(points[home]) & set(points[away]))
    # A missing/unpaired pressure series is missing evidence, never zero pressure.
    if len(shared) >= 60:
        net = {t: float(np.mean(points[home][t]) - np.mean(points[away][t])) for t in shared}
        for team, sign in ((home, 1), (away, -1)):
            values = [v * sign for v in net.values()]
            late = [v * sign for t, v in net.items() if t >= 61]
            result[team]['Pressure Net'] = float(np.mean(values))
            result[team]['Pressure Dominance'] = float(np.mean([1. if v > 0 else .5 if v == 0 else 0. for v in values]))
            if len(late) >= 20:
                result[team]['Pressure Late Net'] = float(np.mean(late))
    return dict(result)


def schema(strategy, stats):
    names = list(CORE)
    if strategy in ('venue', 'schedule'):
        names += VENUE
    if strategy == 'schedule':
        names += SCHEDULE
    names += [f'{side}:{name}' for side in ('own', 'opponent', 'opp_own', 'opp_opponent') for name in stats]
    return names


def _mean(rows, key, weighted, adjusted=False):
    from .forecast import WINDOW
    rows = rows[-WINDOW:]
    available = []
    for index, row in enumerate(rows):
        value = row.get(key) if ':' not in key else row.get(key.split(':', 1)[0], {}).get(key.split(':', 1)[1])
        if value is None or not math.isfinite(float(value)):
            continue
        weight = 2 ** (-(len(rows) - 1 - index) / 5.) if weighted else 1.
        # Pre-match opponent rating is stored at the historical kickoff. We
        # adjust attacking production up against stronger opponents, and vice versa.
        if adjusted and (key in ('gf', 'ga') or key.split(':')[-1] in ('Expected Goals', 'Expected Goals On Target', 'Shots On Target', 'Shots Total', 'Shots Insidebox', 'Big Chances Created')):
            strength = row.get('opponentStrength', 0.)
            attacking = key == 'gf' or key.startswith('own:')
            value *= math.exp((1 if attacking else -1) * strength)
        available.append((float(value), weight))
    if not available or (':' in key and len(available) < max(3, math.ceil(len(rows) * .6))):
        return None
    return sum(v * w for v, w in available) / sum(w for _, w in available)


def summary(history, home, kickoff, strategy):
    from .forecast import utc, WINDOW
    rows = history[-WINDOW:]
    weighted = strategy != 'legacy'
    adjusted = strategy in ('opponent', 'venue', 'schedule')
    result = {key: _mean(rows, key, weighted, adjusted) for key in ('gf', 'ga')}
    for side in ('own', 'opponent'):
        for name in STAT_NAMES:
            result[f'{side}:{name}'] = _mean(rows, f'{side}:{name}', weighted, adjusted)
    for key in ('gf', 'ga'):
        result[f'recent_{key}'] = _mean(rows[-3:], key, weighted, adjusted)
        if strategy in ('venue', 'schedule'):
            venue = [r for r in rows if r.get('home') is home]
            specific = _mean(venue, key, weighted, adjusted)
            overall = result[key]
            strength = len(venue) / (len(venue) + 5.)
            result[f'venue_{key}'] = overall if specific is None else strength * specific + (1 - strength) * overall
    if strategy == 'schedule' and kickoff and rows:
        days = [(utc(kickoff) - utc(row['at'])).total_seconds() / 86400 for row in rows]
        if min(days) <= 0:
            raise ValueError('Forecast history crosses kickoff')
        result.update(rest_days=min(min(days), 30.), matches_7_days=sum(d <= 7 for d in days), matches_14_days=sum(d <= 14 for d in days))
    return result


def values(own, opponent, home, context, strategy, names):
    context = context or {}
    a = summary(own, home, context.get('kickoff'), strategy)
    b = summary(opponent, not home, context.get('kickoff'), strategy)
    merged = {**a, **{f'opp_{key}': value for key, value in b.items()}, 'home': float(home), **context}
    return [merged.get(name) for name in names]


def append_match(histories, match, ratings):
    from .forecast import utc
    # Ratings used in adjustment must precede this match's result.
    for side, other, gf, ga in (('home', 'away', 'hg', 'ag'), ('away', 'home', 'ag', 'hg')):
        team, opponent = match[side], match[other]
        # Global 1500 Elo anchor remains stable; shrink and cap for sparse schedules.
        strength = max(-.4, min(.4, (ratings.get(opponent, 1500.) - 1500.) * math.log(10) / 800.))
        histories[team].append({'id': match['id'], 'at': match['at'], 'gf': match[gf], 'ga': match[ga],
            'hgf': match.get('h' + gf), 'hga': match.get('h' + ga), 'home': side == 'home',
            'opponentStrength': strength, 'own': match['stats'].get(str(team), {}), 'opponent': match['stats'].get(str(opponent), {})})


def examples(matches, strategy):
    from .forecast import utc, _context, _update_context, MIN_HISTORY
    histories, totals, ratings = defaultdict(list), {}, {}
    matches = sorted(matches, key=lambda m: (utc(m['at']), m['id']))
    names = schema(strategy, STAT_NAMES)
    rows, targets, times, leagues, ids = [], [], [], [], []
    pending, index = [], 0
    for match in matches:
        kickoff = utc(match['at'])
        # A final result of an overlapping fixture must not enter the features.
        while index < len(pending) and utc(pending[index]['at']) + timedelta(hours=3) <= kickoff:
            previous = pending[index]
            append_match(histories, previous, ratings)
            _update_context(previous, totals, ratings)
            index += 1
        own, opponent = histories[match['home']], histories[match['away']]
        if min(len(own), len(opponent)) >= MIN_HISTORY:
            for a, b, home, team, other, goal in ((own, opponent, True, match['home'], match['away'], 'hg'), (opponent, own, False, match['away'], match['home'], 'ag')):
                context = {**_context(totals, ratings, match['league'], team, other), 'kickoff': match['at']}
                rows.append(values(a, b, home, context, strategy, names))
                targets.append(match[goal])
            times.append(match['at']); leagues.append(match['league']); ids.append(match['id'])
        pending.append(match)
    for match in pending[index:]:
        append_match(histories, match, ratings); _update_context(match, totals, ratings)
    return np.array(rows, dtype=float), np.array(targets, dtype=float), times, leagues, ids, histories, totals, ratings, names


def _boundary(times, fraction):
    boundary = int(len(times) * fraction)
    while boundary > 1 and times[boundary - 1] == times[boundary]:
        boundary -= 1
    return boundary


def _joint_loss(rates, goals, rho=0.):
    losses = rates - goals * np.log(rates) + np.array([math.lgamma(y + 1) for y in goals])
    pairs = goals.reshape(-1, 2)
    rates = rates.reshape(-1, 2)
    factors = []
    for (h, a), (hg, ag) in zip(rates, pairs):
        factors.append(dc_factor(h, a, int(hg), int(ag), rho))
    return float(np.mean(losses.reshape(-1, 2).sum(axis=1) - np.log(np.maximum(factors, 1e-12))))


def bounded_rho(home, away, rho):
    # Strict positivity for all four affected cells, even at extreme goal rates.
    return max(-.99 / max(home, away), min(float(rho), .99, .99 / (home * away)))


def dc_factor(home, away, hg, ag, rho):
    rho = bounded_rho(home, away, rho)
    if (hg, ag) == (0, 0): return 1 - home * away * rho
    if (hg, ag) == (0, 1): return 1 + home * rho
    if (hg, ag) == (1, 0): return 1 + away * rho
    if (hg, ag) == (1, 1): return 1 - rho
    return 1.


def metrics(rates, goals, rho=0.):
    from .forecast import score_distribution
    pairs = goals.reshape(-1, 2)
    distributions = [score_distribution(float(h), float(a), rho) for h, a in rates.reshape(-1, 2)]
    p = np.array([[d['outcomes'][k] for k in ('home', 'draw', 'away')] for d in distributions])
    observed = np.array([[float(h > a), float(h == a), float(h < a)] for h, a in pairs])
    accuracy = float(np.mean(p.argmax(axis=1) == observed.argmax(axis=1)))
    exact = float(np.mean([d['topScores'][0]['home'] == h and d['topScores'][0]['away'] == a for d, (h, a) in zip(distributions, pairs)]))
    reliability = []
    for lo in np.arange(0., 1., .1):
        mask = (p >= lo) & (p < lo + .1 if lo < .9 else p <= 1.)
        if mask.any():
            reliability.append({'lower': round(float(lo), 1), 'count': int(mask.sum()), 'predicted': float(p[mask].mean()), 'observed': float(observed[mask].mean())})
    return {'matches': len(pairs), 'scoreLogLoss': _joint_loss(rates, goals, rho),
        'brier': float(np.mean(np.sum((p - observed) ** 2, axis=1))),
        'outcomeLogLoss': float(-np.log(np.maximum((p * observed).sum(axis=1), 1e-12)).mean()),
        'outcomeAccuracy': accuracy, 'exactScoreAccuracy': exact, 'reliability': reliability}


def league_weights(leagues, power=.5, reference=None):
    """Temper league-volume imbalance; weights use earlier training counts only."""
    from collections import Counter
    counts = Counter(reference if reference is not None else leagues)
    typical = float(np.median(list(counts.values()))) if counts else 1.
    weights = np.array([min(2.5, max(.4, (typical / max(counts.get(league, 1), 1)) ** power)) for league in leagues])
    return weights / weights.mean() if len(weights) else weights


def fit_goal_calibration(prediction, targets, weights, strength):
    """Shrink home/away goal-rate corrections toward one using earlier OOF games."""
    result = {'priorGoals': strength, 'matches': len(targets) // 2}
    for index, side in enumerate(('home', 'away')):
        goals = float(np.sum(targets[index::2] * weights[index::2]))
        expected = float(np.sum(prediction[index::2] * weights[index::2]))
        factor = 1. if strength is None else min(1.25, max(.75, (goals + strength) / (expected + strength)))
        result[side] = {'factor': factor, 'observedGoals': goals, 'predictedGoals': expected}
    return result


def calibrated_rates(prediction, calibration):
    result = np.array(prediction, dtype=float, copy=True)
    for index, side in enumerate(('home', 'away')):
        result[index::2] *= calibration[side]['factor']
    return np.clip(result, .05, 8.)


def _weighted_loss(prediction, targets, weights):
    losses = prediction - targets * np.log(prediction)
    return float(np.average(losses, weights=weights))


def _oof_calibration(raw, y, times, leagues, cols, penalty, power, strength, end):
    """Expanding predictions inside the fitting period; never sees the outer test."""
    from .forecast import _fit, _prepare
    predictions, observed, available_weights = [], [], []
    for left, right in ((.5, .65), (.65, .8), (.8, 1.)):
        start = _boundary(times[:end // 2], left) * 2
        stop = _boundary(times[:end // 2], right) * 2 if right < 1 else end
        train_weights = np.repeat(league_weights(leagues[:start // 2], power), 2)
        x, _, _, _ = _prepare(raw, start, cols, train_weights)
        beta = _fit(x[:start], y[:start], penalty, train_weights)
        predictions.extend(np.exp(np.clip(x[start:stop] @ beta, math.log(.05), math.log(8.))))
        observed.extend(y[start:stop])
        available_weights.extend(np.repeat(league_weights(leagues[start // 2:stop // 2], power, leagues[:start // 2]), 2))
    return fit_goal_calibration(np.array(predictions), np.array(observed), np.array(available_weights), strength)


def fit_model(matches, strategies=None, metric_groups=None, league_balance=True, calibrate=True):
    from .forecast import utc, _fit, _prepare, MIN_TRAINING, WINDOW, fit_half_model
    from collections import Counter
    matches = sorted(matches, key=lambda m: (utc(m['at']), m['id']))
    best = None
    audit = []
    for strategy in strategies or STRATEGIES:
        raw, y, times, leagues, ids, histories, totals, ratings, names = examples(matches, strategy)
        if len(times) < MIN_TRAINING:
            return None
        train, calibration_end, tune = (_boundary(times, fraction) * 2 for fraction in (.5, .6, .8))
        core = len(CORE) + (len(VENUE) if strategy in ('venue', 'schedule') else 0) + (len(SCHEDULE) if strategy == 'schedule' else 0)
        seen = set()
        for group in metric_groups or ('goals', 'basic', 'xg', 'pressure', 'advanced', 'all'):
            allowed = {'goals': (), 'basic': LEGACY_STATS, 'xg': LEGACY_STATS + ADVANCED_STATS[:5],
                'pressure': LEGACY_STATS + PRESSURE_STATS, 'advanced': LEGACY_STATS + ADVANCED_STATS, 'all': STAT_NAMES}[group]
            cols = list(range(core)) + [i for i in range(core, len(names)) if names[i].split(':', 1)[-1] in allowed and np.isfinite(raw[:train, i]).mean() >= .4]
            if tuple(cols) in seen:
                continue
            seen.add(tuple(cols))
            for power in ((.5, 1.) if league_balance else (0.,)):
                train_weights = np.repeat(league_weights(leagues[:train // 2], power), 2)
                tuning_weights = np.repeat(league_weights(leagues[calibration_end // 2:tune // 2], .5 if league_balance else 0., leagues[:train // 2]), 2)
                cal_weights = np.repeat(league_weights(leagues[train // 2:calibration_end // 2], power, leagues[:train // 2]), 2)
                x, _, _, _ = _prepare(raw, train, cols, train_weights)
                candidate = None
                for penalty in (20., 80., 320.):
                    beta = _fit(x[:train], y[:train], penalty, train_weights)
                    cal_pred = np.exp(np.clip(x[train:calibration_end] @ beta, math.log(.05), math.log(8.)))
                    tuning_pred = np.exp(np.clip(x[calibration_end:tune] @ beta, math.log(.05), math.log(8.)))
                    for strength in ((25., 100., None) if calibrate else (None,)):
                        calibration = fit_goal_calibration(cal_pred, y[train:calibration_end], cal_weights, strength)
                        pred = calibrated_rates(tuning_pred, calibration)
                        loss = _weighted_loss(pred, y[calibration_end:tune], tuning_weights)
                        if candidate is None or loss < candidate[0]:
                            candidate = (loss, penalty, strength)
                loss, penalty, strength = candidate
                audit.append({'strategy': strategy, 'metricGroup': group, 'poissonLoss': loss, 'penalty': penalty,
                    'leagueBalancePower': power, 'calibrationPriorGoals': strength})
                if best is None or loss < best[0]:
                    best = (loss, strategy, group, cols, penalty, power, strength,
                        (raw, y, times, leagues, ids, histories, totals, ratings, names), train, calibration_end, tune)
    loss, strategy, group, cols, penalty, power, strength, data, train, calibration_end, tune = best
    raw, y, times, leagues, ids, histories, totals, ratings, names = data
    # Outer test comes after all settings and calibration-strength selection.
    weights = np.repeat(league_weights(leagues[:tune // 2], power), 2)
    x, _, _, _ = _prepare(raw, tune, cols, weights)
    beta = _fit(x[:tune], y[:tune], penalty, weights)
    held_calibration = _oof_calibration(raw, y, times, leagues, cols, penalty, power, strength, tune)
    held_out = calibrated_rates(np.exp(np.clip(x[tune:] @ beta, math.log(.05), math.log(8.))), held_calibration)
    validation = metrics(held_out, y[tune:])
    by_league = {}
    for league in set(leagues[tune // 2:]):
        positions = [p for i, value in enumerate(leagues[tune // 2:]) if value == league for p in (2 * i, 2 * i + 1)]
        by_league[str(league)] = metrics(held_out[positions], y[tune:][positions])
    # Refit after evaluation. This final artifact can use every available match.
    calibration = _oof_calibration(raw, y, times, leagues, cols, penalty, power, strength, len(y))
    weights = np.repeat(league_weights(leagues, power), 2)
    x, medians, mean, scale = _prepare(raw, len(raw), cols, weights)
    beta = _fit(x, y, penalty, weights)
    used_stats = {names[i].split(':', 1)[-1] for i in cols if ':' in names[i]}
    compact_histories = {}
    for team, history in histories.items():
        compact_histories[str(team)] = [{**row, **{side: {k: v for k, v in row[side].items() if k in used_stats}
            for side in ('own', 'opponent')}} for row in history[-WINDOW:]]
    return {'featureSchema': 3, 'strategy': strategy, 'metricGroup': group, 'rho': 0.,
        'leagueBalancePower': power, 'goalRateCalibration': calibration, 'dailyHistoryRefresh': True,
        'columns': cols, 'featureNames': names, 'features': [names[i] for i in cols],
        'medians': medians.tolist(), 'mean': mean.tolist(), 'scale': scale.tolist(), 'beta': beta.tolist(),
        'metricAudit': {'candidateUniqueMetrics': list(STAT_NAMES), 'selectedUniqueMetrics': sorted(used_stats),
            'pressureMinPairedMinutes': 60, 'teamHistoryWindow': WINDOW, 'decayHalfLifeMatches': 5,
            'venuePriorMatches': 5, 'restScheduleScope': 'completed fixtures in covered current-season competitions'},
        'trainingMatches': len(times), 'validationMatches': len(y[tune:]) // 2, 'inputMatches': len(matches),
        'trainingByCompetition': {str(k): v for k, v in Counter(leagues).items()}, 'usesTeamStats': group != 'goals',
        'validationLoss': validation['scoreLogLoss'], 'validationBrier': validation['brier'],
        'validationMetrics': validation, 'validationByCompetition': by_league,
        'validationProtocol': {'trainMatches': train // 2, 'calibrationMatches': (calibration_end - train) // 2,
            'tuningMatches': (tune - calibration_end) // 2, 'testMatches': (len(y) - tune) // 2,
            'testStartsAt': times[tune // 2], 'testFixtureIds': ids[tune // 2:], 'completionLagHours': 3},
        'selectionAudit': audit, 'regularization': penalty,
        'halfTimeModel': fit_half_model(matches, league_balance_power=power),
        'leagueTotals': {str(k): v for k, v in totals.items()},
        'ratings': {str(k): v for k, v in ratings.items()}, 'histories': compact_histories}


def rate(model, own, opponent, home, context):
    raw = np.array(values(own, opponent, home, context, model['strategy'], model['featureNames']), dtype=float)[model['columns']]
    missing = ~np.isfinite(raw)
    raw = np.where(missing, model['medians'], raw)
    x = np.concatenate([[1.], (raw - model['mean']) / model['scale'], missing.astype(float)])
    value = float(np.exp(np.clip(x @ np.array(model['beta']), math.log(.05), math.log(8.))))
    factor = model.get('goalRateCalibration', {}).get('home' if home else 'away', {}).get('factor', 1.)
    return min(8., max(.05, value * factor))


def promotion_check(matches, candidate):
    """Gate weekly updates against the existing method on all covered leagues.

    This is a release check, not another hyperparameter-selection step. Freeze
    histories at the holdout boundary to reproduce saved-model serving.
    """
    if candidate.get('refinedEnsemble'):
        from .forecast_refined import promotion_check as check
        return check(matches, candidate)
    from . import forecast as f
    matches = sorted(matches, key=lambda m: (f.utc(m['at']), m['id']))
    covered_leagues = {match['league'] for match in matches}
    cutoff = f.utc(candidate['validationProtocol']['testStartsAt'])
    prior = [m for m in matches if f.utc(m['at']) + timedelta(hours=3) <= cutoff]
    baseline = f._fit_model_legacy(prior)
    if baseline is None:
        return {'passed': False, 'reason': 'insufficient_baseline_history'}
    # Reuse the actual eligible-example league sequence, not raw fixture counts.
    raw, y, times, eligible_leagues, _, histories, totals, ratings, _ = examples(prior, candidate['strategy'])
    power = candidate.get('leagueBalancePower', 0.)
    weights = np.repeat(league_weights(eligible_leagues, power), 2)
    x, medians, mean, scale = f._prepare(raw, len(raw), candidate['columns'], weights)
    fitted = {**candidate, 'beta': f._fit(x, y, candidate['regularization'], weights).tolist(),
        'medians': medians.tolist(), 'mean': mean.tolist(), 'scale': scale.tolist(),
        'histories': {str(k): v[-f.WINDOW:] for k, v in histories.items()},
        'leagueTotals': {str(k): v for k, v in totals.items()}, 'ratings': {str(k): v for k, v in ratings.items()},
        'halfTimeModel': f.fit_half_model(prior, league_balance_power=power)}
    if 'goalRateCalibration' in candidate:
        strength = candidate['goalRateCalibration']['priorGoals']
        fitted['goalRateCalibration'] = _oof_calibration(raw, y, times, eligible_leagues,
            candidate['columns'], candidate['regularization'], power, strength, len(y))
    models = {'baseline': baseline, 'candidate': fitted}
    contexts = {name: ({int(k): v for k, v in model['leagueTotals'].items()}, {int(k): v for k, v in model['ratings'].items()})
                for name, model in models.items()}
    # Candidate contexts must also stop at the evaluation cutoff.
    contexts['candidate'] = (totals, ratings)
    rates = {name: [] for name in models}; goals = []
    refreshed, refreshed_at = fitted, None
    for match in matches:
        if match['league'] not in covered_leagues or f.utc(match['at']) < cutoff:
            continue
        if candidate.get('dailyHistoryRefresh'):
            from .forecast_history import apply_matches, daily_boundary
            boundary = daily_boundary(f.utc(match['at']))
            if boundary > cutoff and refreshed_at != boundary:
                settled = [m for m in matches if cutoff <= f.utc(m['at']) + timedelta(hours=3) <= boundary]
                refreshed = apply_matches(fitted, settled, boundary)
                refreshed_at = boundary
            models['candidate'] = refreshed
            contexts['candidate'] = ({int(k): v for k, v in refreshed['leagueTotals'].items()}, {int(k): v for k, v in refreshed['ratings'].items()})
        if any(min(len(model['histories'].get(str(match[side]), [])) for side in ('home', 'away')) < f.MIN_HISTORY for model in models.values()):
            continue
        for name, model in models.items():
            own, opp = (model['histories'][str(match[side])] for side in ('home', 'away'))
            totals_for_model, ratings_for_model = contexts[name]
            for a, b, home, team, other in ((own, opp, True, match['home'], match['away']), (opp, own, False, match['away'], match['home'])):
                context = {**f._context(totals_for_model, ratings_for_model, match['league'], team, other), 'kickoff': match['at']}
                rates[name].append(f._rate(model, a, b, home, context))
        goals.extend((match['hg'], match['ag']))
    if len(goals) < 100:
        return {'passed': False, 'reason': 'insufficient_promotion_test_matches', 'matches': len(goals) // 2}
    result = {name: metrics(np.array(values), np.array(goals), candidate['rho'] if name == 'candidate' else 0.) for name, values in rates.items()}
    before, after = result['baseline'], result['candidate']
    passed = (after['brier'] <= before['brier'] and after['scoreLogLoss'] <= before['scoreLogLoss']
              and after['exactScoreAccuracy'] >= before['exactScoreAccuracy'])
    return {'passed': passed, 'reason': 'passed' if passed else 'forecast_quality_regression',
            'cutoff': cutoff.isoformat(), 'comparisonScope': 'all covered leagues on identical fixtures', 'baseline': before, 'candidate': after}
