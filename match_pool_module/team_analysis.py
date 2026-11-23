"""Mobile post-match findings use Enterprise's single-fixture selection rules."""
from collections import OrderedDict, defaultdict
from itertools import combinations, product
import threading
import time

from standings_module.comparison_ranks import canonical_metric_name
from standings_module.team_derived import MIRRORS
from team_analysis_module.team_findings import CATALOG, number
from .pre_match_findings import TOPIC_ANCHORS, TOPIC_METRICS, _supports

TEAM_ANALYSIS_VERSION = 6

_CACHE = OrderedDict()
_LOCK = threading.Lock()
_TTL_SECONDS = 1800

def cached_analysis(key):
    with _LOCK:
        item = _CACHE.get(key)
        if item and time.monotonic() - item[0] < _TTL_SECONDS:
            _CACHE.move_to_end(key)
            return item[1]
        _CACHE.pop(key, None)
    return None

def cache_analysis(key, value):
    with _LOCK:
        _CACHE[key] = (time.monotonic(), value)
        _CACHE.move_to_end(key)
        while len(_CACHE) > 128:
            _CACHE.popitem(last=False)

# A count and rates that reuse the same outcome cannot establish independent evidence.
_METRIC_FAMILIES = (
    {'Goals', 'Goal Conversion (%)', 'On Target Goal Conversion (%)'},
    {'Shots On Target', 'Shots On Target (%)'},
    {'Expected Goals', 'Expected Goals (xG)', 'Expected Goals Open Play (xGOP)', 'Expected Goals Non Penalty Goals (npxG)'},
    {'Shots Total', 'Shots Insidebox', 'Shots Outsidebox'},
    {'Passes', 'Accurate Passes', 'Accurate Passes (%)'},
    {'Total Crosses', 'Accurate Crosses', 'Accurate Crosses (%)', 'Unsuccessful Crosses'},
    {'Fouls', 'Fouls Per 100 Opponent Passes'},
    {'Ball Recovery', 'Recoveries Per 100 Opponent Passes'},
    {'Interceptions', 'Interceptions Per 100 Opponent Passes'},
    {'Total Cards', 'Yellow Cards', 'Red Cards'},
)
_FAMILY_BY_METRIC = {name: index for index, group in enumerate(_METRIC_FAMILIES) for name in group}


def _family(name):
    source = MIRRORS.get(name, (name.removeprefix('Opponent '),))[0]
    return _FAMILY_BY_METRIC.get(source, source)


def _match_metrics(team):
    return {
        canonical_metric_name(row['name']): {'value': value, 'matches_covered': 1}
        for rows in [*(team.get('categories') or {}).values(), team.get('expected_metrics') or []]
        for row in rows if row.get('name') and not row.get('_inferred_zero')
        and (value := number(row.get('value'))) is not None
    }


def _comparison_metrics(team, opponent):
    values = dict(team)
    for name, (source, _, _) in MIRRORS.items():
        if source in opponent and name not in values:
            values[name] = dict(opponent[source])
    for name, numerator, denominator in (
        ('Goal Conversion (%)', 'Goals', 'Shots Total'),
        ('On Target Goal Conversion (%)', 'Goals', 'Shots On Target'),
        ('Fouls Per 100 Opponent Passes', 'Fouls', 'Opponent Passes'),
        ('Recoveries Per 100 Opponent Passes', 'Ball Recovery', 'Opponent Passes'),
        ('Interceptions Per 100 Opponent Passes', 'Interceptions', 'Opponent Passes'),
    ):
        top, bottom = values.get(numerator), values.get(denominator)
        if name not in values and top and bottom and bottom['value'] > 0:
            ratio = top['value'] / bottom['value']
            if not name.endswith('(%)') or ratio <= 1:
                values[name] = {'value': 100 * ratio, 'matches_covered': 1}
    return values


