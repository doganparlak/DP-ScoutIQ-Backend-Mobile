"""Domestic-league report benchmarks from stored per-appearance averages.

No metric is zero-filled. A player's league stints are combined before rates
are derived. Percentiles use equal player weights, tied midranks, and the same
300-minute and top-two/20-percentage-point position rules on both sides.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from math import isfinite
from time import perf_counter

from sqlalchemy import text
from similar_players_module.features import eligible_roles

MIN_MINUTES = 300
MIN_PEERS = 5
BENCHMARK_VERSION = 4
POSITION_GROUPS = {
    'central_midfield': ('CDM', 'CM', 'CAM'),
    'full_back': ('LB', 'RB'),
    'centre_back': ('CB',),
    'goalkeeper': ('GK',),
    'centre_forward': ('CF',),
    'wide': ('LM', 'RM', 'LW', 'RW'),
}
ROLE_GROUP = {role: group for group, roles in POSITION_GROUPS.items() for role in roles}


def comparison_groups(roles):
    """Map already eligible top-two roles to unique groups, in usage order.

    Eligibility is decided BEFORE grouping. Two roles in one family cannot
    promote an otherwise ineligible secondary role. Unspecified roles retain
    their own benchmark rather than silently joining an unrelated family.
    """
    groups = {}
    for role, share in roles.items():
        group = ROLE_GROUP.get(role, role)
        entry = groups.setdefault(group, {'roles': list(POSITION_GROUPS.get(group, (role,))), 'playerRoles': {}})
        entry['playerRoles'][role] = share
    return groups

# Explicit source mappings avoid guessing formulas from display labels.
COUNTS = {
    'Goals': 'goals', 'Assists': 'assists', 'Penalties Won': 'penalties_won',
    'Touches': 'touches', 'Big Chances Created': 'big_chances_created',
    'Dribble Attempts': 'dribble_attempts', 'Successful Dribbles': 'successful_dribbles',
    'Man Of Match': 'man_of_match', 'Fouls Drawn': 'fouls_drawn',
    'Offsides Provoked': 'offsides_provoked', 'Shots Total': 'shots_total',
    'Shots On Target': 'shots_on_target', 'Expected Goals': 'expected_goals',
    'Expected Goals On Target': 'expected_goals_on_target', 'Hit Woodwork': 'hit_woodwork',
    'Penalties Scored': 'penalties_scored', 'Long Balls': 'long_balls',
    'Long Balls Won': 'long_balls_won', 'Total Crosses': 'total_crosses',
    'Accurate Crosses': 'accurate_crosses', 'Passes': 'passes',
    'Accurate Passes': 'accurate_passes', 'Backward Passes': 'backward_passes',
    'Key Passes': 'key_passes', 'Passes In Final Third': 'passes_in_final_third',
    'Through Balls': 'through_balls', 'Through Balls Won': 'through_balls_won',
    'Interceptions': 'interceptions', 'Tackles': 'tackles', 'Tackles Won': 'tackles_won',
    'Last Man Tackle': 'last_man_tackle', 'Blocked Shots': 'blocked_shots',
    'Clearances': 'clearances', 'Clearance Offline': 'clearance_offline',
    'Ball Recovery': 'ball_recovery', 'Aerials': 'aerials', 'Aerials Won': 'aerials_won',
    'Total Duels': 'total_duels', 'Duels Won': 'duels_won',
    'Goals Conceded': 'goals_conceded', 'Penalties Committed': 'penalties_committed',
    'Penalties Missed': 'penalties_missed', 'Shots Off Target': 'shots_off_target',
    'Big Chances Missed': 'big_chances_missed', 'Aerials Lost': 'aerials_lost',
    'Duels Lost': 'duels_lost', 'Fouls': 'fouls', 'Dispossessed': 'dispossessed',
    'Dribbled Past': 'dribbled_past', 'Turn Over': 'turn_over',
    'Possession Lost': 'possession_lost', 'Offsides': 'offsides', 'Own Goals': 'own_goals',
    'Error Lead To Goal': 'error_lead_to_goal', 'Error Lead To Shot': 'error_lead_to_shot',
    'Yellow Cards': 'yellow_cards', 'Yellow & Red Cards': 'yellow_red_cards',
    'Red Cards': 'red_cards', 'Saves': 'saves', 'Saves Insidebox': 'saves_insidebox',
    'Penalties Saved': 'penalties_saved', 'Punches': 'punches', 'Good High Claim': 'good_high_claim',
}
NEGATIVE = {
    'Goals Conceded', 'Penalties Committed', 'Penalties Missed', 'Shots Off Target',
    'Big Chances Missed', 'Aerials Lost', 'Duels Lost', 'Fouls', 'Dispossessed',
    'Dribbled Past', 'Turn Over', 'Possession Lost', 'Offsides', 'Own Goals',
    'Error Lead To Goal', 'Error Lead To Shot', 'Yellow Cards', 'Yellow & Red Cards', 'Red Cards',
}
# Verified against DataModule.aggregate_stream.AVAILABLE_AVERAGE_COLUMNS.
# player_comp_data has no stat_counts: these cannot be reconstructed as totals.
COVERED_AVERAGES = {'expected_goals', 'expected_goals_on_target', 'shooting_performance'}
# Numerator, denominator(s), minimum reconstructed attempts. Aerial attempts
# use won + lost, matching the existing report validation rule.
RATIOS = {
    'Accurate Passes (%)': ('accurate_passes', ('passes',), 100),
    'Long Balls Won (%)': ('long_balls_won', ('long_balls',), 20),
    'Successful Crosses (%)': ('accurate_crosses', ('total_crosses',), 20),
    'Dribble Accuracy (%)': ('successful_dribbles', ('dribble_attempts',), 20),
    'Tackles Won (%)': ('tackles_won', ('tackles',), 20),
    'Duels Won (%)': ('duels_won', ('total_duels',), 20),
    'Aerials Won (%)': ('aerials_won', ('aerials_won', 'aerials_lost'), 20),
    'Shots On Target (%)': ('shots_on_target', ('shots_total',), 20),
    'Goal Conversion (%)': ('goals', ('shots_total',), 20),
    'On-Target to Goal Conversion (%)': ('goals', ('shots_on_target',), 20),
    'Assist Efficiency (%)': ('assists', ('key_passes',), 20),
}


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def aggregate(rows):
    """One league + player + gender. Require complete stint coverage per metric."""
    matches = [number(r.get('match_count')) for r in rows]
    minutes = [number((r.get('stats') or {}).get('minutes_played')) for r in rows]
    if not rows or any(m is None or m <= 0 or t is None or t <= 0 for m, t in zip(matches, minutes)):
        return None
    total_matches = sum(matches)
    total_minutes = round(sum(m * t for m, t in zip(matches, minutes)), 8)
    keys = set.intersection(*(set((r.get('stats') or {}).keys()) for r in rows))
    totals = {}
    for key in keys:
        values = [number(r['stats'][key]) for r in rows]
        if all(v is not None for v in values):
            totals[key] = round(sum(v * m for v, m in zip(values, matches)), 8)
    counts = defaultdict(float)
    for row in rows:
        for role, count in (row.get('position_counts') or {}).items():
            if (value := number(count)) is not None:
                counts[role] += value
    metrics = {}
    for label, key in COUNTS.items():
        if key in totals and key not in COVERED_AVERAGES:
            total = totals[key]
            metrics[label] = {'value': total * 90 / total_minutes, 'perMatch': total / total_matches,
                              'total': total, 'basis': 'per90', 'lowerIsBetter': label in NEGATIVE,
                              'rankable': True}
    for label, (num, den, minimum) in RATIOS.items():
        if num not in totals or any(key not in totals for key in den):
            continue
        denominator = sum(totals[key] for key in den)
        numerator = totals[num]
        if denominator <= 0 or numerator > denominator:
            continue
        value = numerator / denominator * 100
        metrics[label] = {'value': value, 'basis': 'percentage', 'attempts': denominator,
                          'minimumAttempts': minimum, 'rankable': denominator >= minimum,
                          'lowerIsBetter': False}
    metrics['Match Count'] = {'value': total_matches, 'basis': 'context', 'rankable': False, 'lowerIsBetter': False}
    metrics['Minutes Played'] = {'value': total_minutes, 'basis': 'context', 'rankable': False, 'lowerIsBetter': False}
    if 'captain' in totals:
        metrics['Captain'] = {'value': totals['captain'] / total_matches * 100, 'basis': 'percentage',
                              'rankable': False, 'lowerIsBetter': False}
    if 'rating' in totals and len(rows) == 1:
        rating = totals['rating'] / total_matches
        if 0 <= rating <= 10:
            metrics['Rating'] = {'value': rating, 'basis': 'rating', 'rankable': len(rows) == 1, 'lowerIsBetter': False}
    # Retain source advanced averages for a single stint, without pretending
    # their unknown per-stat denominator equals match_count. Never merge these
    # averages or derive shot-quality percentages from mismatched denominators.
    if len(rows) == 1:
        for label, key in [('Expected Goals', 'expected_goals'),
                           ('Expected Goals On Target', 'expected_goals_on_target'),
                           ('Shooting Performance', 'shooting_performance')]:
            raw = (rows[0].get('stats') or {}).get(key)
            try:
                value = float(raw)
            except (ValueError, TypeError):
                continue
            if isfinite(value) and (key == 'shooting_performance' or value >= 0):
                metrics[label] = {'value': value,
                    'basis': 'coveredAverage',
                    'rankable': False, 'lowerIsBetter': False, 'unrankedReason': 'coverage_unknown'}
    roles = eligible_roles({'position_counts': dict(counts)})
    return {'playerId': int(rows[0]['player_id']), 'gender': rows[0].get('gender'),
            'roles': roles, 'comparisonGroups': comparison_groups(roles), 'minutes': total_minutes,
            'matches': total_matches, 'metrics': metrics}


def rank(value, distribution, lower=False):
    """Tie-aware midrank. Constant distributions have no discriminative rank."""
    if len(distribution) < MIN_PEERS or distribution[0] == distribution[-1]:
        return None
    score = 100 * (bisect_left(distribution, value) + bisect_right(distribution, value)) / (2 * len(distribution))
    return round(100 - score if lower else score, 1)


def profile_benchmarks(source, peers, groups=None):
    """One player per group; source must occupy exactly one slot in its pool."""
    benchmarks = {}
    for group in groups if groups is not None else source['comparisonGroups']:
        info = source['comparisonGroups'][group]
        pool = [p for p in peers if group in p['comparisonGroups'] and p['minutes'] >= MIN_MINUTES]
        metrics = {}
        for label, metric in source['metrics'].items():
            values = sorted(round(p['metrics'][label]['value'], 6) for p in pool
                            if label in p['metrics'] and p['metrics'][label]['rankable'])
            percentile = rank(round(metric['value'], 6), values, metric['lowerIsBetter']) if source['minutes'] >= MIN_MINUTES and metric['rankable'] else None
            metrics[label] = {**metric, 'percentile': percentile, 'peerCount': len(values)}
        benchmarks[group] = {**info, 'peerCount': len(pool), 'metrics': metrics}
    return benchmarks


def build_domestic_percentiles(db, metadata):
    start = perf_counter()
    player_id = number(metadata.get('player_id'))
    if not player_id:
        return {'version': BENCHMARK_VERSION, 'competitions': [], 'computationMs': 0}
    rows = db.execute(text("""SELECT player_id, team_id, team_name, league_id, league_name, league_image_path,
        gender, match_count, position_counts, stats
        FROM player_comp_data
        WHERE league_type = 'league' AND league_sub_type = 'domestic'
          AND player_id = :player_id ORDER BY league_id, team_id"""), {'player_id': int(player_id)}).mappings().all()
    league_ids = list({row['league_id'] for row in rows})
    if not league_ids:
        return {'version': BENCHMARK_VERSION, 'competitions': [], 'computationMs': round((perf_counter()-start)*1000, 2)}
    peers = db.execute(text("""SELECT player_id, team_id, league_id, gender,
        match_count, position_counts, stats FROM player_comp_data
        WHERE league_type = 'league' AND league_sub_type = 'domestic'
          AND league_id = ANY(:league_ids)"""), {'league_ids': league_ids}).mappings().all()
    grouped = defaultdict(list)
    for row in peers:
        grouped[(row['league_id'], row['player_id'], row['gender'])].append(row)
    profiles = {key: aggregate(stints) for key, stints in grouped.items()}
    competitions = []
    for league_id in sorted(league_ids):
        own_rows = [r for r in rows if r['league_id'] == league_id]
        genders = {r['gender'] for r in own_rows}
        if len(genders) != 1:
            continue
        gender = own_rows[0]['gender']
        source = profiles.get((league_id, int(player_id), gender))
        if not source:
            continue
        league_peers = [p for (lid, _, g), p in profiles.items()
                        if p and lid == league_id and g == gender and bool(g)]
        role_metrics = profile_benchmarks(source, league_peers)
        competitions.append({'leagueId': league_id, 'leagueName': own_rows[0]['league_name'],
            'leagueLogoUrl': next((r.get('league_image_path') for r in own_rows if r.get('league_image_path')), None),
            'teams': list(dict.fromkeys(r['team_name'] for r in own_rows)),
            'currentTeam': any(str(r['team_id']) == str(metadata.get('team_id')) for r in own_rows),
            'minutes': source['minutes'], 'matches': source['matches'], 'roles': source['roles'],
            'benchmarks': role_metrics})
    # PDF uses only the latest club's domestic stint and its most-played role.
    # League peers remain deduplicated annual league profiles. Replace our own
    # pooled profile with the selected stint so the target is counted once.
    current_team_id = number(metadata.get('team_id'))
    pdf_candidates = []
    for competition in competitions:
        own_rows = [r for r in rows if r['league_id'] == competition['leagueId']
                    and current_team_id and r['team_id'] == int(current_team_id)]
        pdf_source = aggregate(own_rows)
        if not pdf_source or not pdf_source['roles']:
            continue
        primary_role = next(iter(pdf_source['roles']))
        primary_group = ROLE_GROUP.get(primary_role, primary_role)
        league_peers = [p for (lid, pid, g), p in profiles.items()
                        if p and lid == competition['leagueId'] and g == pdf_source['gender']
                        and bool(g) and pid != int(player_id)] + [pdf_source]
        pdf_candidates.append({**competition, 'teamId': int(current_team_id),
            'teams': list(dict.fromkeys(r['team_name'] for r in own_rows)),
            'minutes': pdf_source['minutes'], 'matches': pdf_source['matches'],
            'roles': pdf_source['roles'], 'primaryRole': primary_role,
            'benchmarks': profile_benchmarks(pdf_source, league_peers, [primary_group])})
    pdf_competition = pdf_candidates[0] if len(pdf_candidates) == 1 else None
    if len(pdf_candidates) > 1:
        # Promotion/relegation can leave two leagues for the same latest club.
        # Resolve from current membership metadata, never from minutes alone.
        league_name = str(metadata.get('team_current_league_name') or metadata.get('league_name') or '').strip().casefold()
        matches = [c for c in pdf_candidates if c['leagueName'].strip().casefold() == league_name]
        if len(matches) == 1:
            pdf_competition = matches[0]
    competitions.sort(key=lambda c: (not c['currentTeam'], -c['minutes'], c['leagueId']))
    return {'version': BENCHMARK_VERSION, 'minimumMinutes': MIN_MINUTES, 'minimumPeers': MIN_PEERS,
            'period': 'annual', 'generatedAt': datetime.now(timezone.utc).isoformat(),
            'competitions': competitions, 'pdfCompetition': pdf_competition, 'computationMs': round((perf_counter()-start)*1000, 2)}
