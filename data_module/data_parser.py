import __main__
import os
import json
import pandas as pd
from collections import defaultdict, Counter
from typing import Dict, List
from stats_module.stats_engine import compute_player_stats

# Paths
BASE_DIR = os.path.join(os.path.dirname(__file__), '..', 'data_module')
EVENTS_DIR = os.path.join(BASE_DIR, 'events')
LINEUPS_DIR = os.path.join(BASE_DIR, 'lineups')

def load_json(path: str):
    with open(path, 'r') as f:
        return json.load(f)

def load_match_events(match_id: str) -> pd.DataFrame:
    path = os.path.join(EVENTS_DIR, f"{match_id}.json")
    events = load_json(path)
    df = pd.json_normalize(events)
    df.columns = [col.replace('.', '_') for col in df.columns]
    df['match_id'] = match_id
    return df

def load_match_lineups(match_id: str) -> Dict[str, Dict]:
    """Returns {player_name: {"player_id": ..., "nationality": ..., "positions": [...]}}"""
    path = os.path.join(LINEUPS_DIR, f"{match_id}.json")
    lineups = load_json(path)
    players_info = {}

    for team in lineups:
        for player in team['lineup']:
            name = player['player_name']
            player_id = player['player_id']
            nationality = player.get('country', {}).get('name', 'Unknown')
            position_raw = player['positions']
            positions = []
            for p in position_raw:
                positions.append(p['position'])
            if name not in players_info:
                players_info[name] = {
                    "player_id": player_id,
                    "nationality": nationality,
                    "positions": positions
                }
            else:
                players_info[name]["positions"].append(positions)

    return players_info

def compute_match_player_stats(df: pd.DataFrame, players: List[str]) -> Dict[str, Dict]:
    match_stats = {}
    for player in players:
        player_stats = compute_player_stats(df, player)
        match_stats[player] = player_stats
    return match_stats

def aggregate_stats(stats_list: List[Dict[str, float]]) -> Dict[str, float]:
    """
    Average all player stats across matches.
    Both count and percentage fields are averaged across available matches.
    """

    sum_stats = defaultdict(float)
    entry_counts = defaultdict(int)

    for stats in stats_list:
        for k, v in stats.items():
            if isinstance(v, (int, float)):
                sum_stats[k] += v
                entry_counts[k] += 1

    averaged = {
        k: round(sum_stats[k] / entry_counts[k], 2) if entry_counts[k] > 0 else 0.0
        for k in sum_stats
    }

    return averaged


def compute_all_players_stats(save_data = False, verbose = False) -> pd.DataFrame:
    match_ids = sorted(set(
        f.replace('.json', '') for f in os.listdir(EVENTS_DIR)
    ).intersection(
        f.replace('.json', '') for f in os.listdir(LINEUPS_DIR)
    ))

    player_stats_by_name = defaultdict(list)
    player_meta = {}
    i = 0
    for match_id in match_ids:
        if verbose and i % 100 == 0:
            print(i)
        df = load_match_events(match_id)
        players = df['player_name'].dropna().unique()
        lineups = load_match_lineups(match_id)
        for player in players:
            stats = compute_player_stats(df, player)
            player_stats_by_name[player].append(stats)

            if player in lineups:
                if player not in player_meta:
                    meta = lineups[player]
                    # Count positions for ordering later
                    player_meta[player] = {
                        "player_id": meta["player_id"],
                        "nationality": meta["nationality"],
                        "positions": Counter(meta["positions"])
                    }
                else:
                    player_meta[player]["positions"].update(lineups[player]["positions"])
        i += 1
    # Final aggregation
    final_data = []
    for player, stats_list in player_stats_by_name.items():
        aggregated = aggregate_stats(stats_list)
        meta = player_meta.get(player, {})
        positions = list(
            pos for pos, _ in sorted(meta.get("positions", {}).items(), key=lambda x: -x[1])
        )
        final_data.append({
            "player_name": player,
            "player_id": meta.get("player_id", None),
            "match_count": len(stats_list),
            "nationality": meta.get("nationality", None),
            "position": positions[0] if positions else None,
            "position_2": positions[1] if len(positions) > 1 else None,
            "position_3": positions[2] if len(positions) > 2 else None,
            **aggregated
        })
    
    df_final = pd.DataFrame(final_data)
    if save_data == True:
        output_path = os.path.join(BASE_DIR, 'player_level_stats.csv')
        df_final.to_csv(output_path, index=False)

    return df_final

if __name__ == "__main__":
    compute_all_players_stats(save_data = True, verbose=False)
