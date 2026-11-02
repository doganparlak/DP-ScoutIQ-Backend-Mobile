"""Offline league-offset challenger. No forecast route imports this module.

Estimate residual Elo gaps conditional on the existing global team ratings.
UEFA coefficients define a prior ordering, not a fixed points-to-Elo conversion.
"""
from dataclasses import dataclass
import math
import numpy as np

ELO_PER_LOGIT = 400. / math.log(10.)


@dataclass(frozen=True)
class AssociationPrior:
    coefficients: dict[str, float]
    available_at: str
    source: str

    def contrast(self, home: str | None, away: str | None):
        """No fabricated strength for an unmapped/non-UEFA association."""
        if home not in self.coefficients or away not in self.coefficients:
            return None
        a, b = self.coefficients[home], self.coefficients[away]
        if not math.isfinite(a) or not math.isfinite(b) or min(a, b) <= 0:
            raise ValueError('Association coefficients must be finite and positive')
        return math.log(a / b)


class LeagueOffsetEstimator:
    """Regularized Elo residual fit, with league deviations shrunk to UEFA prior.

    Draws count as half a win, as in the existing Elo update. Only settled
    cross-association games belong in observations. Callers enforce cutoffs.
    The prior slope is nonnegative and learned; individual deviations are learned
    too. Shrinkage is fixed before the backtest, not selected on its outcomes.
    """
    def __init__(self, prior: AssociationPrior, prior_penalty=5., deviation_penalty=20.):
        if prior_penalty <= 0 or deviation_penalty <= 0:
            raise ValueError('Positive shrinkage is required')
        self.prior = prior
        self.associations = sorted(prior.coefficients)
        self.positions = {code: i + 1 for i, code in enumerate(self.associations)}
        self.penalties = np.array([prior_penalty] + [deviation_penalty] * len(self.associations))
        self.theta = np.zeros(len(self.associations) + 1)
        self.matches = 0

    def design(self, home, away):
        contrast = self.prior.contrast(home, away)
        if contrast is None:
            return None
        row = np.zeros(len(self.theta))
        row[0] = contrast
        row[self.positions[home]] += 1.
        row[self.positions[away]] -= 1.
        return row

    def fit(self, observations):
        from scipy.optimize import minimize
        from scipy.special import expit
        valid = [(self.design(r['homeAssociation'], r['awayAssociation']), r) for r in observations]
        valid = [(x, r) for x, r in valid if x is not None and np.any(x)]
        self.matches = len(valid)
        if not valid:
            self.theta[:] = 0.
            return self
        x = np.array([row for row, _ in valid])
        base = np.array([(r['eloDifference'] + r.get('homeAdvantage', 60.)) / ELO_PER_LOGIT for _, r in valid])
        y = np.array([r['result'] for _, r in valid])
        if not np.all(np.isfinite(base)) or not np.all(np.isin(y, [0., .5, 1.])):
            raise ValueError('Expected finite Elo gaps and home results in {0, .5, 1}')
        def objective(theta):
            z = base + x @ theta
            loss = np.sum(np.logaddexp(0., z) - y * z) + .5 * np.sum(self.penalties * theta ** 2)
            gradient = x.T @ (expit(z) - y) + self.penalties * theta
            return loss, gradient
        fitted = minimize(objective, np.zeros(len(self.theta)), jac=True, method='L-BFGS-B',
                          bounds=[(0., None)] + [(None, None)] * len(self.associations),
                          options={'ftol': 1e-12, 'gtol': 1e-8, 'maxiter': 1000})
        if not fitted.success:
            raise ValueError(f'League offset fit failed: {fitted.message}')
        self.theta = fitted.x
        return self

    def gap(self, home, away):
        row = self.design(home, away)
        return 0. if row is None else float(ELO_PER_LOGIT * (row @ self.theta))

    def dump(self):
        logs = np.log(list(self.prior.coefficients.values()))
        center = float(logs.mean())
        return {'kind': 'residual_league_elo_offset_v1', 'trainingMatches': self.matches,
                'priorAvailableAt': self.prior.available_at, 'source': self.prior.source,
                'eloPerLogCoefficient': float(self.theta[0] * ELO_PER_LOGIT),
                'priorPenalty': float(self.penalties[0]), 'deviationPenalty': float(self.penalties[1]),
                'offsets': {code: float(ELO_PER_LOGIT * (self.theta[0] * (math.log(self.prior.coefficients[code]) - center) + self.theta[i + 1]))
                            for i, code in enumerate(self.associations)}}


def apply_elo_gap(raw, names, home_gap):
    """Preserve pair mean and make same-league adjustment exactly zero.

    Gap is home-minus-away, one value per fixture; rows alternate home/away.
    Keeping the pair mean avoids injecting a league-quality intercept into
    same-league matches through separately fitted own/opponent Elo coefficients.
    """
    result = np.asarray(raw, dtype=float).copy()
    gaps = np.asarray(home_gap, dtype=float)
    if len(result) != 2 * len(gaps) or not np.all(np.isfinite(gaps)):
        raise ValueError('Expected one finite Elo gap per home/away row pair')
    rating, opponent = names.index('rating'), names.index('opponent_rating')
    delta = np.column_stack((gaps / 2., -gaps / 2.)).ravel()
    result[:, rating] += delta
    result[:, opponent] -= delta
    return result


INTERNATIONAL_COMPETITIONS = (2, 5, 2286)
COUNTRY_ALIASES = {'Turkey': 'Türkiye', 'Czech Republic': 'Czechia'}


