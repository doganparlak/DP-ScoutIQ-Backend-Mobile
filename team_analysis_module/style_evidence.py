"""Adapt selected mobile match snapshots to the Enterprise classifier."""
import math
import re
from collections import defaultdict

ROLE_METRICS = ['Passes', 'Key Passes', 'Shots Total', 'Tackles', 'Interceptions', 'Successful Dribbles', 'Total Crosses', 'Ball Recovery']
DETAIL_METRICS = ROLE_METRICS + ['Clearances', 'Total Duels', 'Duels Won', 'Aerials Won', 'Fouls', 'Passes In Final Third', 'Through Balls', 'Touches', 'Possession Lost', 'Dispossessed', 'Dribble Attempts', 'Tackles Won']

def numeric(value):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def role_name(player):
    name = player['position'].casefold()
    if player.get('positionId') == 24 or 'goalkeeper' in name: return 'goalkeeper'
    if player.get('positionId') == 25 or 'defender' in name: return 'defender'
    if player.get('positionId') == 26 or 'midfielder' in name: return 'midfielder'
    if player.get('positionId') == 27 or any(word in name for word in ('attacker', 'forward', 'striker')): return 'attacker'
    return 'unknown'


PLAYER_METRICS = DETAIL_METRICS
ROLES = {'defender':('Savunma hattı','Defenders'),'midfielder':('Orta saha','Midfielders'),'attacker':('Hücum hattı','Attackers')}
TEAM_METRICS = {'Counter Attacks','Ball Possession %','Passes','Accurate Passes','Long Balls','Long Balls Won','Key Passes','Total Crosses','Accurate Crosses','Dribble Attempts','Successful Dribbles','Shots Total','Shots On Target','Shots Insidebox','Shots Outsidebox','Goals','Big Chances Created','Big Chances Missed','Dangerous Attacks','Attacks','Interceptions','Tackles','Tackles Won','Ball Recovery','Blocked Shots','Fouls','Yellow Cards','Red Cards','Error Lead To Shot','Error Lead To Goal','Corners','Expected Goals','Expected Goals On Target','Expected Goals Open Play (xGOP)','Expected Goals Set Play (xGSP)'}
OPPONENT_METRICS={'Shots Total','Shots On Target','Shots Insidebox','Shots Outsidebox','Key Passes','Total Crosses','Accurate Crosses','Dribble Attempts','Successful Dribbles','Big Chances Created','Dangerous Attacks','Passes','Accurate Passes','Expected Goals'}
TEAM_METRICS |= {'Clearances','Total Duels','Duels Won','Aerials Won','Offsides','Offsides Provoked','Passes In Final Third','Through Balls','Through Balls Won','Backward Passes','Touches','Possession Lost','Dispossessed','Dribbled Past'}
OPPONENT_METRICS |= {'Counter Attacks','Offsides','Long Balls','Long Balls Won','Passes In Final Third','Through Balls','Total Duels','Duels Won','Possession Lost'}
METRIC_ALIASES={'Expected Goals (xG)':'Expected Goals','Expected Goals on Target (xGoT)':'Expected Goals On Target'}
FORBIDDEN=re.compile(r'goalkeep|save|kaleci|kurtar|punch|xgp|goal.prevent',re.I)


def add_counts(sample,key,value,meta):
    if value is not None and math.isfinite(value) and value>=0:sample[key]=(value,1);meta.setdefault(key,{'name':key,'percentage':False})


def extract_match(fixture,team_id,meta):
    from .report import DERIVED_PERCENTAGE_METRICS, TEAM_METRIC_CATEGORY

    opponent=next((int(p['id']) for p in fixture.get('participants') or [] if p.get('id') and int(p['id'])!=team_id),None)
    values={}
    for row in [*(fixture.get('statistics') or []),*(fixture.get('xgfixture') or fixture.get('xGFixture') or [])]:
        original=(row.get('type') or {}).get('name') or ''
        name=METRIC_ALIASES.get(original,TEAM_METRIC_CATEGORY.get(original,('',original))[1])
        value=numeric((row.get('data') or {}).get('value'))
        if value is not None and value>=0:values[(int(row.get('participant_id') or 0),name)]=value
    sample={}
    for name in TEAM_METRICS:add_counts(sample,name,values.get((team_id,name)),meta)
    for name in OPPONENT_METRICS:add_counts(sample,'Opponent '+name,values.get((opponent,name)),meta)
    pairs={**DERIVED_PERCENTAGE_METRICS,'Long Ball Share (%)':('Long Balls','Passes'),'Inside-box Shot Share (%)':('Shots Insidebox','Shots Total'),'Cross Share (%)':('Total Crosses','Passes'),'Final-third Pass Share (%)':('Passes In Final Third','Passes')}
    for name,(a,b) in pairs.items():
        for prefix in ('','Opponent '):
            aa,bb=sample.get(prefix+a),sample.get(prefix+b)
            if aa is not None and bb is not None and bb[0]>0 and 0<=aa[0]<=bb[0]:
                sample[prefix+name]=(aa[0],bb[0]);meta[prefix+name]={'name':prefix+name,'percentage':True}
    # Preserve a recorded provider percentage when its paired counts are absent.
    for name,(numerator,denominator) in DERIVED_PERCENTAGE_METRICS.items():
        for prefix in ('','Opponent '):
            key=prefix+name;value=values.get((opponent if prefix else team_id,name))
            if prefix+numerator in sample and prefix+denominator in sample:
                continue
            if key not in sample and value is not None and 0<=value<=100:
                sample[key]=(value,100);meta[key]={'name':key,'percentage':True}
    add_context_rates(sample, meta)
    if 'Ball Possession %' in sample:meta['Ball Possession %']['percentage']=True
    return sample


