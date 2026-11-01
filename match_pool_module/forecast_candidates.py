"""Explicit offline forecast challengers; never activated by report endpoints.

All features use settled, current-season matches. Artifacts are plain JSON and
inference needs only NumPy. Scikit-learn is an optional offline training tool.
"""
from collections import defaultdict
from datetime import timedelta
import math
import numpy as np

from . import forecast as f
from . import forecast_learning as l

BLOCKS = ('ratings', 'windows', 'league_home', 'red_cards')
FORM_KEYS = ('gf', 'ga', 'own:Expected Goals', 'opponent:Expected Goals',
             'own:Shots On Target', 'opponent:Shots On Target', 'own:Pressure Net')
HALF_KEYS = ('hgf', 'hga', 'second_gf', 'second_ga')


def card_context(events, home, away):
    result = {'known': bool(events.get('available')), 'exposure': {}, 'early': {}}
    for team in (home, away):
        minutes = []
        for event in events.get('reds') or []:
            if event.get('team') != team or event.get('minute') is None:
                continue
            minute = float(event['minute']) + float(event.get('extra') or 0)
            if 0 <= minute <= 60:
                minutes.append(minute)
        result['early'][str(team)] = len(minutes)
        result['exposure'][str(team)] = min(1., sum((90 - minute) / 90 for minute in minutes))
    return result


def _value(row, key):
    if key == 'second_gf':
        return None if row.get('hgf') is None else row['gf'] - row['hgf']
    if key == 'second_ga':
        return None if row.get('hga') is None else row['ga'] - row['hga']
    if ':' in key:
        side, name = key.split(':', 1)
        return row.get(side, {}).get(name)
    return row.get(key)


def _mean(rows, key, red=False):
    available = []
    for row in rows:
        value = _value(row, key)
        if value is None or not math.isfinite(float(value)):
            continue
        weight = 1 - .7 * row.get('red_exposure', 0.) if red else 1.
        available.append((float(value), weight))
    if not available or (':' in key and len(available) < max(3, math.ceil(len(rows) * .6))):
        return None
    return sum(v * w for v, w in available) / sum(w for _, w in available)


class History:
    def __init__(self):
        self.histories = defaultdict(list)
        self.totals = {}
        self.ratings = {}
        self.strengths = {}
        self.half_totals = {}
        self.seen = set()
        self.last_available = None

    def consume(self, match):
        if match['id'] in self.seen:
            return
        home, away = match['home'], match['away']
        context = f._context(self.totals, self.ratings, match['league'], home, away)
        ah, dh = self.strengths.get(home, (0., 0.))
        aa, da = self.strengths.get(away, (0., 0.))
        eh = np.clip(context['league_home_goals'] * math.exp(ah + da), .1, 8.)
        ea = np.clip(context['league_away_goals'] * math.exp(aa + dh), .1, 8.)
        rh, ra = float(np.clip(match['hg'] - eh, -3., 3.)), float(np.clip(match['ag'] - ea, -3., 3.))
        self.strengths[home] = (.998 * ah + .04 * rh, .998 * dh + .04 * ra)
        self.strengths[away] = (.998 * aa + .04 * ra, .998 * da + .04 * rh)
        l.append_match(self.histories, match, self.ratings)
        cards = match.get('cardContext') or {}
        for team, other in ((home, away), (away, home)):
            row = self.histories[team][-1]
            exposure = cards.get('exposure', {})
            row.update(red_known=cards.get('known', False),
                       red_exposure=max(exposure.get(str(team), 0.), exposure.get(str(other), 0.)),
                       early_own=cards.get('early', {}).get(str(team), 0),
                       early_opp=cards.get('early', {}).get(str(other), 0))
        f._update_context(match, self.totals, self.ratings)
        f._update_half_totals(match, self.half_totals)
        self.seen.add(match['id'])
        available = f.utc(match['at']) + timedelta(hours=3)
        self.last_available = max(self.last_available, available) if self.last_available else available

    def dump(self):
        return {'histories': dict(self.histories), 'totals': self.totals, 'ratings': self.ratings,
                'strengths': self.strengths, 'halfTotals': self.half_totals, 'seen': sorted(self.seen),
                'lastAvailableAt': self.last_available.isoformat() if self.last_available else None}

    @classmethod
    def restore(cls, payload):
        result = cls()
        result.histories = defaultdict(list, {int(k): list(v) for k, v in payload['histories'].items()})
        result.totals = {int(k): list(v) for k, v in payload['totals'].items()}
        result.ratings = {int(k): v for k, v in payload['ratings'].items()}
        result.strengths = {int(k): v for k, v in payload['strengths'].items()}
        result.half_totals = {int(k): list(v) for k, v in payload['halfTotals'].items()}
        result.seen = set(payload['seen'])
        result.last_available = f.utc(payload['lastAvailableAt']) if payload.get('lastAvailableAt') else None
        return result


