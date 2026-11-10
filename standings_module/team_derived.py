"""Mobile League Analysis team-only views over published paired match data."""
from threading import Lock

from sqlalchemy import text

from report_module.rate_evidence import empty, add_sample, key as rate_key, rate_value
from .stages import EUROPEAN_LEAGUES
from .metric_definitions import TEAM_METRIC_CATEGORY
from .metrics import LOWER_BETTER

VERSION = 2
_READY = False
_LOCK = Lock()

# key -> (recorded source name, category, preferred direction).
MIRRORS = {
    'Opponent Attacks': ('Attacks', 'opponent_threat', 'lower'),
    'Opponent Dangerous Attacks': ('Dangerous Attacks', 'opponent_threat', 'lower'),
    'Opponent Big Chances Created': ('Big Chances Created', 'opponent_threat', 'lower'),
    'Opponent Shots Total': ('Shots Total', 'opponent_threat', 'lower'),
    'Opponent Shots On Target': ('Shots On Target', 'opponent_threat', 'lower'),
    'Opponent Shots Insidebox': ('Shots Insidebox', 'opponent_threat', 'lower'),
    'Opponent Shots Outsidebox': ('Shots Outsidebox', 'opponent_threat', 'lower'),
    'Expected Goals Against (xGA)': ('Expected Goals', 'opponent_threat', 'lower'),
    'Expected Goals On Target Against (xGoTA)': ('Expected Goals On Target', 'opponent_threat', 'lower'),
    'Opponent Key Passes': ('Key Passes', 'opponent_threat', 'lower'),
    'Opponent Total Crosses': ('Total Crosses', 'opponent_threat', 'lower'),
    'Opponent Accurate Crosses': ('Accurate Crosses', 'opponent_threat', 'lower'),
    'Opponent Dribble Attempts': ('Dribble Attempts', 'opponent_threat', 'lower'),
    'Opponent Successful Dribbles': ('Successful Dribbles', 'opponent_threat', 'lower'),
    'Opponent Corners': ('Corners', 'opponent_threat', 'lower'),
    'Opponent Big Chances Missed': ('Big Chances Missed', 'opponent_threat', 'lower'),
    'Opponent Hit Woodwork': ('Hit Woodwork', 'opponent_threat', 'lower'),
    'Opponent Passes': ('Passes', 'opponent_profile', 'neutral'),
    'Opponent Accurate Passes': ('Accurate Passes', 'opponent_profile', 'neutral'),
    'Opponent Long Balls': ('Long Balls', 'opponent_profile', 'neutral'),
    'Opponent Long Balls Won': ('Long Balls Won', 'opponent_profile', 'neutral'),
    'Opponent Tackles Won': ('Tackles Won', 'opponent_profile', 'neutral'),
    'Opponent Interceptions': ('Interceptions', 'opponent_profile', 'neutral'),
    'Opponent Saves': ('Saves', 'opponent_profile', 'neutral'),
    'Opponent Counter Attacks': ('Counter Attacks', 'opponent_profile', 'neutral'),
}
RATES = {
    'Opponent Shots On Target (%)': ('Shots On Target (%)', 'opponent_threat', 'lower'),
    'Opponent Goal Conversion (%)': ('Goal Conversion (%)', 'opponent_threat', 'lower'),
    'Opponent On Target Goal Conversion (%)': ('On Target Goal Conversion (%)', 'opponent_threat', 'lower'),
    'Opponent Accurate Crosses (%)': ('Accurate Crosses (%)', 'opponent_threat', 'lower'),
    'Opponent Dribble Accuracy (%)': ('Dribble Accuracy (%)', 'opponent_threat', 'lower'),
    'Opponent Shot Quality (%)': ('Shot Quality (%)', 'opponent_threat', 'lower'),
    'Opponent On Target Shot Quality (%)': ('On Target Shot Quality (%)', 'opponent_threat', 'lower'),
    'Opponent Accurate Passes (%)': ('Accurate Passes (%)', 'opponent_profile', 'neutral'),
    'Opponent Long Balls Won (%)': ('Long Balls Won (%)', 'opponent_profile', 'neutral'),
}
BALANCES = {
    'Dangerous Attacks Difference': ('Dangerous Attacks', 'contribution_impact', 'higher'),
    'Shots Difference': ('Shots Total', 'shooting', 'higher'),
    'Shots On Target Difference': ('Shots On Target', 'shooting', 'higher'),
}
DEFINITIONS = {**MIRRORS, **RATES, **BALANCES,
               'Shot Share (%)': ('Shots Total', 'shooting', 'higher'),
               'Expected Goals Difference (xGD)': ('Expected Goals Difference (xGD)', 'expected', 'higher')}


