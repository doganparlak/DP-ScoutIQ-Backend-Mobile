"""Mobile league-performance entitlements; shared caches remain unmodified."""
from copy import deepcopy

PLUS_METRICS = frozenset({'Goals', 'Shots Total', 'Shots On Target', 'Passes', 'Ball Possession %', 'Accurate Passes (%)', 'Corners', 'Fouls'})


def restrict_insights(summary, plan):
    result = deepcopy(summary)
    tier = 'pro' if plan in {'Pro Monthly', 'Pro Yearly'} else 'plus' if plan == 'No Ads Monthly' else 'free'
    allowed = None if tier == 'pro' else PLUS_METRICS if tier == 'plus' else frozenset()
    result['access'] = {'tier': tier, 'biweekly': tier != 'free'}
    season = result['season']
    season['metric_catalog'] = [{**metric, 'locked': allowed is not None and metric['key'] not in allowed}
                                for metric in season.get('metric_catalog', [])]
    if allowed is not None:
        season['team_metrics'] = {key: {**team, 'metrics': {name: value for name, value in team.get('metrics', {}).items() if name in allowed}}
                                  for key, team in season.get('team_metrics', {}).items()}
    if tier == 'free':
        result['biweekly'] = None
        result['periods'] = []
    return result