def feature_names(strategy):
    names = l.schema(strategy, l.STAT_NAMES)
    blocks = {}
    def add(block, values):
        blocks[block] = list(range(len(names), len(names) + len(values)))
        names.extend(values)
    add('ratings', ['attack_strength', 'defence_weakness', 'opp_attack_strength', 'opp_defence_weakness'])
    add('windows', [f'{side}_{window}:{key}' for side in ('team', 'opp') for window in (5, 10, 'season') for key in FORM_KEYS])
    add('league_home', ['venue_scoring_prior', 'opponent_venue_scoring_prior', 'league_home_advantage'])
    add('red_cards', [f'{side}_red_weighted:{key}' for side in ('team', 'opp') for key in FORM_KEYS[:6]] +
        [f'{side}_{kind}_early_card_frequency' for side in ('team', 'opp') for kind in ('own', 'opp')])
    add('halves', [f'{side}_{window}:{key}' for side in ('team', 'opp') for window in (5, 10, 'season') for key in HALF_KEYS] + ['league_first_half_share', 'opponent_first_half_share'])
    return names, blocks


def features(state, team, other, home, league, kickoff, strategy):
    own, opponent = state.histories[team], state.histories[other]
    context = {**f._context(state.totals, state.ratings, league, team, other), 'kickoff': kickoff}
    values = l.values(own, opponent, home, context, strategy, l.schema(strategy, l.STAT_NAMES))
    values += [*state.strengths.get(team, (0., 0.)), *state.strengths.get(other, (0., 0.))]
    for rows in (own, opponent):
        for window in (5, 10, None):
            sample = rows[-window:] if window else rows
            values.extend(_mean(sample, key) for key in FORM_KEYS)
    lh, la = context['league_home_goals'], context['league_away_goals']
    values += [lh if home else la, la if home else lh, (1 if home else -1) * math.log(lh / la)]
    for rows in (own, opponent):
        values.extend(_mean(rows[-10:], key, red=True) for key in FORM_KEYS[:6])
    for rows in (own, opponent):
        known = [row for row in rows[-10:] if row.get('red_known')]
        values.extend(sum(row.get('early_' + side, 0) > 0 for row in known) / len(known) if known else None for side in ('own', 'opp'))
    for rows in (own, opponent):
        for window in (5, 10, None):
            sample = rows[-window:] if window else rows
            values.extend(_mean(sample, key) for key in HALF_KEYS)
    values += [f._half_prior(state.half_totals, league, home), f._half_prior(state.half_totals, league, not home)]
    return values


