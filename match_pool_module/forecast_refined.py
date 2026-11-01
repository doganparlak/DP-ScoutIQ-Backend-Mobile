"""Refined goal-rate ensemble: portable inference and explicit offline training."""
from datetime import timedelta
from collections import OrderedDict
import base64
import json
import math
from threading import Lock
import zlib
import numpy as np

from . import forecast as f, forecast_candidates as c, forecast_learning as l

KIND = 'refined_rate_ensemble_v1'
_STATE_CACHE = OrderedDict()
_STATE_LOCK = Lock()


def pack_state(state):
    payload = state.dump() if isinstance(state, c.History) else state
    encoded = base64.b64encode(zlib.compress(json.dumps(payload, allow_nan=False,
        separators=(',', ':')).encode(), 6)).decode('ascii')
    return {'encoding': 'zlib-json-v1', 'payload': encoded,
            'lastAvailableAt': payload['lastAvailableAt']}


def restore_state(payload):
    if payload.get('encoding') != 'zlib-json-v1':
        return c.History.restore(payload)
    encoded = payload['payload']
    with _STATE_LOCK:
        decoded = _STATE_CACHE.get(encoded)
    if decoded is None:
        decoded = json.loads(zlib.decompress(base64.b64decode(encoded)))
        with _STATE_LOCK:
            _STATE_CACHE[encoded] = decoded
            _STATE_CACHE.move_to_end(encoded)
            while len(_STATE_CACHE) > 3:
                _STATE_CACHE.popitem(last=False)
    return c.History.restore(decoded)


def _subset(data, n):
    return {key: value[:2*n] if key in ('raw', 'goals', 'first', 'cardWeights') else
            value[:n] if key in ('times', 'leagues', 'ids') else value
            for key, value in data.items() if key != 'state'}


def _linear(data, baseline):
    columns = [data['names'].index(name) for name in baseline['features']]
    weights = np.repeat(l.league_weights(data['leagues'], baseline['leagueBalancePower']), 2)
    x, prep = c._matrix(data['raw'], columns, weights)
    return {'columns': columns, **prep,
            'beta': f._fit(x, data['goals'], baseline['regularization'], weights).tolist(),
            'calibration': baseline['goalRateCalibration']}


def _refit_tree(data, baseline, selected):
    # Optional training dependency never imported by serving/report requests.
    from sklearn.ensemble import HistGradientBoostingRegressor
    params = selected['hyperparameters']
    columns = selected['columns']
    estimator = HistGradientBoostingRegressor(loss='poisson', max_depth=params['depth'],
        max_leaf_nodes=2 ** params['depth'], max_iter=params['trees'],
        min_samples_leaf=params['minLeaf'], l2_regularization=params['l2'],
        learning_rate=.05, max_bins=64, early_stopping=False,
        categorical_features=None, random_state=20261101)
    weights = np.repeat(l.league_weights(data['leagues'], baseline['leagueBalancePower']), 2) * data['cardWeights']
    estimator.fit(data['raw'][:, columns], data['goals'], sample_weight=weights)
    model = {'columns': columns, 'hyperparameters': params, **c.export_trees(estimator)}
    np.testing.assert_allclose(c.tree_predict(model, data['raw']),
        np.clip(estimator.predict(data['raw'][:, columns]), .05, 8.), atol=1e-11, rtol=1e-11)
    return model


def fit_refined(matches, baseline):
    """Same nested recipe as the reviewed rate_blend; no registry mutations."""
    data = c.examples(matches, baseline['strategy'])
    boundary = l._boundary(data['times'], .8)
    inner = _subset(data, boundary)
    linear = _linear(inner, baseline)
    selected, _ = c.fit_nonlinear(inner, baseline)
    validation = data['raw'][2*boundary:]
    predictions = {'incumbent': c.linear_predict(linear, validation),
                   'nonlinear': c.tree_predict(selected, validation)}
    blend = c.fit_ensemble(predictions, data['goals'][2*boundary:])
    nonlinear = _refit_tree(data, baseline, selected)
    state = data['state'].dump()
    # All nonlinear columns are base inputs or the explicit engineered blocks;
    # retain only their underlying unique statistics in the full-season state.
    needed = set(l.LEGACY_STATS + l.PRESSURE_STATS)
    needed.update(name.split(':', 1)[1] for name in baseline['features'] if ':' in name)
    for rows in state['histories'].values():
        for row in rows:
            for side in ('own', 'opponent'):
                row[side] = {key: value for key, value in row[side].items() if key in needed}
    component = {'kind': KIND, 'weights': blend['weights'], 'nonlinear': nonlinear,
        'state': pack_state(state), 'featureNames': data['names'],
        'selection': {'criterion': 'joint_score_probability_quality_no_category_weights',
            'nestedChronologicalValidation': True, 'blendValidationMatches': len(data['ids'])-boundary,
            'blendValidationStartsAt': data['times'][boundary], 'validationLoss': blend['validationLoss']}}
    selected = {name.split(':', 1)[1] for name in baseline['features'] if ':' in name}
    compact = {team: [{**row, **{side: {key: value for key, value in row[side].items() if key in selected}
                for side in ('own', 'opponent')}} for row in rows]
               for team, rows in baseline['histories'].items()}
    return {**baseline, 'histories': compact, 'refinedEnsemble': component,
            'scope': 'current_season_multi_window', 'independentPoisson': True}