def add_context_rates(sample, meta):
    def rate(name, numerator, denominator, scale=1, percentage=False):
        a, b = sample.get(numerator), sample.get(denominator)
        if a is not None and b is not None and b[0] > 0 and (not percentage or 0 <= a[0] <= b[0]):
            sample[name] = (a[0] * scale, b[0])
            meta[name] = {'name':name, 'percentage':percentage}
    rate('Shots Per 100 Passes', 'Shots Total', 'Passes', 100)
    rate('Key Passes Per 100 Passes', 'Key Passes', 'Passes', 100)
    rate('Through Balls Per 100 Passes', 'Through Balls', 'Passes', 100)
    rate('Outfield Final-third Pass Share (%)', 'Outfield Passes In Final Third', 'Outfield Passes', percentage=True)
    rate('Outfield Duels Won (%)', 'Outfield Duels Won', 'Outfield Total Duels', percentage=True)
    rate('Recovery To Loss Ratio', 'Ball Recovery', 'Possession Lost')
    for label, source in [('Tackles','Tackles'),('Interceptions','Interceptions'),('Recoveries','Ball Recovery'),('Duels','Total Duels'),('Fouls','Fouls')]:
        rate(f'{label} Per 100 Opponent Passes', source, 'Opponent Passes', 100)
        rate(f'Outfield {label} Per 100 Opponent Passes', 'Outfield '+source, 'Opponent Passes', 100)
    rate('Outfield Recovery To Loss Ratio', 'Outfield Ball Recovery', 'Outfield Possession Lost')


class PlayerSamples:
    """Accumulate outfield contributions without retaining lineup payloads."""
    def __init__(self, team_id, meta):
        self.team_id = team_id
        self.meta = meta
        self.samples = {}

    def add(self, fixture):
        team_id, meta = self.team_id, self.meta
        rows=[p for p in fixture['players'] if p['teamId']==team_id and role_name(p)!='goalkeeper']
        totals=defaultdict(float);grouped=defaultdict(lambda:defaultdict(float))
        for player in rows:
            role=role_name(player)
            for metric,value in player['metrics'].items():
                if value<0:continue
                totals[metric]+=value;grouped[role][metric]+=value
        sample={}
        for role,names in grouped.items():
            if role not in ROLES:continue
            for metric,value in names.items():
                if totals[metric]>0:
                    key=f'role:{role}:{metric}';sample[key]=(value,totals[metric])
                    meta[key]={'name':f'{ROLES[role][1]} {metric} Share (%)','percentage':True,'role':role,'playerMetric':metric}
        # Record observed outfield totals separately from provider team statistics.
        for metric, total in totals.items():
            key='Outfield '+metric;sample[key]=(total,1)
            meta[key]={'name':key,'percentage':False}
        concentrations=[]
        for metric in ('Key Passes','Successful Dribbles'):
            values=[p['metrics'][metric] for p in rows if metric in p['metrics']]
            if values and sum(values)>0:concentrations.append(max(values)/sum(values))
        if concentrations:
            key='Creator Concentration (%)';sample[key]=(sum(concentrations),len(concentrations))
            meta[key]={'name':key,'percentage':True}
        balances=[]
        for metric in ('Interceptions','Ball Recovery'):
            if all(metric in grouped[role] for role in ROLES):
                values=[grouped[role][metric] for role in ROLES];total=sum(values)
                if total>0:balances.append(-sum(v/total*math.log(v/total) for v in values if v>0)/math.log(3))
        if balances:
            key='Defensive Role Balance (%)';sample[key]=(sum(balances),len(balances))
            meta[key]={'name':key,'percentage':True}
        self.samples[fixture['id']]=sample

def pool(samples,keys,meta):
    return [{'key':key,**meta[key],'value':round(sum(s[key][0] for s in samples)/sum(s[key][1] for s in samples)*(100 if meta[key]['percentage'] and key!='Ball Possession %' else 1),3)} for key in keys]