def examples(matches, strategy):
    state = History()
    raw, goals, first, times, leagues, ids, target_card_weights = [], [], [], [], [], [], []
    pending, index = [], 0
    for match in sorted(matches, key=lambda m: (f.utc(m['at']), m['id'])):
        while index < len(pending) and f.utc(pending[index]['at']) + timedelta(hours=3) <= f.utc(match['at']):
            state.consume(pending[index]); index += 1
        if min(len(state.histories[match['home']]), len(state.histories[match['away']])) >= f.MIN_HISTORY:
            for side, other, home, goal, half in (('home', 'away', True, 'hg', 'hhg'), ('away', 'home', False, 'ag', 'hag')):
                raw.append(features(state, match[side], match[other], home, match['league'], match['at'], strategy))
                goals.append(match[goal]); first.append(match.get(half))
            times.append(match['at']); leagues.append(match['league']); ids.append(match['id'])
            exposure = max((match.get('cardContext') or {}).get('exposure', {}).values(), default=0.)
            target_card_weights.extend([1 - .5 * exposure] * 2)
        pending.append(match)
    for match in pending[index:]:
        state.consume(match)
    names, blocks = feature_names(strategy)
    return {'raw': np.array(raw, dtype=float), 'goals': np.array(goals, dtype=float), 'first': np.array(first, dtype=float),
            'times': times, 'leagues': leagues, 'ids': ids, 'cardWeights': np.array(target_card_weights),
            'names': names, 'blocks': blocks, 'state': state}


def _matrix(raw, cols, weights):
    x, medians, mean, scale = f._prepare(raw, len(raw), cols, weights)
    return x, {'medians': medians.tolist(), 'mean': mean.tolist(), 'scale': scale.tolist()}


def _transform(raw, model):
    selected = np.asarray(raw, dtype=float)[:, model['columns']]
    missing = ~np.isfinite(selected)
    selected = np.where(missing, model['medians'], selected)
    return np.column_stack([np.ones(len(raw)), (selected - model['mean']) / model['scale'], missing.astype(float)])


def linear_predict(model, raw):
    rates = np.exp(np.clip(_transform(raw, model) @ np.array(model['beta']), math.log(.05), math.log(8.)))
    return l.calibrated_rates(rates, model.get('calibration', {'home': {'factor': 1.}, 'away': {'factor': 1.}}))


def _loss(rates, goals):
    return float(np.mean(rates - goals * np.log(np.maximum(rates, 1e-12))))


