"""Compact, mergeable standings statistics. No network, database or AI calls."""
from __future__ import annotations

from report_module.rate_evidence import empty, add_sample, rate_value, key as rate_key, RATES
import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .metric_definitions import DERIVED_PERCENTAGE_METRICS, TEAM_METRIC_CATEGORY, PLAYER_METRIC_CATEGORY

VERSION = 2
TEAM_ADVANCED_VERSION = 2
# Keep provider identifiers: xGD is supplied, never reconstructed as xG - xGA.
TEAM_ADVANCED_METRICS = frozenset({
    'Expected Goals Against (xGA)', 'Expected Goals Corners (xGC)',
    'Expected Goals Difference (xGD)', 'Expected Goals Free Kicks (xGFK)',
    'Expected Goals Non Penalty Goals (npxG)', 'Expected Goals Open Play (xGOP)',
    'Expected Goals Penalties (pxG)', 'Expected Goals Prevented (xGP)',
    'Expected Goals Set Play (xGSP)', 'Expected Points (xPTS)',
})
PERIOD_ZONE = ZoneInfo('Europe/Istanbul')
PERIOD_ANCHOR = date(2026, 1, 5)  # Monday; continuous 14-day blocks across years.
COMPLETED_STATES = {5, 7, 8}  # FT, AET, FT after penalties; exclude walkovers.
EXTRA_COUNTS = {'Assists', 'Hit Woodwork', 'Substitutions', 'Clearances', 'Total Duels',
                'Duels Lost', 'Aerials', 'Aerials Won', 'Tackles Won', 'Ball Recovery',
                'Possession Lost', 'Dispossessed', 'Error Lead To Goal', 'Error Lead To Shot'}
AVERAGES = {'Ball Possession %', 'Shooting Performance'}
LOWER_BETTER = {'Goals Conceded', 'Yellow Cards', 'Red Cards', 'Fouls', 'Offsides',
                'Big Chances Missed', 'Shots Off Target', 'Penalties Committed',
                'Penalties Missed', 'Possession Lost', 'Dispossessed',
                'Error Lead To Goal', 'Error Lead To Shot', 'Duels Lost', 'Expected Goals Against (xGA)'}


# Only semantically matching player metrics are eligible. In particular, fixture
# Shots Blocked (58) describes the shooter's blocked attempts, not defensive blocks (97).
PLAYER_COUNT_METRICS = frozenset({
    'Accurate Crosses', 'Accurate Passes', 'Assists', 'Big Chances Created',
    'Big Chances Missed', 'Blocked Shots', 'Dribble Attempts', 'Duels Won',
    'Expected Goals', 'Expected Goals On Target', 'Fouls', 'Goals', 'Hit Woodwork',
    'Interceptions', 'Key Passes', 'Long Balls', 'Long Balls Won', 'Offsides',
    'Passes', 'Red Cards', 'Saves', 'Shooting Performance', 'Shots Off Target',
    'Shots On Target', 'Shots Total', 'Successful Dribbles', 'Tackles',
    'Tackles Won', 'Total Crosses', 'Yellow Cards',
})
PLAYER_RATE_METRICS = {name: pair for name, pair in DERIVED_PERCENTAGE_METRICS.items()
                       if all(component in PLAYER_COUNT_METRICS for component in pair)}
PLAYER_AWARD_METRICS = PLAYER_COUNT_METRICS | PLAYER_RATE_METRICS.keys()


def player_metric_result(player, team_matches, name, mode='total'):
    """Shared eligibility/value rules for metric awards and player leaderboards."""
    if player['appearances'] * 2 < team_matches:
        return None
    total = player['metricTotals'].get(name)
    if not total or not total['matchesCovered'] or total['matchesCovered'] * 5 < team_matches:
        return None
    if name in PLAYER_RATE_METRICS:
        value = total.get('rateValue') if total['denominator'] > 0 else None
    elif mode == 'per90' and name not in AVERAGES:
        minutes = total.get('minutesCovered', 0)
        value = total['sum'] * 90 / minutes if minutes > 0 and not total.get('missingMinutes') else None
    else:
        value = total['sum'] / total['matchesCovered'] if mode == 'perMatch' or name in AVERAGES else total['sum']
    return (value, total) if value is not None else None


