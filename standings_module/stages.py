"""Competition scope shared by official tables and statistical summaries."""
EUROPEAN_LEAGUES = frozenset({2, 5, 2286})
SCOPES = ('league', 'knockout', 'overall')


def stage_scope(stage):
    # Qualifying play-offs must never be confused with knockout play-offs.
    kind = stage.get('type_id')
    return {223: 'league', 224: 'knockout', 225: 'qualifying'}.get(kind, 'unknown')


def scoped_schedule(stages, league_id):
    fixtures = {}
    for stage in stages:
        scope = stage_scope(stage) if league_id in EUROPEAN_LEAGUES else 'overall'
        if scope not in SCOPES:
            continue
        for source in [stage, *(stage.get('rounds') or []), *(stage.get('aggregates') or [])]:
            for fixture in source.get('fixtures') or []:
                fixtures[int(fixture['id'])] = {**fixture, '_scope': scope}
    return fixtures


def knockout_rounds(stages):
    result = []
    for stage in sorted(stages, key=lambda s: (s.get('sort_order') or 0, s['id'])):
        if stage_scope(stage) != 'knockout':
            continue
        fixtures = scoped_schedule([stage], 2)
        matches = []
        for fixture in sorted(fixtures.values(), key=lambda f: (f.get('starting_at') or '', f['id'])):
            participants = {(p.get('meta') or {}).get('location'): p for p in fixture.get('participants') or []}
            def team(location):
                p = participants.get(location) or {}
                return {'id': p.get('id'), 'name': p.get('name'), 'imageUrl': p.get('image_path')}
            scores = {}
            penalties = {}
            for score in fixture.get('scores') or []:
                value = score.get('score') or {}
                if score.get('description') == 'CURRENT':
                    scores[value.get('participant')] = value.get('goals')
                elif score.get('description') == 'PENALTY_SHOOTOUT':
                    penalties[value.get('participant')] = value.get('goals')
            matches.append({'id': fixture['id'], 'aggregateId': fixture.get('aggregate_id'),
                            'startingAt': fixture.get('starting_at'), 'stateId': fixture.get('state_id'),
                            'placeholder': bool(fixture.get('placeholder')), 'leg': fixture.get('leg'),
                            'home': team('home'), 'away': team('away'),
                            'homeScore': scores.get('home'), 'awayScore': scores.get('away'),
                            'homePenalties': penalties.get('home'), 'awayPenalties': penalties.get('away')})
        result.append({'id': stage['id'], 'label': stage.get('name') or '', 'matches': matches})
    return result
