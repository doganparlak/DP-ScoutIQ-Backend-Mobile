"""Compact, mergeable standings statistics. No network, database or AI calls."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .metric_definitions import DERIVED_PERCENTAGE_METRICS, TEAM_METRIC_CATEGORY

VERSION = 1
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
                'Error Lead To Goal', 'Error Lead To Shot', 'Duels Lost'}


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
                        'imageUrl': player.get('image_path'), 'rating': rating, 'minutes': minutes})
    result = {'fixture_id': int(fixture['id']), 'league_id': int(fixture['league_id']),
              'season_id': int(fixture['season_id']), 'kickoff_at': kickoff(fixture['starting_at']),
              'home_team_id': locations['home'], 'away_team_id': locations['away'],
              'included_in_aggregation': fixture.get('state_id') in COMPLETED_STATES,
              'team_contributions': teams, 'player_contributions': players,
              'coverage': {'teamsWithStats': sum(bool(t['metrics']) for t in teams.values()),
                           'ratedPlayers': sum(p['rating'] is not None for p in players)}}
    result['source_fingerprint'] = hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()
    return result


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
            for name, value in metrics.items():
                if name in DERIVED_PERCENTAGE_METRICS:
                    continue  # Never average provider percentages for known ratios.
                item = team['metrics'].setdefault(name, {'sum': 0.0, 'matchesCovered': 0})
                item['sum'] += value
                item['matchesCovered'] += 1
            for name, (numerator, denominator) in DERIVED_PERCENTAGE_METRICS.items():
                n, d = metrics.get(numerator), metrics.get(denominator)
                if n is None or d is None or d < 0:
                    continue
                item = team['metrics'].setdefault(name, {'numerator': 0.0, 'denominator': 0.0, 'matchesCovered': 0})
                item['numerator'] += n
                item['denominator'] += d
                item['matchesCovered'] += 1
        for p in row['player_contributions']:
            key = f"{p['teamId']}:{p['playerId']}"
            item = self.players.setdefault(key, {**p, 'ratingSum': 0.0, 'ratedAppearances': 0, 'appearances': 0, 'minutes': 0.0})
            item.update(name=p['name'], imageUrl=p.get('imageUrl'))
            item['appearances'] += 1
            item['minutes'] += p.get('minutes') or 0
            if p.get('rating') is not None:
                item['ratingSum'] += p['rating']
                item['ratedAppearances'] += 1

    def finish(self):
        catalog = {}
        for team in self.teams.values():
            if not self.include_team_metrics:
                continue
            for name, metric in team['metrics'].items():
                kind = 'derived_rate' if name in DERIVED_PERCENTAGE_METRICS else 'average' if name in AVERAGES or '%' in name or 'percentage' in name.casefold() or 'performance' in name.casefold() else 'total'
                if kind == 'derived_rate':
                    value = metric['numerator'] / metric['denominator'] * 100 if metric['denominator'] else None
                    per_match = value
                else:
                    per_match = metric['sum'] / metric['matchesCovered']
                    value = per_match if kind == 'average' else metric['sum']
                metric.update(value=round(value, 4) if value is not None else None,
                              perMatch=round(per_match, 4) if per_match is not None else None, aggregation=kind)
                catalog[name] = {'key': name, 'label': name, 'aggregation': kind,
                                 'unit': '%' if kind == 'derived_rate' or '%' in name or 'percentage' in name.casefold() else '',
                                 'direction': 'asc' if name in LOWER_BETTER else 'desc'}
        def rank(p):
            return (-p['ratingSum'] / p['ratedAppearances'], -p['ratedAppearances'], -p['minutes'], p['playerId'], p['teamId'])
        winners = {}
        for player in sorted((p for p in self.players.values() if p['ratedAppearances']), key=rank):
            tid = str(player['teamId'])
            if tid not in winners:
                winners[tid] = {k: v for k, v in player.items() if k not in {'rating', 'ratingSum'}}
                winners[tid].update(averageRating=round(player['ratingSum'] / player['ratedAppearances'], 2), teamName=self.teams[tid]['teamName'])
        league_best = next(iter(winners.values()), None)
        return {'team_metrics': self.teams, 'metric_catalog': sorted(catalog.values(), key=lambda m: m['label']),
                'player_rating_totals': self.players, 'team_best_players': winners, 'league_best_player': league_best,
                'fixture_count': self.fixture_count, 'latest_included_kickoff_at': self.latest,
                'coverage': {'teamCount': len(self.teams), 'ratedPlayerCount': sum(p['ratedAppearances'] > 0 for p in self.players.values())}}
