"""Offline score-grid challengers. Not registered with any live forecast route.

A mixture or outcome correction introduces dependent goal counts. Clients must
read total-goal ranges from the joint grid, rather than convolving marginals.
"""
import math
import numpy as np

MAX_GOALS = 30  # Poisson(8) omitted tail below 2e-9; rates are bounded to eight.


def poisson_grid(rates):
    rates = np.asarray(rates, dtype=float)
    if rates.shape != (2,) or not np.all(np.isfinite(rates)) or np.any(rates <= 0) or np.any(rates > 8):
        raise ValueError('Expected two finite goal rates in (0, 8]')
    goals = np.arange(MAX_GOALS + 1)
    factorial = np.array([math.lgamma(int(k) + 1) for k in goals])
    rows = np.exp(-rates[:, None] + goals * np.log(rates[:, None]) - factorial)
    result = np.outer(*rows)
    return result / result.sum()


def outcome_masks(size):
    home, away = np.indices((size, size))
    return home > away, home == away, home < away


def blend_grids(grids, weights):
    grids, weights = np.asarray(grids, dtype=float), np.asarray(weights, dtype=float)
    if grids.ndim != 3 or grids.shape[1] != grids.shape[2] or weights.shape != (len(grids),):
        raise ValueError('Expected square grids and one weight per grid')
    if not np.all(np.isfinite(grids)) or np.any(grids < 0) or not np.allclose(grids.sum((1, 2)), 1):
        raise ValueError('Each grid must be a probability distribution')
    if not np.all(np.isfinite(weights)) or np.any(weights < 0) or not np.isclose(weights.sum(), 1):
        raise ValueError('Blend weights must be nonnegative and sum to one')
    return np.tensordot(weights, grids, axes=1)


def reconcile_outcomes(grid, classifier, weight):
    """Preserve within-outcome scores while softly adjusting outcome mass."""
    grid = np.asarray(grid, dtype=float)
    probability = np.asarray(classifier, dtype=float)
    if not 0 <= weight <= 1 or probability.shape != (3,) or not np.all(np.isfinite(probability)) or np.any(probability < 0) or not np.isclose(probability.sum(), 1):
        raise ValueError('Expected home/draw/away probabilities and a weight in [0, 1]')
    result = grid.copy()
    for mask, target in zip(outcome_masks(len(grid)), probability):
        current = grid[mask].sum()
        if current <= 0:
            raise ValueError('Every outcome must have positive grid support')
        result[mask] *= ((1 - weight) * current + weight * target) / current
    return result / result.sum()


def classifier_predict(model, raw_pairs):
    """Portable logistic inference; training and scikit-learn stay offline."""
    raw = np.asarray(raw_pairs, dtype=float)
    selected = raw[:, model['columns']]
    missing = ~np.isfinite(selected)
    scaled = (np.where(missing, model['medians'], selected) - model['mean']) / model['scale']
    x = np.concatenate((scaled, missing.astype(float)), axis=1).reshape(-1, 2 * scaled.shape[1] * 2)
    logits = (x @ np.asarray(model['coefficients']).T + model['intercepts']) / model.get('temperature', 1.)
    logits -= logits.max(axis=1, keepdims=True)
    probabilities = np.exp(logits)
    return probabilities / probabilities.sum(axis=1, keepdims=True)


def _binomial(n, probability):
    return np.array([math.comb(n, k) * probability ** k * (1 - probability) ** (n - k) for k in range(n + 1)])


def half_scenarios(grids, weights, shares, corrected_grid=None):
    """Thin the very same component grids into halves, including corrections."""
    mixture = blend_grids(grids, weights)
    adjustment = np.ones_like(mixture) if corrected_grid is None else np.divide(corrected_grid, mixture)
    size = len(mixture)
    first_btts, more = 0., np.zeros(3)
    for grid, weight, (home_share, away_share) in zip(grids, weights, shares):
        if not 0 < home_share < 1 or not 0 < away_share < 1:
            raise ValueError('Half shares must lie in (0, 1)')
        if not weight:
            continue
        home_bin = [_binomial(n, home_share) for n in range(size)]
        away_bin = [_binomial(n, away_share) for n in range(size)]
        for h, a in np.ndindex((size, size)):
            mass = weight * grid[h, a] * adjustment[h, a]
            first_btts += mass * (1 - (1 - home_share) ** h) * (1 - (1 - away_share) ** a)
            distribution = np.convolve(home_bin[h], away_bin[a])
            first_goals = np.arange(h + a + 1)
            more += mass * np.array([distribution[2 * first_goals > h + a].sum(),
                                    distribution[2 * first_goals == h + a].sum(),
                                    distribution[2 * first_goals < h + a].sum()])
    return {'firstHalfBothTeamsScore': float(first_btts),
            'moreGoalsHalf': dict(zip(('first', 'equal', 'second'), more.tolist()))}