def player_metric_values(row):
    values = {}
    for detail in [*(row.get('details') or []), *(row.get('xglineup') or row.get('xGLineup') or [])]:
        name = (detail.get('type') or {}).get('name') or ''
        if name == 'Blocked Shots':
            continue  # Defensive blocks are not fixture Shots Blocked.
        canonical = PLAYER_METRIC_CATEGORY.get(name, (None, name))[1]
        raw = detail.get('data') or {}
        value = numeric(raw.get('value') if isinstance(raw, dict) and 'value' in raw else detail.get('value'))
        if canonical in PLAYER_COUNT_METRICS and value is not None and (value >= 0 or canonical == 'Shooting Performance'):
            values.setdefault(canonical, value)
    return values


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def period_for(day: date):
    start = PERIOD_ANCHOR + timedelta(days=((day - PERIOD_ANCHOR).days // 14) * 14)
    return start, start + timedelta(days=14)


def kickoff(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def advanced_team_metrics(fixture):
    """Read additive team fields directly; absent fields stay absent."""
    teams = {}
    for row in [*(fixture.get('xgfixture') or fixture.get('xGFixture') or []), *(fixture.get('statistics') or [])]:
        name = (row.get('type') or {}).get('name') or ''
        value = numeric((row.get('data') or {}).get('value'))
        if name not in TEAM_ADVANCED_METRICS | {'Penalties'} or value is None:
            continue
        if value < 0 and name not in {'Expected Goals Difference (xGD)', 'Expected Goals Prevented (xGP)'}:
            continue
        participant = row.get('participant_id')
        if participant:
            teams.setdefault(str(participant), {})[name] = value
    return teams


def normalize_fixture(fixture):
    teams = {}
    participants = fixture.get('participants') or []
    locations = {(p.get('meta') or {}).get('location'): int(p['id']) for p in participants}
    if not locations.get('home') or not locations.get('away'):
        raise ValueError('Fixture participants are incomplete')
    for p in participants:
        teams[str(p['id'])] = {'name': p.get('name') or '', 'metrics': {}}
    for row in [*(fixture.get('statistics') or []), *(fixture.get('xgfixture') or fixture.get('xGFixture') or [])]:
        team = teams.get(str(row.get('participant_id')))
        name = (row.get('type') or {}).get('name') or ''
        canonical = TEAM_METRIC_CATEGORY.get(name, (None, name))[1]
        value = numeric((row.get('data') or {}).get('value'))
        if team is not None and value is not None and (name in TEAM_METRIC_CATEGORY or name in EXTRA_COUNTS):
            # Prefer the team-statistics source when xG appears in both includes.
            team['metrics'].setdefault(canonical, value)
    for team_id, metrics in advanced_team_metrics(fixture).items():
        if team_id in teams:
            for name in TEAM_ADVANCED_METRICS:
                teams[team_id]['metrics'].pop(name, None)
            teams[team_id]['metrics'].update(metrics)
    players = []
    seen = set()
    for row in fixture.get('lineups') or []:
        pid, tid = int(row.get('player_id') or 0), int(row.get('team_id') or 0)
        if not pid or str(tid) not in teams or (pid, tid) in seen:
            continue
        seen.add((pid, tid))
        values = {(d.get('type') or {}).get('name'): numeric((d.get('data') or {}).get('value')) for d in row.get('details') or []}
        minutes, rating = values.get('Minutes Played'), values.get('Rating')
        if rating is not None and not 0 < rating <= 10:
            rating = None
        # An unused substitute is never an appearance. A rating without minutes
        # still establishes participation; missing minutes remain missing.
        if minutes is not None and minutes <= 0 or minutes is None and rating is None:
            continue
        player = row.get('player') or {}
        players.append({'playerId': pid, 'teamId': tid,
                        'name': player.get('display_name') or player.get('name') or row.get('player_name') or str(pid),
                        'imageUrl': player.get('image_path'), 'rating': rating, 'minutes': minutes,
                        'metrics': player_metric_values(row)})
    result = {'fixture_id': int(fixture['id']), 'league_id': int(fixture['league_id']),
              'season_id': int(fixture['season_id']), 'kickoff_at': kickoff(fixture['starting_at']),
              'home_team_id': locations['home'], 'away_team_id': locations['away'],
              'included_in_aggregation': fixture.get('state_id') in COMPLETED_STATES,
              'team_contributions': teams, 'player_contributions': players,
              'coverage': {'teamAdvancedVersion': TEAM_ADVANCED_VERSION, 'teamsWithStats': sum(bool(t['metrics']) for t in teams.values()),
                           'ratedPlayers': sum(p['rating'] is not None for p in players)}}
    result['source_fingerprint'] = hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()
    return result


def _write_rates(metrics, evidence, names):
    for name in names:
        state = evidence['rates'].get(rate_key(name))
        value = rate_value(name, state)
        if value is None:
            metrics.pop(name, None)
            continue
        metrics[name] = {'numerator': state['numerator'], 'denominator': state['denominator'],
                         'matchesCovered': state['coverage'], 'numeratorMatchesCovered': state['coverage'],
                         'rateValue': value}


def _hide_incomplete_attempts(metrics, evidence):
    incomplete = {RATES[name][1] for name, state in evidence.get('rates', {}).items()
                  if state.get('missing_attempts')}
    for name in list(metrics):
        if rate_key(name) in incomplete:
            metrics.pop(name)


class Aggregate:
    def __init__(self, *, include_team_metrics=True):
        self.include_team_metrics = include_team_metrics
        self.teams = {}
        self.players = {}
        self.fixture_count = 0
        self.latest = None

    def add(self, row):
        if not row['included_in_aggregation']:
            return
        self.fixture_count += 1
        dt = row['kickoff_at']
        self.latest = max(self.latest, dt) if self.latest else dt
        for tid, source in row['team_contributions'].items():
            team = self.teams.setdefault(tid, {'teamName': source['name'], 'matches': 0, 'metrics': {}})
            team['teamName'] = source['name']
            team['matches'] += 1
            if not self.include_team_metrics:
                continue
            metrics = source['metrics']
            add_sample(team.setdefault('_rate_inputs', empty()), metrics)
            for name, value in metrics.items():
                if name in DERIVED_PERCENTAGE_METRICS:
                    continue  # Never average provider percentages for known ratios.
                item = team['metrics'].setdefault(name, {'sum': 0.0, 'matchesCovered': 0})
                item['sum'] += value
                item['matchesCovered'] += 1
            _write_rates(team['metrics'], team['_rate_inputs'], DERIVED_PERCENTAGE_METRICS)
        for p in row['player_contributions']:
            key = f"{p['teamId']}:{p['playerId']}"
            item = self.players.setdefault(key, {**{k: v for k, v in p.items() if k != 'metrics'}, 'ratingSum': 0.0, 'ratedAppearances': 0, 'appearances': 0, 'minutes': 0.0, 'metricTotals': {}})
            item.update(name=p['name'], imageUrl=p.get('imageUrl'))
            item['appearances'] += 1
            item['minutes'] += p.get('minutes') or 0
            if p.get('rating') is not None:
                item['ratingSum'] += p['rating']
                item['ratedAppearances'] += 1
            metrics = p.get('metrics') or {}
            add_sample(item.setdefault('_rate_inputs', empty()), metrics)
            for name, value in metrics.items():
                if name not in PLAYER_COUNT_METRICS or numeric(value) is None:
                    continue
                total = item['metricTotals'].setdefault(name, {'sum': 0.0, 'matchesCovered': 0})
                total['sum'] += value
                total['matchesCovered'] += 1
                minutes = numeric(p.get('minutes'))
                if minutes is not None and minutes > 0:
                    total['minutesCovered'] = total.get('minutesCovered', 0) + minutes
                else:
                    total['missingMinutes'] = True
            _write_rates(item['metricTotals'], item['_rate_inputs'], PLAYER_RATE_METRICS)

    def finish(self, *, season_awards=False):
        catalog = {}
        for team in self.teams.values():
            if not self.include_team_metrics:
                continue
            _hide_incomplete_attempts(team['metrics'], team.get('_rate_inputs') or empty())
            for name, metric in team['metrics'].items():
                kind = 'derived_rate' if name in DERIVED_PERCENTAGE_METRICS else 'average' if name in AVERAGES or '%' in name or 'percentage' in name.casefold() or 'performance' in name.casefold() else 'total'
                if kind == 'derived_rate':
                    value = metric.get('rateValue')
                    per_match = value
                else:
                    per_match = metric['sum'] / metric['matchesCovered']
                    value = per_match if kind == 'average' else metric['sum']
                metric.update(value=round(value, 4) if value is not None else None,
                              perMatch=round(per_match, 4) if per_match is not None else None, aggregation=kind)
                catalog[name] = {'key': name, 'label': name, 'aggregation': kind,
                                 'unit': '%' if kind == 'derived_rate' or '%' in name or 'percentage' in name.casefold() else '',
                                 'direction': 'asc' if name in LOWER_BETTER else 'desc',
                                 'playerAwardSupported': name in PLAYER_AWARD_METRICS}
        def rank(p):
            return (-p['ratingSum'] / p['ratedAppearances'], -p['ratedAppearances'], -p['minutes'], p['playerId'], p['teamId'])
        winners = {}
        eligible = (
            p for p in self.players.values()
            if p['ratedAppearances'] and (
                not season_awards
                or p['appearances'] * 2 >= self.teams[str(p['teamId'])]['matches']
            )
        )
        for player in sorted(eligible, key=rank):
            tid = str(player['teamId'])
            if tid not in winners:
                winners[tid] = {k: v for k, v in player.items() if k not in {'rating', 'ratingSum', 'metricTotals', '_rate_inputs'}}
                winners[tid].update(averageRating=round(player['ratingSum'] / player['ratedAppearances'], 2), teamName=self.teams[tid]['teamName'])
        league_best = next(iter(winners.values()), None)
        return {'team_metrics': self.teams, 'metric_catalog': sorted(catalog.values(), key=lambda m: m['label']),
                'player_rating_totals': self.players, 'team_best_players': winners, 'league_best_player': league_best,
                'metric_player_awards': self.metric_awards(season_awards=season_awards),
                'fixture_count': self.fixture_count, 'latest_included_kickoff_at': self.latest,
                'coverage': {'teamCount': len(self.teams), 'ratedPlayerCount': sum(p['ratedAppearances'] > 0 for p in self.players.values())}}


    def metric_awards(self, *, season_awards=False):
        awards = {}
        for player in self.players.values():
            _hide_incomplete_attempts(player['metricTotals'], player.get('_rate_inputs') or empty())
        for name in sorted(PLAYER_AWARD_METRICS):
            for mode in ('total', 'perMatch', 'per90'):
                candidates = []
                for player in self.players.values():
                    team_matches = self.teams[str(player['teamId'])]['matches']
                    result = player_metric_result(player, team_matches, name, mode)
                    if result is None:
                        continue
                    value, total = result
                    candidates.append((value, player, total))
                def rank(candidate):
                    value, player, total = candidate
                    return (value if name in LOWER_BETTER else -value, -total['matchesCovered'], -player['minutes'], player['playerId'], player['teamId'])
                winners = {}
                for value, player, total in sorted(candidates, key=rank):
                    tid = str(player['teamId'])
                    if tid in winners:
                        continue
                    winner = {k: v for k, v in player.items() if k not in {'rating', 'ratingSum', 'metricTotals', '_rate_inputs'}}
                    winner.update(teamName=self.teams[tid]['teamName'],
                                  averageRating=round(player['ratingSum'] / player['ratedAppearances'], 2) if player['ratedAppearances'] else None,
                                  awardMetric=name, awardValue=round(value, 4), awardMode=mode,
                                  awardUnit='%' if name in PLAYER_RATE_METRICS else '', awardMatchesCovered=total['matchesCovered'])
                    if name in PLAYER_RATE_METRICS:
                        winner.update(awardNumerator=total['numerator'], awardDenominator=total['denominator'])
                    winners[tid] = winner
                # Keep empty supported entries: absence of data never falls back to ratings.
                awards.setdefault(name, {})[mode] = {'team_best_players': winners, 'league_best_player': next(iter(winners.values()), None)}
        return awards