def metric_sort_direction(name):
    """Neutral opponent profiles sort low first without implying better output."""
    return 'asc' if DEFINITIONS[name][2] in {'lower', 'neutral'} else 'desc'


def number(value):
    from math import isfinite
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (ValueError, TypeError):
        return None


def build_team_derived(rows):
    teams = {}
    for row in rows:
        paired = row.get('team_contributions') or {}
        if len(paired) != 2:
            continue
        for tid, team in paired.items():
            own = team.get('metrics') or {}
            other = next(other for key, other in paired.items() if key != tid)
            opponent = {**(other.get('metrics') or {}), **(other.get('opponent_inputs') or {})}
            state = teams.setdefault(str(tid), {'counts': {}, 'rates': empty(), 'share': [0., 0., 0, False]})
            add_sample(state['rates'], opponent)
            values = {name: number(opponent.get(source)) for name, (source, _, _) in MIRRORS.items()}
            # Prefer the supplied xGA. Never reconstruct the provider's xGD.
            supplied_xga = number(own.get('Expected Goals Against (xGA)'))
            if supplied_xga is not None:
                values['Expected Goals Against (xGA)'] = supplied_xga
            values['Expected Goals Difference (xGD)'] = number(own.get('Expected Goals Difference (xGD)'))
            for name, (source, _, _) in BALANCES.items():
                a, b = number(own.get(source)), number(opponent.get(source))
                values[name] = a - b if a is not None and b is not None and a >= 0 and b >= 0 else None
            for name, value in values.items():
                if value is None or value < 0 and name not in BALANCES and name != 'Expected Goals Difference (xGD)':
                    continue
                total = state['counts'].setdefault(name, [0., 0])
                total[0] += value
                total[1] += 1
            a, b = number(own.get('Shots Total')), number(opponent.get('Shots Total'))
            share = state['share']
            if a is not None and b is not None and a >= 0 and b >= 0:
                share[0] += a
                share[1] += a + b
                share[2] += 1
            elif (a or 0) > 0 or (b or 0) > 0:
                share[3] = True
    catalog, result = {}, {}
    for tid, state in teams.items():
        metrics = {name: {'value': round(total, 4), 'perMatch': round(total / covered, 4),
                          'matchesCovered': covered, 'aggregation': 'total'}
                   for name, (total, covered) in state['counts'].items()}
        for name, (source, _, _) in RATES.items():
            rate = state['rates']['rates'].get(rate_key(source))
            value = rate_value(source, rate)
            if value is not None:
                metrics[name] = {'value': round(value, 4), 'perMatch': round(value, 4),
                                 'matchesCovered': int(rate['coverage']), 'aggregation': 'derived_rate'}
        a, b, covered, invalid = state['share']
        if b > 0 and not invalid:
            metrics['Shot Share (%)'] = {'value': round(a / b * 100, 4), 'perMatch': round(a / b * 100, 4),
                                         'matchesCovered': covered, 'aggregation': 'derived_rate'}
        for name, metric in metrics.items():
            _, category, preference = DEFINITIONS[name]
            catalog[name] = {'key': name, 'label': name, 'aggregation': metric['aggregation'],
                             'unit': '%' if metric['aggregation'] == 'derived_rate' else '',
                             'category': category, 'preference': preference,
                             'direction': metric_sort_direction(name), 'playerAwardSupported': False}
        result[tid] = metrics
    return {'teams': result, 'metric_catalog': list(catalog.values())}


def ensure_schema(db):
    global _READY
    if _READY:
        return
    with _LOCK:
        if _READY:
            return
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('mobile-team-derived-schema',0))"))
        db.execute(text("""CREATE TABLE IF NOT EXISTS mobile_league_team_derived (
            league_id bigint NOT NULL,season_id bigint NOT NULL,scope text NOT NULL,
            calculation_version integer NOT NULL,source_computed_at timestamptz NOT NULL,
            payload jsonb NOT NULL,PRIMARY KEY(league_id,season_id,scope))"""))
        db.execute(text('ALTER TABLE mobile_league_team_derived ENABLE ROW LEVEL SECURITY'))
        db.commit()
        _READY = True


