"""Mergeable rate evidence. Volume totals are never substituted for rate inputs."""
from math import isfinite
import re

VERSION = 1


def key(name):
    value = str(name).lower().removeprefix('stat_').replace('%', ' percentage ')
    value = re.sub(r'[^a-z0-9]+', '_', value).strip('_')
    return {
        'aeriels_won': 'aerials_won', 'aeriels_lost': 'aerials_lost',
        'accurate_crosses_percentage': 'successful_crosses_percentage',
        'on_target_conversion_percentage': 'on_target_to_goal_conversion_percentage',
        'on_target_goal_conversion_percentage': 'on_target_to_goal_conversion_percentage',
        'expected_goals_xg': 'expected_goals',
        'expected_goals_on_target_xgot': 'expected_goals_on_target',
    }.get(value, value)


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if isfinite(value) and value >= 0 else None
    except (ValueError, TypeError):
        return None


# Custom metrics retain the product's cap; their numerators need not be subsets.
CUSTOM = {'assist_efficiency_percentage', 'shot_quality_percentage',
          'on_target_shot_quality_percentage'}
MATCHED = {'shot_quality_percentage', 'on_target_shot_quality_percentage'}
RATES = {
    'accurate_passes_percentage': ('accurate_passes', 'passes'),
    'long_balls_won_percentage': ('long_balls_won', 'long_balls'),
    'successful_crosses_percentage': ('accurate_crosses', 'total_crosses'),
    'dribble_accuracy_percentage': ('successful_dribbles', 'dribble_attempts'),
    'tackles_won_percentage': ('tackles_won', 'tackles'),
    'duels_won_percentage': ('duels_won', 'total_duels'),
    'aerials_won_percentage': ('aerials_won', 'aerials'),
    'shots_on_target_percentage': ('shots_on_target', 'shots_total'),
    'goal_conversion_percentage': ('goals', 'shots_total'),
    'on_target_to_goal_conversion_percentage': ('goals', 'shots_on_target'),
    'assist_efficiency_percentage': ('assists', 'key_passes'),
    'shot_quality_percentage': ('expected_goals', 'shots_total'),
    'on_target_shot_quality_percentage': ('expected_goals_on_target', 'shots_on_target'),
}


def empty():
    return {'version': VERSION, 'rates': {}, 'counts': {}}


def missing_attempt_names(values):
    """Names that must not be filled with an inferred zero attempt count."""
    normalized = {key(k): number(v) for k, v in values.items()}
    missing = set()
    for name, (a, b) in RATES.items():
        if name not in MATCHED and (normalized.get(a) or 0) > 0 and normalized.get(b) is None:
            missing.add(b)
            if b == 'aerials':
                missing.add('aerials_lost')
    return missing


def add_sample(evidence, values, weight=1):
    original = values
    values = {key(k): number(v) for k, v in original.items()}
    for name, raw in original.items():
        if key(name) == 'shooting_performance':
            try:
                value = float(raw)
                values['shooting_performance'] = value if isfinite(value) else None
            except (TypeError, ValueError):
                pass
    for name, value in values.items():
        if value is not None:
            count = evidence['counts'].setdefault(name, {'sum': 0.0, 'covered': 0})
            count['sum'] += value
            count['covered'] += 1
    for name, (numerator, denominator) in RATES.items():
        a, b = values.get(numerator), values.get(denominator)
        if name == 'aerials_won_percentage' and b is None:
            lost = values.get('aerials_lost')
            if a is not None and lost is not None:
                b = a + lost
        state = evidence['rates'].setdefault(name, {
            'numerator': 0.0, 'denominator': 0.0, 'coverage': 0.0,
            'missing_attempts': False, 'invalid': False,
        })
        if name in MATCHED:
            if a is None or b is None:
                continue
        elif b is None:
            if a is not None and a > 0:
                state['missing_attempts'] = True
            continue
        elif a is None:
            if numerator in values:
                state['invalid'] = True
                continue
            # A recorded attempt count establishes coverage for omitted successes.
            a = 0.0
        if a is None:
            continue
        if name not in CUSTOM and a > b:
            state['invalid'] = True
        state['numerator'] += a
        state['denominator'] += b
        state['coverage'] += weight


def merge(evidences):
    result = empty()
    for evidence in evidences:
        if not isinstance(evidence, dict) or evidence.get('version') != VERSION:
            return None
        for name, source in evidence.get('rates', {}).items():
            target = result['rates'].setdefault(name, {
                'numerator': 0.0, 'denominator': 0.0, 'coverage': 0.0,
                'missing_attempts': False, 'invalid': False,
            })
            for field in ('numerator', 'denominator', 'coverage'):
                target[field] += source.get(field, 0)
            for field in ('missing_attempts', 'invalid'):
                target[field] |= bool(source.get(field))
        for name, source in evidence.get('counts', {}).items():
            target = result['counts'].setdefault(name, {'sum': 0.0, 'covered': 0})
            target['sum'] += source.get('sum', 0)
            target['covered'] += source.get('covered', 0)
    return result


def rate_value(name, state):
    if not state or state.get('missing_attempts') or state.get('invalid'):
        return None
    a, b = number(state.get('numerator')), number(state.get('denominator'))
    if a is None or b is None or b <= 0:
        return None
    return min(100.0, a / b * 100) if key(name) in CUSTOM else a / b * 100


def apply_merged_rates(result, stats, matches):
    """Update existing API metric names, preserving null availability markers."""
    index = {key(name): name for name in result}
    pieces = []
    complete_coverage = True
    for source, count in zip(stats, matches):
        evidence = source.get('_rate_inputs')
        if not isinstance(evidence, dict) or evidence.get('version') != VERSION:
            complete_coverage = False
            evidence = empty()
            # Legacy counts can be pooled, but cannot establish matched xG
            # coverage. Preserve flags from any newer stints in the selection.
            add_sample(evidence, {key(k): number(v) * count for k, v in source.items()
                                  if number(v) is not None and key(k) not in RATES})
            for name in MATCHED:
                evidence['rates'].pop(name, None)
        pieces.append(evidence)
    evidence = merge(pieces)
    if complete_coverage:
        result['_rate_inputs'] = evidence
        total_matches = sum(matches)
        for name, count in evidence['counts'].items():
            if (name in index and name not in RATES and not name.endswith('_percentage')
                    and name not in {'expected_goals', 'expected_goals_on_target', 'shooting_performance', 'rating'}
                    and total_matches > 0):
                result[index[name]] = count['sum'] / total_matches
    for name in RATES:
        if name in index:
            result[index[name]] = rate_value(name, evidence['rates'].get(name))
    for name, state in evidence['rates'].items():
        if state.get('missing_attempts'):
            denominator = RATES[name][1]
            if denominator in index:
                result[index[denominator]] = None
    # Covered-average inputs have a per-stat observation count, not match_count.
    for name in ('expected_goals', 'expected_goals_on_target', 'shooting_performance', 'rating'):
        if name in index:
            covered = evidence['counts'].get(name)
            if covered and covered['covered'] > 0 and complete_coverage:
                result[index[name]] = covered['sum'] / covered['covered']
            elif len(stats) > 1:
                result[index[name]] = None
    return evidence