def summarize_grid(grid):
    grid = np.asarray(grid, dtype=float)
    if grid.ndim != 2 or grid.shape[0] != grid.shape[1] or len(grid) < 7 or not np.all(np.isfinite(grid)) or np.any(grid < 0) or not np.isclose(grid.sum(), 1):
        raise ValueError('Expected a normalized square probability grid of size >= 7')
    home, away = np.indices(grid.shape)
    marginal_home, marginal_away = grid.sum(axis=1), grid.sum(axis=0)
    cells = [{'home': h, 'away': a, 'probability': float(grid[h, a])} for h, a in np.ndindex(grid.shape)]
    return {'rates': [float(home.ravel() @ grid.ravel()), float(away.ravel() @ grid.ravel())],
            'outcomes': dict(zip(('home', 'draw', 'away'), [float(grid[m].sum()) for m in outcome_masks(len(grid))])),
            'bothTeamsScore': float(grid[1:, 1:].sum()), 'over25': float(grid[home + away >= 3].sum()),
            'cleanSheet': {'home': float(marginal_away[0]), 'away': float(marginal_home[0])},
            'goalDistribution': {side: marginal[:6].tolist() + [float(marginal[6:].sum())]
                                 for side, marginal in (('home', marginal_home), ('away', marginal_away))},
            'topScores': sorted(cells, key=lambda cell: cell['probability'], reverse=True)[:3],
            'grid': [cell for cell in cells if cell['home'] <= 5 and cell['away'] <= 5],
            'gridRemainder': float(grid[(home > 5) | (away > 5)].sum()),
            'totalGoalRanges': [float(grid[home + away <= 1].sum()), float(grid[(home + away >= 2) & (home + away <= 3)].sum()), float(grid[home + away >= 4].sum())]}


def project_classifier_rates(rates, classifier, weight, iterations=12):
    """Fit classifier guidance back into independent Poisson for existing clients.

    No outcome or scenario is overwritten separately: every percentage remains
    derived from these two rates. Regularization keeps unsupported classifier
    signals from causing large goal-rate changes.
    """
    rates = np.asarray(rates, dtype=float)
    classifier = np.asarray(classifier, dtype=float)
    if rates.shape[-1] != 2 or classifier.shape != rates.shape[:-1] + (3,):
        raise ValueError('Expected aligned goal rates and result probabilities')
    if not 0 <= weight <= 1 or not np.all(np.isfinite(rates)) or np.any(rates <= 0) or np.any(rates > 8) or not np.all(np.isfinite(classifier)) or np.any(classifier < 0) or not np.allclose(classifier.sum(axis=-1), 1):
        raise ValueError('Invalid rates, probabilities or guidance weight')
    if weight == 0:
        return rates.copy()
    shape = rates.shape
    rates, classifier = rates.reshape(-1, 2), classifier.reshape(-1, 3)
    counts = np.arange(MAX_GOALS + 1)
    factorial = np.array([math.lgamma(int(k) + 1) for k in counts])
    home, away = np.indices((MAX_GOALS + 1, MAX_GOALS + 1))
    masks = np.stack(outcome_masks(MAX_GOALS + 1)).reshape(3, -1).astype(float)
    def probabilities(eta, derivatives=False):
        lam = np.exp(eta)
        p = np.exp(-lam[:, :, None] + counts * eta[:, :, None] - factorial)
        grid = p[:, 0, :, None] * p[:, 1, None, :]
        outcome = grid.reshape(len(lam), -1) @ masks.T
        if not derivatives:
            return np.maximum(outcome, 1e-14)
        derivative = np.stack([(grid * (home - lam[:, 0, None, None])).reshape(len(lam), -1) @ masks.T,
                               (grid * (away - lam[:, 1, None, None])).reshape(len(lam), -1) @ masks.T], axis=2)
        return np.maximum(outcome, 1e-14), derivative
    origin = np.log(rates)
    target = (1 - weight) * probabilities(origin) + weight * classifier
    eta = origin.copy()
    regularization = .1
    def objective(value):
        return -(target * np.log(probabilities(value))).sum(axis=1) + .5 * regularization * ((value - origin) ** 2).sum(axis=1)
    for _ in range(iterations):
        p, derivative = probabilities(eta, True)
        gradient = -np.einsum('ni,nij->nj', target / p, derivative) + regularization * (eta - origin)
        hessian = np.einsum('ni,nij,nik->njk', target / p ** 2, derivative, derivative) + regularization * np.eye(2)
        step = np.linalg.solve(hessian, gradient[..., None])[..., 0]
        old_loss = objective(eta)
        accepted = np.zeros(len(eta), dtype=bool)
        for factor in (1., .5, .25, .125, .0625):
            trial = np.clip(eta - factor * step, math.log(.05), math.log(8.))
            mask = ~accepted & (objective(trial) <= old_loss + 1e-12)
            eta[mask] = trial[mask]
            accepted |= mask
        if np.max(np.abs(gradient)) < 1e-7:
            break
    return np.exp(eta).reshape(shape)