def predict_rates(model, fixture, cutoff, base_rates):
    component = model['refinedEnsemble']
    if component['kind'] != KIND:
        raise ValueError('Unsupported refined ensemble')
    weights = component['weights']
    if set(weights) != {'incumbent', 'nonlinear'} or min(weights.values()) < 0 or not math.isclose(sum(weights.values()), 1):
        raise ValueError('Invalid refined ensemble weights')
    if not weights['nonlinear']:
        return np.asarray(base_rates, dtype=float)
    state = restore_state(component['state'])
    if state.last_available and state.last_available > cutoff:
        raise ValueError('Refined ensemble history crosses prediction cutoff')
    home, away = fixture['homeTeam']['id'], fixture['awayTeam']['id']
    if min(len(state.histories[home]), len(state.histories[away])) < f.MIN_HISTORY:
        raise ValueError('Insufficient refined history')
    raw = np.array([c.features(state, home, away, True, fixture['league']['id'], fixture['startingAt'], model['strategy']),
                    c.features(state, away, home, False, fixture['league']['id'], fixture['startingAt'], model['strategy'])], float)
    tree_rates = c.tree_predict(component['nonlinear'], raw)
    return weights['incumbent'] * np.asarray(base_rates) + weights['nonlinear'] * tree_rates


def promotion_check(matches, candidate):
    """Automatic weekly updates must earn promotion; otherwise keep approval."""
    from .forecast_history import apply_matches, daily_boundary
    cutoff = f.utc(candidate['validationProtocol']['testStartsAt'])
    prior = [m for m in matches if f.utc(m['at']) + timedelta(hours=3) <= cutoff]
    baseline = l.fit_model(prior)
    if baseline is None:
        return {'passed': False, 'reason': 'insufficient_baseline_history'}
    baseline.update(asOf=cutoff.isoformat(), version=f.VERSION)
    fitted = fit_refined(prior, baseline)
    models = {'baseline': baseline, 'candidate': fitted}
    rates = {name: [] for name in models}
    goals = []
    last = cutoff
    for match in sorted(matches, key=lambda m: (f.utc(m['at']), m['id'])):
        if f.utc(match['at']) < cutoff:
            continue
        boundary = daily_boundary(f.utc(match['at']))
        if boundary > last:
            settled = [m for m in matches if last < f.utc(m['at']) + timedelta(hours=3) <= boundary]
            models = {name: apply_matches(model, settled, boundary) for name, model in models.items()}
            last = boundary
        if any(min(len(model['histories'].get(str(match[side]), [])) for side in ('home', 'away')) < f.MIN_HISTORY for model in models.values()):
            continue
        fixture = {'startingAt': match['at'], 'league': {'id': match['league']},
                   'homeTeam': {'id': match['home']}, 'awayTeam': {'id': match['away']}}
        for name, model in models.items():
            histories = model['histories'];totals={int(k):v for k,v in model['leagueTotals'].items()};ratings={int(k):v for k,v in model['ratings'].items()}
            predicted=[]
            for team, other, home in ((match['home'],match['away'],True),(match['away'],match['home'],False)):
                context={**f._context(totals,ratings,match['league'],team,other),'kickoff':match['at']}
                predicted.append(f._rate(model,histories[str(team)],histories[str(other)],home,context))
            if name == 'candidate':
                predicted = predict_rates(model,fixture,f.utc(match['at']),predicted)
            rates[name].extend(predicted)
        goals.extend((match['hg'],match['ag']))
    if len(goals) < 100:
        return {'passed':False,'reason':'insufficient_promotion_test_matches','matches':len(goals)//2}
    metrics={name:l.metrics(np.asarray(values),np.asarray(goals)) for name,values in rates.items()}
    before,after=metrics['baseline'],metrics['candidate']
    passed=after['brier']<=before['brier'] and after['scoreLogLoss']<=before['scoreLogLoss'] and after['exactScoreAccuracy']>=before['exactScoreAccuracy']
    return {'passed':passed,'reason':'passed' if passed else 'forecast_quality_regression',
            'cutoff':cutoff.isoformat(),'baseline':before,'candidate':after,'matches':len(goals)//2}
