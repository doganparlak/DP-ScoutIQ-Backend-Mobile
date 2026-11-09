"""Remove legacy player rankings outside player comparison."""
from copy import deepcopy

def strip_player_ranks(usage):
    """Discard legacy standout-player ranks, retaining team and comparison ranks."""
    result = deepcopy(usage)
    for team in result.get('teams') or []:
        summary = deepcopy(team.get('performance_summary') or {})
        if 'performance_summary' in team:
            team['performance_summary'] = summary
        for key in ('featured_players', 'top_scorers', 'top_rated', 'top_scorer', 'development'):
            entries = summary.get(key)
            for player in entries if isinstance(entries, list) else [entries]:
                if not isinstance(player, dict):
                    continue
                player.pop('rating_competition_rank', None)
                player.pop('league_ranking_highlights', None)
                for group in ('standout_metrics', 'development_metrics'):
                    for metric in player.get(group) or []:
                        metric.pop('competition_rank', None)
    return result
