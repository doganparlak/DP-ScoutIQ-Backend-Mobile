"""Select Mobile pre-match labels from the Enterprise topic and evidence rules."""
import math
from itertools import combinations

from standings_module.metrics import LOWER_BETTER
from standings_module.team_derived import DEFINITIONS
from team_analysis_module.team_findings import CATALOG, number


TOPIC_METRICS = {
    'creation': {'Key Passes', 'Big Chances Created', 'Expected Goals (xG)', 'Expected Goals',
                 'Expected Goals Open Play (xGOP)', 'Expected Goals Non Penalty Goals (npxG)',
                 'Shots Insidebox', 'Shots Total', 'Dangerous Attacks', 'Expected Assists'},
    'finishing': {'Goals', 'Goal Conversion (%)', 'On Target Goal Conversion (%)',
                  'Shots On Target (%)', 'Shots On Target'},
    'passing': {'Accurate Passes (%)', 'Passes', 'Accurate Passes', 'Possession Lost', 'Dispossessed'},
    'progression': {'Total Crosses', 'Final-third Pass Share (%)', 'Passes In Final Third', 'Outfield Final-third Pass Share (%)',
                    'Outfield Passes In Final Third', 'Accurate Crosses', 'Accurate Crosses (%)', 'Unsuccessful Crosses',
                    'Accurate Long Balls', 'Long Balls Won (%)', 'Dangerous Attacks'},
    'dribbling': {'Dribble Accuracy (%)', 'Successful Dribbles', 'Dribbles Won', 'Dispossessed'},
    'set_pieces': {'Expected Goals Set Play (xGSP)', 'Expected Goals Corners (xGC)',
                   'Expected Goals Free Kicks (xGFK)', 'Corners'},
    'duels': {'Duels Won (%)', 'Aerials Won (%)', 'Duels Won', 'Aerials Won', 'Duels Lost', 'Aerials Lost'},
    'recoveries': {'Ball Recovery', 'Interceptions', 'Tackles Won', 'Recoveries Per 100 Opponent Passes',
                   'Interceptions Per 100 Opponent Passes', 'Tackles Won (%)'},
    'threat': {name for name, (_, category, _) in DEFINITIONS.items()
               if category == 'opponent_threat' and name not in ('Opponent Big Chances Missed', 'Opponent Hit Woodwork')},
    'errors': {'Error Lead To Goal', 'Error Lead To Shot', 'Own Goals', 'Dispossessed', 'Possession Lost'},
    'discipline': {'Fouls', 'Fouls Per 100 Opponent Passes', 'Yellow Cards', 'Red Cards', 'Total Cards',
                   'Penalties Committed'},
    'game_management': {'Lead Win Rate (%)', 'Trailing Equalizer Rate (%)'},
}
TOPIC_ANCHORS = {
    'passing': {'Accurate Passes (%)', 'Possession Lost', 'Dispossessed'},
    'finishing': {'Goals', 'Goal Conversion (%)', 'On Target Goal Conversion (%)', 'Shots On Target (%)'},
    'dribbling': {'Dribble Accuracy (%)', 'Successful Dribbles'},
    'set_pieces': {'Expected Goals Set Play (xGSP)', 'Expected Goals Corners (xGC)', 'Expected Goals Free Kicks (xGFK)'},
    'errors': {'Error Lead To Goal', 'Error Lead To Shot', 'Own Goals'},
}


def _preference(name):
    if name in DEFINITIONS:
        return DEFINITIONS[name][2]
    if name in LOWER_BETTER or name in {'Fouls Per 100 Opponent Passes', 'Total Cards'}:
        return 'lower'
    return 'higher'


def _supports(name, own, opponent, side):
    if name == 'Total Crosses':
        return False  # Delivery volume contextualizes accuracy and completion.
    preference = _preference(name)
    if preference == 'neutral':
        return False
    gap = own['value'] - opponent['value']
    if preference == 'lower':
        gap = -gap
    if side == 'weakness':
        gap = -gap
    # Do not turn negligible execution-rate differences into a finding.
    return gap >= 0.5 if name.endswith('(%)') else gap > 0


def _eligible_metrics(left, right, side):
    eligible = {}
    for topic, pool in TOPIC_METRICS.items():
        available = pool & left.keys() & right.keys()
        supporting = {name for name in available if _supports(name, left[name], right[name], side)}
        if (len(available) >= 3 and len(supporting) >= 2
                and (topic not in TOPIC_ANCHORS or TOPIC_ANCHORS[topic].intersection(supporting))):
            eligible[topic] = {'supporting': sorted(supporting), 'context': sorted(available - supporting)}
    return eligible


