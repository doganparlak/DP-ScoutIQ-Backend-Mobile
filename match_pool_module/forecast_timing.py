"""Offline timing challengers and portable inference; no automatic activation.

The full-match goal model is never fitted or modified here. Runtime features
require only the existing last-ten-match history and league half totals.
"""
import math
import numpy as np

KIND = 'fixture_timing_v1'


def logit(p):
    p = np.clip(p, .02, .98)
    return np.log(p / (1 - p))


def features(own, opponent, home, prior):
    """Features available before kickoff, with sparse evidence shrunk to prior."""
    values = [float(home)]
    for history, venue in ((own[-10:], home), (opponent[-10:], not home)):
        for window, same_venue in ((5, False), (10, False), (10, True)):
            rows = history[-window:]
            if same_venue:
                rows = [r for r in rows if r.get('home') == venue]
            for first, full in (('hgf', 'gf'), ('hga', 'ga')):
                available = [r for r in rows if r.get(first) is not None
                             and 0 <= r[first] <= r[full]]
                total = sum(r[full] for r in available)
                share = (sum(r[first] for r in available) + 10 * prior) / (total + 10)
                values.extend([float(logit(share) - logit(prior)),
                               len(available) / 10., total / (total + 10)])
        for key in ('gf', 'ga'):
            values.append(sum(r[key] for r in history) / max(1, len(history)))
    return np.asarray(values, dtype=float)


def predict_share(model, own, opponent, home, prior):
    if model.get('kind') != KIND:
        raise ValueError('Unsupported timing model')
    x = features(own, opponent, home, prior)
    if len(x) != len(model['mean']):
        raise ValueError('Timing feature schema mismatch')
    if model['method'] == 'logistic':
        z = (x - model['mean']) / model['scale']
        residual = model['intercept'] + float(z @ np.asarray(model['coefficients']))
    elif model['method'] == 'tree':
        residual = model['intercept']
        for tree in model['trees']:
            index = 0
            while tree['left'][index] != -1:
                index = tree['left'][index] if x[tree['feature'][index]] <= tree['threshold'][index] else tree['right'][index]
            residual += model['learningRate'] * tree['value'][index]
    else:
        raise ValueError('Unsupported timing inference method')
    return float(1 / (1 + math.exp(-float(np.clip(logit(prior) + model.get('blend', 1.) * residual, -4., 4.)))))


def fit(x, prior, first, full, method='logistic', penalty=10., depth=2, sample_weight=None):
    """Training-only dependencies never enter serving paths."""
    x, prior, first, full = map(np.asarray, (x, prior, first, full))
    weights = np.ones(len(full)) if sample_weight is None else np.asarray(sample_weight, dtype=float)
    usable = full > 0
    x, prior, first, full = (a[usable] for a in (x, prior, first, full))
    weights = weights[usable]
    weights = weights / weights.mean()
    mean = np.average(x, axis=0, weights=weights)
    scale = np.sqrt(np.average((x-mean)**2, axis=0, weights=weights))
    scale = np.where(scale > 1e-8, scale, 1.)
    base = {'kind': KIND, 'method': method, 'mean': mean.tolist(), 'scale': scale.tolist()}
    if method == 'logistic':
        from scipy.optimize import minimize
        z = np.column_stack([np.ones(len(x)), (x - mean) / scale])
        offset = logit(prior)
        def objective(beta):
            eta = offset + z @ beta
            p = 1 / (1 + np.exp(-np.clip(eta, -40, 40)))
            loss = np.sum(weights * (full * np.logaddexp(0, eta) - first * eta)) + .5 * penalty * np.sum(beta ** 2)
            gradient = z.T @ (weights * (full * p - first)) + penalty * beta
            return loss, gradient
        result = minimize(objective, np.zeros(z.shape[1]), jac=True, method='L-BFGS-B', options={'maxiter': 500, 'ftol': 1e-11})
        if not result.success:
            raise ValueError('Timing optimizer did not converge: ' + str(result.message))
        return {**base, 'intercept': float(result.x[0]), 'coefficients': result.x[1:].tolist(), 'penalty': penalty}
    from sklearn.ensemble import GradientBoostingRegressor
    # Smoothed empirical logits avoid infinite targets from all-first/all-second games.
    target = logit((first + .5) / (full + 1)) - logit(prior)
    estimator = GradientBoostingRegressor(n_estimators=80, max_depth=depth,
        min_samples_leaf=80, learning_rate=.03, loss='huber', random_state=20261104)
    estimator.fit(x, target, sample_weight=full * weights)
    trees = []
    for row in estimator.estimators_:
        tree = row[0].tree_
        trees.append({'left': tree.children_left.tolist(), 'right': tree.children_right.tolist(),
                      'feature': tree.feature.tolist(), 'threshold': tree.threshold.tolist(),
                      'value': tree.value[:, 0, 0].tolist()})
    return {**base, 'intercept': float(estimator.init_.constant_.ravel()[0]),
            'trees': trees, 'learningRate': .03, 'depth': depth}


def predict_matrix(model,x,prior):
    """Batch form of the exact portable serving transform."""
    x=np.asarray(x,float);prior=np.asarray(prior,float)
    if model.get('kind')!=KIND:raise ValueError('Unsupported timing model')
    if model['method']=='logistic':
        residual=model['intercept']+((x-model['mean'])/model['scale'])@np.asarray(model['coefficients'])
    elif model['method']=='tree':
        residual=np.full(len(x),model['intercept'],dtype=float)
        for tree in model['trees']:
            for row,values in enumerate(x):
                node=0
                while tree['left'][node]!=-1:
                    node=tree['left'][node]if values[tree['feature'][node]]<=tree['threshold'][node]else tree['right'][node]
                residual[row]+=model['learningRate']*tree['value'][node]
    else:raise ValueError('Unsupported timing inference method')
    return 1/(1+np.exp(-np.clip(logit(prior)+model.get('blend',1.)*residual,-4,4)))
