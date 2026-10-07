"""League-fit evidence mirrors Enterprise toSpiderPoints/buildProComparisonEvidence.

Names/categories follow enterprise-spider-ranges.ts; counts use average minutes,
percentages/ratings retain their scale, and available derived ratios take priority.
"""
import math
import re

LABELS = ['Blocked Shots', 'Tackles Won', 'Big Chances Missed', 'Chances Created', 'Goals Conceded', 'Long Balls Won', 'Successful Crosses (%)', 'Last Man Tackle', 'Accurate Passes (%)', 'Aerials Won (%)', 'Fouls', 'Hit Woodwork', 'Total Duels', 'Accurate Passes', 'Error Lead To Goal', 'Error Lead To Shot', 'Key Passes', 'Penalties Missed', 'Yellow Cards', 'Duels Won', 'Rating', 'Shots Total', 'Shots On Target (%)', 'Expected Goals', 'Expected Goals On Target', 'Shooting Performance', 'Shot Quality (%)', 'On-Target Shot Quality (%)', 'Goal Conversion (%)', 'On-Target to Goal Conversion (%)', 'Assist Efficiency (%)', 'Dribble Accuracy (%)', 'Total Crosses', 'Passes', 'Offsides', 'Aerials Lost', 'Penalties Committed', 'Possession Lost', 'Long Balls', 'Aerials Won', 'Clearances', 'Man Of Match', 'Match Count', 'Ball Recovery', 'Red Cards', 'Accurate Crosses', 'Goals', 'Offsides Provoked', 'Aerials', 'Saves', 'Touches', 'Assists', 'Minutes Played', 'Dribble Attempts', 'Tackles', 'Turn Over', 'Fouls Drawn', 'Big Chances Created', 'Long Balls Won (%)', 'Penalties Scored', 'Penalties Won', 'Duels Lost', 'Penalties Saved', 'Saves Insidebox', 'Shots Off Target', 'Good High Claim', 'Dispossessed', 'Shots On Target', 'Through Balls Won', 'Duels Won (%)', 'Punches', 'Successful Dribbles', 'Tackles Won (%)', 'Interceptions', 'Yellow & Red Cards', 'Backward Passes', 'Captain', 'Own Goals', 'Dribbled Past', 'Clearance Offline', 'Through Balls', 'Passes In Final Third']
CATEGORIES = {'contribution_impact': ['Match Count', 'Minutes Played', 'Penalties Won', 'Touches', 'Big Chances Created', 'Dribble Attempts', 'Successful Dribbles', 'Dribble Accuracy (%)', 'Man Of Match', 'Rating', 'Captain', 'Fouls Drawn', 'Offsides Provoked'], 'goalkeeping': ['Saves', 'Saves Insidebox', 'Penalties Saved', 'Punches', 'Good High Claim'], 'shooting': ['Shots Total', 'Shots On Target', 'Shots On Target (%)', 'Expected Goals', 'Expected Goals On Target', 'Shooting Performance', 'Shot Quality (%)', 'On-Target Shot Quality (%)', 'Goal Conversion (%)', 'On-Target to Goal Conversion (%)', 'Goals', 'Hit Woodwork', 'Penalties Scored'], 'passing': ['Assists', 'Assist Efficiency (%)', 'Long Balls', 'Long Balls Won', 'Long Balls Won (%)', 'Total Crosses', 'Accurate Crosses', 'Successful Crosses (%)', 'Passes', 'Accurate Passes', 'Accurate Passes (%)', 'Backward Passes', 'Key Passes', 'Passes In Final Third', 'Through Balls', 'Through Balls Won'], 'defending': ['Interceptions', 'Tackles', 'Tackles Won', 'Tackles Won (%)', 'Last Man Tackle', 'Blocked Shots', 'Clearances', 'Clearance Offline', 'Ball Recovery', 'Aerials', 'Aerials Won', 'Aerials Won (%)', 'Total Duels', 'Duels Won', 'Duels Won (%)'], 'errors_discipline': ['Goals Conceded', 'Penalties Committed', 'Penalties Missed', 'Shots Off Target', 'Big Chances Missed', 'Aerials Lost', 'Duels Lost', 'Fouls', 'Dispossessed', 'Dribbled Past', 'Turn Over', 'Possession Lost', 'Offsides', 'Own Goals', 'Error Lead To Goal', 'Error Lead To Shot', 'Yellow Cards', 'Yellow & Red Cards', 'Red Cards']}


def metric_key(name):
    value=str(name).replace('%',' percentage ').replace('&',' and ')
    value=re.sub(r'[_-]+',' ',value)
    return re.sub(r'\s+',' ',re.sub(r'[^a-zA-Z0-9\s]',' ',value)).strip().lower()


CANONICAL = {metric_key(name):name for name in LABELS}
CANONICAL.update({metric_key('yellow_red_cards'):'Yellow & Red Cards',metric_key('shots_blocked'):'Blocked Shots'})
DERIVED = {
    'Shot Quality (%)':('Expected Goals','Shots Total'),
    'On-Target Shot Quality (%)':('Expected Goals On Target','Shots On Target'),
    'Goal Conversion (%)':('Goals','Shots Total'),
    'On-Target to Goal Conversion (%)':('Goals','Shots On Target'),
    'Assist Efficiency (%)':('Assists','Key Passes'),
    'Dribble Accuracy (%)':('Successful Dribbles','Dribble Attempts'),
}


def finite(value):
    try:
        if isinstance(value,str): value=value.replace('%','').strip()
        number=float(value)
        return number if math.isfinite(number) else None
    except (ValueError,TypeError): return None


def enterprise_per90(stats):
    index={CANONICAL.get(metric_key(name),str(name).strip()):finite(value) for name,value in stats.items()}
    minutes=index.get('Minutes Played')
    values={}
    for label in LABELS:
        value=index.get(label)
        if label in DERIVED:
            numerator,denominator=(index.get(name) for name in DERIVED[label])
            if numerator is not None and denominator is not None and denominator>0:
                value=numerator/denominator*100
        if value is None: continue
        fixed=label in ('Rating','Match Count') or '(%)' in label
        if not fixed and label!='Minutes Played' and minutes and minutes>0:
            value*=90/minutes
        values[label]=value
    return values


def build_league_categories(candidate_stats,league_stats):
    player,league=enterprise_per90(candidate_stats),enterprise_per90(league_stats)
    groups=[]
    for key,names in CATEGORIES.items():
        rows=[{'metric':name,'values':[player[name],league[name]],'lowerIsBetter':key=='errors_discipline'}
              for name in names if name not in ('Rating','Match Count','Minutes Played','Captain')
              and name in player and name in league and not (player[name]==0 and league[name]==0)]
        if rows: groups.append({'key':key,'metrics':rows})
    return groups