def fit_linear(data, baseline, extra_blocks):
    raw, y = data['raw'], data['goals']
    end = l._boundary(data['times'], .8) * 2
    cols = [data['names'].index(name) for name in baseline['features']]
    for block in extra_blocks:
        cols.extend(i for i in data['blocks'][block] if np.isfinite(raw[:end, i]).mean() >= .4)
    cols = list(dict.fromkeys(cols))
    power = baseline['leagueBalancePower']
    weights = np.repeat(l.league_weights(data['leagues'][:end // 2], power), 2)
    if 'red_cards' in extra_blocks:
        weights *= data['cardWeights'][:end]
    x, prep = _matrix(raw[:end], cols, weights)
    validation_x = _transform(raw[end:], {**prep, 'columns': cols})
    candidates = []
    for penalty in (80., 320., 1280.):
        beta = f._fit(x, y[:end], penalty, weights)
        predicted = np.exp(np.clip(validation_x @ beta, math.log(.05), math.log(8.)))
        candidates.append((_loss(predicted, y[end:]), penalty, predicted))
    loss, penalty, predicted = min(candidates, key=lambda row: row[0])
    all_weights = np.repeat(l.league_weights(data['leagues'], power), 2)
    if 'red_cards' in extra_blocks:
        all_weights *= data['cardWeights']
    x, prep = _matrix(raw, cols, all_weights)
    beta = f._fit(x, y, penalty, all_weights)
    return {'kind': 'linear', 'columns': cols, 'featureNames': data['names'], **prep,
            'beta': beta.tolist(), 'regularization': penalty, 'blocks': list(extra_blocks),
            'validationLoss': loss, 'selectionEndsBefore': data['times'][-1]}, predicted


def baseline_oof(data, baseline):
    end = l._boundary(data['times'], .8) * 2
    cols = [data['names'].index(name) for name in baseline['features']]
    weights = np.repeat(l.league_weights(data['leagues'][:end // 2], baseline['leagueBalancePower']), 2)
    x, prep = _matrix(data['raw'][:end], cols, weights)
    beta = f._fit(x, data['goals'][:end], baseline['regularization'], weights)
    model = {'columns': cols, **prep, 'beta': beta.tolist(), 'calibration': baseline['goalRateCalibration']}
    return linear_predict(model, data['raw'][end:])


def export_trees(estimator):
    trees = []
    for predictors in estimator._predictors:
        tree = []
        for node in predictors[0].nodes:
            if node['is_categorical']:
                raise ValueError('Only numeric candidate trees may be exported')
            tree.append([float(node['value']), int(node['feature_idx']), float(node['num_threshold']),
                         bool(node['missing_go_to_left']), int(node['left']), int(node['right']), bool(node['is_leaf'])])
        trees.append(tree)
    return {'offset': float(estimator._baseline_prediction[0, 0]), 'trees': trees}


def tree_predict(model, raw):
    x = np.asarray(raw, dtype=float)[:, model['columns']]
    eta = np.full(len(x), model['offset'])
    for tree in model['trees']:
        for index, row in enumerate(x):
            node_index = 0
            while True:
                value, feature, threshold, missing_left, left, right, leaf = tree[node_index]
                if leaf:
                    eta[index] += value
                    break
                number = row[feature]
                node_index = left if (missing_left if np.isnan(number) else number <= threshold) else right
    return np.exp(np.clip(eta, math.log(.05), math.log(8.)))


def fit_nonlinear(data, baseline):
    # Training dependency is deliberately local to this explicit offline call.
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn import __version__ as sklearn_version
    raw, y = data['raw'], data['goals']
    end = l._boundary(data['times'], .8) * 2
    cols = [data['names'].index(name) for name in baseline['features']]
    cols += [i for block in BLOCKS for i in data['blocks'][block] if np.isfinite(raw[:end, i]).mean() >= .4]
    cols = list(dict.fromkeys(cols))
    weights = np.repeat(l.league_weights(data['leagues'][:end // 2], baseline['leagueBalancePower']), 2) * data['cardWeights'][:end]
    options = [(2, 40, 200, 100.), (2, 80, 100, 30.), (3, 60, 100, 100.)]
    results = []
    for depth, iterations, leaf, regularization in options:
        estimator = HistGradientBoostingRegressor(loss='poisson', max_depth=depth, max_leaf_nodes=2 ** depth,
            max_iter=iterations, min_samples_leaf=leaf, l2_regularization=regularization,
            learning_rate=.05, max_bins=64, early_stopping=False, categorical_features=None, random_state=20261101)
        estimator.fit(raw[:end, cols], y[:end], sample_weight=weights)
        prediction = np.clip(estimator.predict(raw[end:, cols]), .05, 8.)
        exported = {'columns': cols, **export_trees(estimator)}
        np.testing.assert_allclose(tree_predict(exported, raw[end:]), prediction, rtol=1e-11, atol=1e-11)
        results.append((_loss(prediction, y[end:]), (depth, iterations, leaf, regularization), prediction))
    loss, option, prediction = min(results, key=lambda item: item[0])
    depth, iterations, leaf, regularization = option
    estimator = HistGradientBoostingRegressor(loss='poisson', max_depth=depth, max_leaf_nodes=2 ** depth,
        max_iter=iterations, min_samples_leaf=leaf, l2_regularization=regularization,
        learning_rate=.05, max_bins=64, early_stopping=False, categorical_features=None, random_state=20261101)
    weights = np.repeat(l.league_weights(data['leagues'], baseline['leagueBalancePower']), 2) * data['cardWeights']
    estimator.fit(raw[:, cols], y, sample_weight=weights)
    model = {'kind': 'nonlinear', 'columns': cols, 'featureNames': data['names'], **export_trees(estimator),
        'validationLoss': loss, 'hyperparameters': {'depth': depth, 'trees': iterations, 'minLeaf': leaf, 'l2': regularization},
        'trainingLibrary': 'scikit-learn ' + sklearn_version}
    np.testing.assert_allclose(tree_predict(model, raw), np.clip(estimator.predict(raw[:, cols]), .05, 8.), rtol=1e-11, atol=1e-11)
    return model, prediction


def fit_ensemble(predictions, targets, three=False):
    names = ['incumbent', 'combined', 'nonlinear'] if three else ['incumbent', 'nonlinear']
    candidates = []
    if three:
        grids = [(a / 4, b / 4, (4 - a - b) / 4) for a in range(5) for b in range(5 - a)]
    else:
        grids = [(1 - weight, weight) for weight in (0., .1, .25, .5, .75, 1.)]
    for weights in grids:
        blend = sum(weight * predictions[name] for name, weight in zip(names, weights))
        candidates.append((_loss(blend, targets), weights))
    loss, weights = min(candidates, key=lambda item: item[0])
    return {'kind': 'ensemble', 'weights': dict(zip(names, weights)), 'validationLoss': loss}


def half_from_shares(home_rate, away_rate, home_share, away_share):
    first = home_rate * home_share + away_rate * away_share
    comparison = f.score_distribution(first, home_rate + away_rate - first)['outcomes']
    return {'moreGoalsHalf': {'first': comparison['home'], 'equal': comparison['draw'], 'second': comparison['away']},
            'firstHalfBothTeamsScore': (1 - math.exp(-home_rate * home_share)) * (1 - math.exp(-away_rate * away_share))}


def half_shares(model, raw):
    first = linear_predict(model['first'], raw)
    second = linear_predict(model['second'], raw)
    return np.clip(first / (first + second), .1, .9)


def fit_halves(data, baseline):
    raw, full, first = data['raw'], data['goals'], data['first']
    if not np.all(np.isfinite(first) & (first >= 0) & (first <= full)):
        raise ValueError('First-half candidates require valid regulation half-time scores')
    end = l._boundary(data['times'], .8) * 2
    cols = [data['names'].index(name) for name in baseline['features']]
    cols += [i for i in data['blocks']['halves'] if np.isfinite(raw[:end, i]).mean() >= .4]
    cols = list(dict.fromkeys(cols))
    weights = np.repeat(l.league_weights(data['leagues'][:end // 2], baseline['leagueBalancePower']), 2)
    x, prep = _matrix(raw[:end], cols, weights)
    vx = _transform(raw[end:], {**prep, 'columns': cols})
    candidates = []
    for penalty in (80., 320., 1280.):
        bf = f._fit(x, first[:end], penalty, weights)
        bs = f._fit(x, full[:end] - first[:end], penalty, weights)
        a, b = np.exp(np.clip(vx @ bf, -5, 3)), np.exp(np.clip(vx @ bs, -5, 3))
        share = np.clip(a / (a + b), .1, .9)
        loss = float(np.mean(-first[end:] * np.log(share) - (full[end:] - first[end:]) * np.log(1 - share)))
        candidates.append((loss, penalty))
    loss, penalty = min(candidates)
    weights = np.repeat(l.league_weights(data['leagues'], baseline['leagueBalancePower']), 2)
    x, prep = _matrix(raw, cols, weights)
    first_model = {'kind': 'linear', 'columns': cols, **prep, 'beta': f._fit(x, first, penalty, weights).tolist()}
    second_model = {'kind': 'linear', 'columns': cols, **prep, 'beta': f._fit(x, full - first, penalty, weights).tolist()}
    return {'kind': 'direct_halves', 'first': first_model, 'second': second_model, 'validationLoss': loss, 'regularization': penalty}


def fit_options(matches, baseline):
    """Train a reviewable artifact without changing any database/model state."""
    data = examples(matches, baseline['strategy'])
    models, validation = {}, {'incumbent': baseline_oof(data, baseline)}
    for name, blocks in (('ratings', ('ratings',)), ('windows', ('windows',)),
                         ('league_home', ('league_home',)), ('red_cards', ('red_cards',)), ('combined', BLOCKS)):
        models[name], predicted = fit_linear(data, baseline, blocks)
        if name == 'combined':
            validation['combined'] = predicted
    models['nonlinear'], validation['nonlinear'] = fit_nonlinear(data, baseline)
    end = l._boundary(data['times'], .8) * 2
    models['ensemble_two'] = fit_ensemble(validation, data['goals'][end:])
    models['ensemble_three'] = fit_ensemble(validation, data['goals'][end:], three=True)
    models['direct_halves'] = fit_halves(data, baseline)
    return {'schema': 'forecast_challengers_v1', 'status': 'experimental_not_activated',
            'baseModelId': baseline.get('modelId'), 'baseline': baseline, 'options': models,
            'history': data['state'].dump(), 'featureNames': data['names'],
            'inputMatches': len(matches), 'trainingExamples': len(data['ids']),
            'createdAt': f.datetime.now(f.timezone.utc).isoformat(),
            'independentPoisson': True, 'activationPerformed': False}


def forecast_option(artifact, fixture, option, cutoff, state=None):
    """Explicit preview only; no automatic model registry or endpoint dispatch."""
    baseline = artifact['baseline']
    state = state or History.restore(artifact['history'])
    if state.last_available and state.last_available > cutoff:
        raise ValueError('Candidate context crosses prediction cutoff')
    home, away = fixture['homeTeam']['id'], fixture['awayTeam']['id']
    if min(len(state.histories[home]), len(state.histories[away])) < f.MIN_HISTORY:
        return {'status': 'unavailable', 'reason': 'insufficient_history'}
    current = {**baseline, 'histories': {str(k): rows[-f.WINDOW:] for k, rows in state.histories.items()},
               'leagueTotals': {str(k): v for k, v in state.totals.items()},
               'ratings': {str(k): v for k, v in state.ratings.items()},
               'halfTimeModel': {**baseline['halfTimeModel'], 'leagueTotals': {str(k): v for k, v in state.half_totals.items()}}}
    result = f.build_forecast(fixture, cutoff, current)
    if result['status'] != 'ready':
        return result
    raw = np.array([features(state, home, away, True, fixture['league']['id'], fixture['startingAt'], baseline['strategy']),
                    features(state, away, home, False, fixture['league']['id'], fixture['startingAt'], baseline['strategy'])], dtype=float)
    options = artifact['options']
    predictions = {'incumbent': np.array([result['forecast']['home']['projectedGoals'], result['forecast']['away']['projectedGoals']])}
    if option in ('ratings', 'windows', 'league_home', 'red_cards', 'combined'):
        predictions[option] = linear_predict(options[option], raw)
    if option in ('nonlinear', 'ensemble_two', 'ensemble_three', 'ensemble_halves'):
        predictions['nonlinear'] = tree_predict(options['nonlinear'], raw)
    if option in ('ensemble_three', 'ensemble_halves'):
        predictions['combined'] = linear_predict(options['combined'], raw)
    if option in ('ensemble_two', 'ensemble_three', 'ensemble_halves'):
        ensemble = options['ensemble_two' if option == 'ensemble_two' else 'ensemble_three']
        rates = sum(weight * predictions[name] for name, weight in ensemble['weights'].items())
    elif option == 'direct_halves':
        rates = predictions['incumbent']
    elif option in predictions:
        rates = predictions[option]
    else:
        raise ValueError('Unknown forecast challenger')
    if option in ('direct_halves', 'ensemble_halves'):
        half = half_from_shares(*rates, *half_shares(options['direct_halves'], raw))
    else:
        half = f.half_scenarios(*rates, state.histories[home], state.histories[away], fixture['league']['id'], current['halfTimeModel'])
    forecast = result['forecast']
    return {**result, 'experimentalOption': option, 'forecast': {**forecast,
            'home': {**forecast['home'], 'projectedGoals': float(rates[0])},
            'away': {**forecast['away'], 'projectedGoals': float(rates[1])},
            'halfScenarios': half, **f.score_distribution(*rates)}}