def _available(usage):
    sample_size = usage.get('sample_size') or 0
    minimum = max(3, math.ceil(sample_size * .6))
    metrics = {}
    for rows in (usage.get('team_comparison') or {}).values():
        for row in rows:
            value, covered = number(row.get('value')), number(row.get('matches_covered'))
            if value is not None and covered is not None and covered >= minimum:
                metric = {'value': value, 'matches_covered': int(covered)}
                metrics[row['name']] = metric
    # Older snapshots may have the complete source counts but no derived rate.
    derived = {
        'On Target Goal Conversion (%)': ('Goals', 'Shots On Target'),
        'Goal Conversion (%)': ('Goals', 'Shots Total'),
        'Fouls Per 100 Opponent Passes': ('Fouls', 'Opponent Passes'),
        'Recoveries Per 100 Opponent Passes': ('Ball Recovery', 'Opponent Passes'),
        'Interceptions Per 100 Opponent Passes': ('Interceptions', 'Opponent Passes'),
    }
    for name, (numerator, denominator) in derived.items():
        top, bottom = metrics.get(numerator), metrics.get(denominator)
        if (name not in metrics and top and bottom and bottom['value'] > 0 and
                top['matches_covered'] == bottom['matches_covered'] == sample_size and
                (not name.endswith('(%)') or top['value'] <= bottom['value'])):
            metrics[name] = {'value': round(100 * top['value'] / bottom['value'], 4), 'matches_covered': sample_size}
    states = {row.get('key'): row for row in (usage.get('score_flow') or {}).get('states', [])}
    for state, field, name in (
        ('ahead', 'final_wins', 'Lead Win Rate (%)'),
        ('behind', 'matches_to_level', 'Trailing Equalizer Rate (%)'),
    ):
        row = states.get(state, {})
        count, wins = number(row.get('matches_entered')), number(row.get(field))
        if count is not None and wins is not None and 0 <= wins <= count and count >= 3:
            metrics[name] = {'value': round(100 * wins / count, 4), 'matches_covered': int(count)}
    return metrics


def with_team_findings(analysis, usages, lang, include_locked):
    """Attach up to two supported labels per side to each team's plan."""
    teams = analysis.get('teams') or {}
    if len(usages) != 2:
        return analysis
    available = [_available(usage) for usage in usages]
    localized = 0 if lang == 'tr' else 1
    result = {}
    for index, usage in enumerate(usages):
        team_id = str(usage['team_id'])
        own, opponent = available[index], available[1 - index]
        ranked = {}
        for side in ('strength', 'weakness'):
            eligible = _eligible_metrics(own, opponent, side)
            scores = {}
            for topic, evidence in eligible.items():
                gaps = [
                    abs(own[name]['value'] - opponent[name]['value']) /
                    max(own[name]['value'], opponent[name]['value'])
                    for name in evidence['supporting']
                ]
                scores[topic] = sum(sorted(gaps, reverse=True)[:2]) / 2
            ranked[side] = scores
        strength_scores, weakness_scores = ranked['strength'], ranked['weakness']
        strengths = sorted(strength_scores, key=lambda topic: (-strength_scores[topic], topic))
        weaknesses = sorted(weakness_scores, key=lambda topic: (-weakness_scores[topic], topic))
        # Prefer full sections, then the strongest evidence. A topic cannot
        # describe the same team as both an advantage and a risk.
        choices = [
            (group, tuple(topic for topic in weaknesses if topic not in group)[:2])
            for size in range(min(2, len(strengths)) + 1)
            for group in combinations(strengths, size)
        ]
        strength, weakness = max(choices, key=lambda pair: (
            len(pair[0]) + len(pair[1]), len(pair[0]),
            sum(strength_scores[topic] for topic in pair[0]) + sum(weakness_scores[topic] for topic in pair[1]),
        ))
        selected = {'strength': strength}
        if include_locked:
            selected['weakness'] = weakness
        findings = {
            side: [{'id': topic, 'title': CATALOG[topic][side][localized],
                    'description': CATALOG[topic]['description'][localized]} for topic in topics]
            for side, topics in selected.items()
        }
        result[team_id] = {'strategy': (teams.get(team_id) or {}).get('strategy'), 'findings': findings}
    return {**analysis, 'teams': result}