def _candidate_pairs(own, opponent):
    by_topic = defaultdict(list)
    for topic, pool in TOPIC_METRICS.items():
        context = {
            'dribbling': {'Dribble Attempts'},
            'progression': {'Dangerous Attacks'},
        }.get(topic, set())
        common = {name for name in (pool | context) & own.keys() & opponent.keys()
                  if own[name]['value'] > 0 and opponent[name]['value'] > 0}
        if len(common) < 3:
            continue
        gaps = {name: abs(own[name]['value'] - opponent[name]['value'])
                / max(abs(own[name]['value']), abs(opponent[name]['value']), 3 if name.endswith('(%)') else 2)
                for name in common}
        for side in ('strength', 'weakness'):
            supporting = {name for name in common - context if _supports(name, own[name], opponent[name], side)}
            opposing = {name for name in common - context if _supports(
                name, own[name], opponent[name], 'weakness' if side == 'strength' else 'strength')}
            for names in combinations(sorted(common), 3):
                if len({_family(name) for name in names}) != len(names):
                    continue
                evidence = supporting.intersection(names)
                if len(evidence) < 2 or (topic in TOPIC_ANCHORS and not TOPIC_ANCHORS[topic].intersection(evidence)):
                    continue
                if opposing.intersection(names):
                    continue
                score = sum(gaps[name] for name in evidence) / len(evidence)
                families = frozenset(_family(name) for name in names)
                by_topic[topic].append((score, topic, side, names, evidence, families))

    # Keep a few distinct evidence sets per topic so the fixture can reuse fewer
    # metrics without enumerating every combination from broad categories.
    for topic, options in by_topic.items():
        options.sort(key=lambda row: (-row[0], row[2], row[3]))
        retained = []
        for side in ('strength', 'weakness'):
            unique = {}
            for option in options:
                families = option[5]
                if option[2] != side or families in unique:
                    continue
                if len(unique) < 3 or any(not families.intersection(prior) for prior in unique):
                    unique[families] = option
                if len(unique) == 5:
                    break
            retained.extend(unique.values())
        by_topic[topic] = retained
    if len(by_topic) < 2:
        return [(option,) for options in by_topic.values() for option in options] or [()]
    return [pair for left, right in combinations(by_topic.values(), 2)
            for pair in product(left, right)]


def _fixture_pair_score(pairs, goal_margin):
    cards = [card for pair in pairs for card in pair]
    topics = {card[1] for card in cards}
    families = set().union(*(card[5] for card in cards))
    repeated_metrics = sum(len(card[5]) for card in cards) - len(families)
    result_fit = 0
    if goal_margin:
        winner, loser = pairs if goal_margin > 0 else pairs[::-1]
        result_fit = int(any(card[2] == 'strength' for card in winner))
        result_fit += int(any(card[2] == 'weakness' for card in loser))
    return result_fit, len(topics), -repeated_metrics, sum(card[0] for card in cards)


def _cards(selected, own, opponent, lang):
    locale = 0 if lang == 'tr' else 1
    return [{
        'id': topic, 'side': side, 'title': CATALOG[topic][side][locale],
        'description': CATALOG[topic]['description'][locale],
        'metrics': [{'metric_name': name, 'team': own[name], 'opponent': opponent[name],
                     'context_only': name not in evidence}
                    for name in sorted(names, key=lambda name: (name not in evidence, -_gap(name, own, opponent), name))],
    } for _, topic, side, names, evidence, _ in sorted(selected, key=lambda card: -card[0])]


def _gap(name, own, opponent):
    return abs(own[name]['value'] - opponent[name]['value']) / max(abs(own[name]['value']), abs(opponent[name]['value']), 1)


def build_team_analysis(report, lang):
    teams = report.get('teams') or []
    if len(teams) != 2:
        return {}
    first, second = map(_match_metrics, teams)
    own, other = _comparison_metrics(first, second), _comparison_metrics(second, first)
    scores = {str(row.get('participant_id')): number((row.get('score') or {}).get('goals'))
              for row in report.get('scores') or [] if row.get('description') == 'CURRENT'}
    first_id, second_id = (str(team['id']) for team in teams)
    first_goals, second_goals = scores.get(first_id), scores.get(second_id)
    goal_margin = first_goals - second_goals if first_goals is not None and second_goals is not None else None
    first_pair, second_pair = max(
        product(_candidate_pairs(own, other), _candidate_pairs(other, own)),
        key=lambda pairs: _fixture_pair_score(pairs, goal_margin),
    )
    return {
        first_id: _cards(first_pair, own, other, lang),
        second_id: _cards(second_pair, other, own, lang),
    }