def _overlay(summary, payload):
    for tid, metrics in payload['teams'].items():
        if tid in summary['team_metrics']:
            summary['team_metrics'][tid]['metrics'].update(metrics)
    existing = {metric['key']: metric for metric in summary.get('metric_catalog') or []}
    # Sorting is presentation metadata: update old cached catalogs without
    # invalidating their arithmetic or fetching source fixtures again.
    existing.update({metric['key']: {**metric, 'direction': metric_sort_direction(metric['key'])}
                     for metric in payload['metric_catalog']})
    categories = {canonical: category for category, canonical in TEAM_METRIC_CATEGORY.values()}
    categories.update({
        'Assists': 'passing', 'Substitutions': 'contribution_impact',
        'Hit Woodwork': 'shooting', 'Shots On Target (%)': 'shooting',
        'Goal Conversion (%)': 'shooting', 'On Target Goal Conversion (%)': 'shooting',
        'Shot Quality (%)': 'shooting', 'On Target Shot Quality (%)': 'shooting',
        'Assist Efficiency (%)': 'passing',
        **{name: 'defending' for name in ('Clearances', 'Total Duels', 'Aerials', 'Aerials Won',
             'Tackles Won', 'Ball Recovery', 'Tackles Won (%)', 'Aerials Won (%)', 'Duels Won (%)')},
        **{name: 'errors_discipline' for name in ('Duels Lost', 'Possession Lost', 'Dispossessed',
             'Error Lead To Goal', 'Error Lead To Shot')},
    })
    for name, metric in existing.items():
        if name in DEFINITIONS:
            continue
        category = 'expected' if name.startswith('Expected ') else categories.get(name, 'other')
        # All error/discipline rankings use low-first ordering.
        lower = category == 'errors_discipline' or name in LOWER_BETTER
        metric.update(category=category, preference='lower' if lower else 'higher',
                      direction='asc' if lower else 'desc')
    summary['metric_catalog'] = list(existing.values())


def enrich_team_derived(db, league, season, scope, summary, published, status):
    """Overlay derived metrics without modifying shared published summaries."""
    import json
    if published is None or not summary.get('team_metrics'):
        return False
    ensure_schema(db)
    params = {'league': league, 'season': season, 'scope': scope, 'version': VERSION, 'published': published}
    saved = db.execute(text('''SELECT payload,source_computed_at,calculation_version FROM mobile_league_team_derived
        WHERE league_id=:league AND season_id=:season AND scope=:scope'''), params).mappings().first()
    fresh = saved and saved['source_computed_at'] == published and saved['calculation_version'] == VERSION
    if not fresh:
        if status != 'ready':
            if saved:
                _overlay(summary, saved['payload'])
            return status in {'pending', 'processing'}
        acquired = db.execute(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))'),
                              {'key': f'mobile-team-derived:{league}:{season}:{scope}'}).scalar()
        if not acquired:
            return True
        source = db.execute(text('''SELECT team_contributions,updated_at FROM league_match_contributions
            WHERE league_id=:league AND season_id=:season AND included_in_aggregation=TRUE
            AND kickoff_at<=:published AND (:european=FALSE OR
                (:scope='overall' AND coverage->>'scope' IN ('league','knockout')) OR coverage->>'scope'=:scope)
            ORDER BY kickoff_at,fixture_id''').execution_options(stream_results=True, yield_per=30),
            {**params, 'european': league in EUROPEAN_LEAGUES}).mappings()
        count = 0
        def compatible():
            nonlocal count
            for row in source:
                if row['updated_at'] > published:
                    raise RuntimeError('Team fixture updates await publication')
                count += 1
                yield row
        try:
            payload = build_team_derived(compatible())
        except RuntimeError:
            source.close()
            db.rollback()
            return True
        if count != summary.get('fixture_count'):
            db.rollback()
            return True
        db.execute(text('''INSERT INTO mobile_league_team_derived
            (league_id,season_id,scope,calculation_version,source_computed_at,payload)
            VALUES (:league,:season,:scope,:version,:published,CAST(:payload AS jsonb))
            ON CONFLICT(league_id,season_id,scope) DO UPDATE SET calculation_version=EXCLUDED.calculation_version,
            source_computed_at=EXCLUDED.source_computed_at,payload=EXCLUDED.payload'''),
            {**params, 'payload': json.dumps(payload, allow_nan=False)})
        db.commit()
    else:
        payload = saved['payload']
    _overlay(summary, payload)
    return False
