"""Mobile league-performance entitlements; shared caches remain unmodified."""
from copy import deepcopy

FREE_METRICS = frozenset({'Goals', 'Shots Total', 'Shots On Target', 'Ball Possession %', 'Corners'})
PLUS_METRICS = FREE_METRICS | frozenset({
    'Passes', 'Accurate Passes (%)', 'Fouls', 'Attacks', 'Dangerous Attacks',
    'Big Chances Created', 'Shots Insidebox', 'Shots Outsidebox', 'Accurate Passes',
    'Key Passes', 'Total Crosses', 'Saves', 'Interceptions', 'Yellow Cards', 'Red Cards',
})


def restrict_insights(summary, plan):
    result = deepcopy(summary)
    tier = 'pro' if plan in {'Pro Monthly', 'Pro Yearly'} else 'plus' if plan == 'No Ads Monthly' else 'free'
    allowed = None if tier == 'pro' else PLUS_METRICS if tier == 'plus' else FREE_METRICS
    result['access'] = {'tier': tier, 'biweekly': tier != 'free'}
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
    if tier == 'free':
        result['biweekly'] = None
        result['periods'] = []
    return result
