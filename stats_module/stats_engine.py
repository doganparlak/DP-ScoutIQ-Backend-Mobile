import pandas as pd
from typing import Dict

def compute_player_stats(df: pd.DataFrame, player_name: str) -> Dict[str, float]:
    player_df = df[df['player_name'] == player_name]
    try:
        mins = round(player_df['minute'].max() + player_df['second'].max() * 0.01)
    except:
        mins = 0

    stats = {'Minutes': mins}

    def safe_count(condition):
        try:
            return len(player_df[condition])
        except:
            return 0

    def ratio(numerator, denominator):
        try:
            return round((numerator / denominator * 100) if denominator > 0 else 0, 2)
        except:
            return 0

    # GOALKEEPER STATS
    for tech in ['Diving', 'Standing']:
        try:
            stats[tech] = len(player_df[player_df['goalkeeper_technique_name'] == tech])
        except:
            stats[tech] = 0

    for part in ['Head', 'Both Hands', 'Right Hand', 'Left Hand', 'Right Foot', 'Left Foot']:
        try:
            stats[part] = len(player_df[player_df['goalkeeper_body_part_name'] == part])
        except:
            stats[part] = 0

    for action in ['Shot Faced', 'Shot Saved', 'Penalty Conceded', 'Collected', 'Punch', 'Smother', 'Keeper Sweeper']:
        try:
            stats[action] = len(player_df[player_df['goalkeeper_type_name'] == action])
        except:
            stats[action] = 0

    for outcome in ['Success', 'Lost in Play', 'Clear', 'No Touch', 'In Play Safe', 'In Play Danger', 'Touched Out', 'Touched In']:
        try:
            stats[outcome] = len(player_df[player_df['goalkeeper_outcome_name'] == outcome])
        except:
            stats[outcome] = 0

    # IN POSSESSION
    try:
        shots = player_df[player_df['type_name'] == 'Shot']
    except:
        shots = pd.DataFrame()

    try:
        goals = shots[shots['shot_outcome_name'] == 'Goal']
    except:
        goals = pd.DataFrame()

    try:
        passes = player_df[player_df['type_name'] == 'Pass']
    except:
        passes = pd.DataFrame()

    try:
        dribbles = player_df[player_df['type_name'] == 'Dribble']
    except:
        dribbles = pd.DataFrame()

    try:
        crosses = passes[passes['pass_cross'] == True]
    except:
        crosses = pd.DataFrame()

    try:
        carries = player_df[player_df['type_name'] == 'Carry']
    except:
        carries = pd.DataFrame()

    try:
        stats['Shots'] = len(shots)
    except:
        stats['Shots'] = 0

    try:
        stats['Shot Accuracy (%)'] = ratio(len(shots[shots['shot_outcome_name'].isin(['Saved', 'Goal', 'Saved to Post'])]), len(shots))
    except:
        stats['Shot Accuracy (%)'] = 0

    try:
        stats['Goals'] = len(goals)
    except:
        stats['Goals'] = 0

    try:
        stats['Assists'] = len(player_df[player_df['pass_goal_assist'] == True])
    except:
        stats['Assists'] = 0

    try:
        stats['xG'] = round(shots['shot_statsbomb_xg'].sum(), 2)
    except:
        stats['xG'] = 0

    try:
        stats['Key Passes'] = passes['pass_assisted_shot_id'].notna().sum()
    except:
        stats['Key Passes'] = 0

    try:
        stats['Passes Attempted'] = len(passes)
    except:
        stats['Passes Attempted'] = 0

    try:
        stats['Pass Accuracy (%)'] = ratio(passes['pass_outcome_name'].isna().sum(), len(passes))
    except:
        stats['Pass Accuracy (%)'] = 0

    try:
        stats['Dribbles'] = len(dribbles)
    except:
        stats['Dribbles'] = 0

    try:
        stats['Dribble Accuracy (%)'] = ratio(len(dribbles[dribbles['dribble_outcome_name'] == 'Complete']), len(dribbles))
    except:
        stats['Dribble Accuracy (%)'] = 0

    try:
        stats['Crosses Attempted'] = len(crosses)
    except:
        stats['Crosses Attempted'] = 0

    try:
        stats['Cross Accuracy (%)'] = ratio(crosses['pass_outcome_name'].isna().sum(), len(crosses))
    except:
        stats['Cross Accuracy (%)'] = 0

    try:
        stats['Carries'] = len(carries)
    except:
        stats['Carries'] = 0

    # OUT OF POSSESSION
    try:
        duels = player_df[player_df['type_name'] == 'Duel']
    except:
        duels = pd.DataFrame()

    try:
        stats['Pressures'] = len(player_df[player_df['type_name'] == 'Pressure'])
    except:
        stats['Pressures'] = 0

    try:
        stats['Counterpressures'] = len(player_df[player_df['counterpress'] == True])
    except:
        stats['Counterpressures'] = 0

    try:
        stats['Interceptions'] = player_df[(player_df["type_name"] == 'Interception') & (player_df["interception_outcome_name"].isin(['Won', 'Success In Play']))].shape[0]
    except:
        stats['Interceptions'] = 0

    try:
        stats['Fouls'] = len(player_df[player_df['type_name'] == 'Foul Committed'])
    except:
        stats['Fouls'] = 0

    try:
        stats['Blocks'] = len(player_df[player_df['type_name'] == 'Block'])
    except:
        stats['Blocks'] = 0

    try:
        stats['Duels Attempted'] = duels.shape[0]
    except:
        stats['Duels Attempted'] = 0

    try:
        stats['Duel Won Accuracy (%)'] = ratio(duels[duels["duel_outcome_name"].isin(['Won', 'Success In Play'])].shape[0], duels.shape[0])
    except:
        stats['Duel Won Accuracy (%)'] = 0

    try:
        stats['Ball Recoveries'] = len(player_df[player_df['type_name'] == 'Ball Recovery'])
    except:
        stats['Ball Recoveries'] = 0

    try:
        stats['Clearances'] = len(player_df[player_df['type_name'] == 'Clearance'])
    except:
        stats['Clearances'] = 0

    return stats

