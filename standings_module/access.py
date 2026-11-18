"""Mobile league-performance entitlements; shared caches remain unmodified."""
from copy import deepcopy

FREE_METRICS = frozenset({
    'Goals', 'Shots Total', 'Shots On Target', 'Ball Possession %', 'Corners',
    'Fouls', 'Opponent Shots Total', 'Opponent Passes', 'Expected Goals',
})
PLUS_METRICS = FREE_METRICS | frozenset({
    'Passes', 'Accurate Passes (%)', 'Attacks', 'Dangerous Attacks',
    'Big Chances Created', 'Shots Insidebox', 'Shots Outsidebox', 'Accurate Passes',
    'Key Passes', 'Total Crosses', 'Saves', 'Interceptions', 'Yellow Cards', 'Red Cards',
    'Penalties Scored', 'Opponent Shots Outsidebox', 'Opponent Shots Insidebox',
    'Opponent Accurate Passes', 'Opponent Accurate Passes (%)', 'Expected Points (xPTS)',
})


def restrict_insights(summary, plan, *, rewarded_biweekly=False, rewarded_metrics=()):
    result = deepcopy(summary)
    tier = 'pro' if plan in {'Pro Monthly', 'Pro Yearly'} else 'plus' if plan == 'No Ads Monthly' else 'free'
    allowed = None if tier == 'pro' else PLUS_METRICS if tier == 'plus' else FREE_METRICS
    if allowed is not None and rewarded_metrics:
        catalog_keys = {metric['key'] for metric in result['season'].get('metric_catalog', [])}
        allowed = allowed | catalog_keys.intersection(rewarded_metrics)
    can_view_biweekly = tier != 'free' or rewarded_biweekly
    result['access'] = {'tier': tier, 'biweekly': can_view_biweekly}
    # Enterprise-only award/leaderboard payloads are never exposed by mobile.
    result.pop('playerAwardMetrics', None)
    for summary in (result['season'], result.get('biweekly')):
        if summary:
            summary.pop('metric_player_awards', None)
            summary.pop('player_rating_totals', None)
            for team in summary.get('team_metrics', {}).values():
                team.pop('_rate_inputs', None)
    season = result['season']
    season['metric_catalog'] = [{**metric, 'locked': allowed is not None and metric['key'] not in allowed,
                                'requiredTier': 'free' if metric['key'] in FREE_METRICS else 'plus' if metric['key'] in PLUS_METRICS else 'pro'}
                                for metric in season.get('metric_catalog', [])]
    if allowed is not None:
        season['team_metrics'] = {key: {**team, 'metrics': {name: value for name, value in team.get('metrics', {}).items() if name in allowed}}
                                  for key, team in season.get('team_metrics', {}).items()}
    if not can_view_biweekly:
        result['biweekly'] = None
        result['periods'] = []
    return result