def fixture_elo_gap(model, fixture):
    """Portable serving: numeric artifact only; no fitting or provider calls."""
    component = model.get('leagueOffsets')
    if not component:
        return 0.
    if component.get('kind') != 'residual_league_elo_offset_v1':
        raise ValueError('Unsupported league-offset artifact')
    if int(fixture['league']['id']) not in component['internationalCompetitionIds']:
        return 0.
    mapping = component['teamAssociations']
    home = mapping.get(str(fixture['homeTeam']['id']))
    away = mapping.get(str(fixture['awayTeam']['id']))
    offsets = component['offsets']
    if home not in offsets or away not in offsets:
        return 0.
    gap = float(offsets[home] - offsets[away])
    if not math.isfinite(gap): raise ValueError('Invalid league Elo gap')
    return gap


def adjust_elo_context(context, home_gap, home):
    half = home_gap / 2. if home else -home_gap / 2.
    return {**context, 'rating': context['rating'] + half,
            'opponent_rating': context['opponent_rating'] - half}


def observe_match(component, match, ratings):
    """Append a settled match to the daily offset-learning state before Elo update."""
    associations = component['teamAssociations']
    domestic = component['competitionAssociations'].get(str(match['league']))
    if domestic:
        for team in (match['home'], match['away']): associations[str(team)] = domestic
    home, away = associations.get(str(match['home'])), associations.get(str(match['away']))
    if match['league'] not in component['internationalCompetitionIds'] or home == away:
        return
    if home not in component['prior']['coefficients'] or away not in component['prior']['coefficients']:
        return
    if any(row['fixtureId'] == match['id'] for row in component['observations']): return
    component['observations'].append({'fixtureId': match['id'], 'homeAssociation': home,
        'awayAssociation': away, 'eloDifference': ratings.get(match['home'], 1500.) - ratings.get(match['away'], 1500.),
        'result': 1. if match['hg'] > match['ag'] else .5 if match['hg'] == match['ag'] else 0.,
        'at': match['at']})


def refresh_offsets(component, as_of):
    """Background/offline only; SciPy is not imported by forecast requests."""
    prior = AssociationPrior(**component['prior'])
    fitted = LeagueOffsetEstimator(prior, component['priorPenalty'], component['deviationPenalty']).fit(component['observations'])
    return {**component, **fitted.dump(), 'asOf': as_of.isoformat()}


def build_component(matches, competitions, prior_payload, as_of):
    from . import forecast_candidates as c
    domestic = {str(row['leagueId']): COUNTRY_ALIASES.get(row.get('country'), row.get('country'))
                for row in competitions if row['leagueId'] not in INTERNATIONAL_COMPETITIONS and row.get('country')}
    teams = {}
    for match in matches:
        if str(match['league']) in domestic:
            for team in (match['home'], match['away']): teams[str(team)] = domestic[str(match['league'])]
    countries = sorted(set(domestic.values()) & set(prior_payload['coefficients']))
    component = {'kind': 'residual_league_elo_offset_v1', 'teamAssociations': teams,
        'competitionAssociations': domestic, 'internationalCompetitionIds': list(INTERNATIONAL_COMPETITIONS),
        'prior': {'coefficients': {key: prior_payload['coefficients'][key] for key in countries},
                  'available_at': prior_payload['availableAt'], 'source': prior_payload['source']},
        'priorPenalty': 5., 'deviationPenalty': 20., 'observations': []}
    state = c.History()
    for match in matches:
        observe_match(component, match, state.ratings)
        state.consume(match)
    return refresh_offsets(component, as_of)


def fit_adjusted_model(matches, baseline, competitions, prior_payload):
    """Explicit offline training: same chosen architecture, refitted adjusted Elo."""
    from . import forecast as f, forecast_candidates as c, forecast_refined as r
    from .forecast_history import daily_boundary
    from datetime import timedelta
    matches = sorted(matches, key=lambda m: (f.utc(m['at']), m['id']))
    component = build_component(matches, competitions, prior_payload, f.utc(matches[-1]['at']) + timedelta(hours=3))
    prior = AssociationPrior(**component['prior'])
    histories = c.History(); pending = []; index = 0; observations = []; daily = {}; gaps = {}
    for match in matches:
        kickoff = f.utc(match['at'])
        if kickoff <= f.utc(prior.available_at): raise ValueError('Association prior crosses kickoff')
        while index < len(pending) and f.utc(pending[index]['at']) + timedelta(hours=3) <= kickoff:
            previous = pending[index]
            temporary = {**component, 'observations': observations}
            before = len(observations)
            observe_match(temporary, previous, histories.ratings)
            if len(observations) > before: observations[-1]['availableAt'] = (f.utc(previous['at']) + timedelta(hours=3)).isoformat()
            histories.consume(previous); index += 1
        boundary = daily_boundary(kickoff); key = boundary.isoformat()
        if key not in daily:
            fit = LeagueOffsetEstimator(prior).fit([row for row in observations if f.utc(row['availableAt']) <= boundary])
            daily[key] = fit
        a, b = component['teamAssociations'].get(str(match['home'])), component['teamAssociations'].get(str(match['away']))
        gaps[match['id']] = daily[key].gap(a, b) if match['league'] in INTERNATIONAL_COMPETITIONS else 0.
        pending.append(match)
    data = c.examples(matches, baseline['strategy'])
    shifted = {**data, 'raw': apply_elo_gap(data['raw'], data['names'], [gaps[i] for i in data['ids']])}
    linear = r._linear(shifted, baseline)
    tree = r._refit_tree(shifted, baseline, baseline['refinedEnsemble']['nonlinear'])
    return {**baseline, **{key: linear[key] for key in ('beta', 'medians', 'mean', 'scale')},
            'refinedEnsemble': {**baseline['refinedEnsemble'], 'nonlinear': tree}, 'leagueOffsets': component}
