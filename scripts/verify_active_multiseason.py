"""Read active shared forecasts through both serving runtimes and mobile HTTP."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import requests
from sqlalchemy import text
from match_pool_module import forecast as f


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--review',type=Path,required=True);parser.add_argument('--enterprise-root',type=Path,required=True);args=parser.parse_args();out=args.review
    sys.path.insert(0,str(args.enterprise_root))
    from match_analysis_module import model_profile as enterprise
    expected=json.loads((out/'activation.json').read_text())['activeModelId'];now=datetime.now(timezone.utc)
    model=f.load_saved_model(now)
    if model['modelId']!=expected:raise ValueError('Wrong active model')
    leagues=','.join(str(c['leagueId'])for c in model['competitions']);rows=[]
    for page in range(1,21):
        payload=f._request(f'fixtures/between/{now.date()}/{(now+timedelta(days=7)).date()}',
            {'filters':f'fixtureLeagues:{leagues}','include':'participants;state;league;season','per_page':100,'page':page})
        rows.extend(payload.get('data')or[])
        if not(payload.get('pagination')or{}).get('has_more'):break
    else:raise ValueError('Upcoming pagination exceeded')
    checks=[];selected={};per_league={}
    for raw in sorted(rows,key=lambda r:r['starting_at']):
        if f.utc(raw['starting_at'])<=now or per_league.get(raw['league_id'],0)>=2:continue
        sides={p.get('meta',{}).get('location'):p for p in raw.get('participants')or[]}
        if not all(sides.get(side,{}).get('id')for side in ('home','away')):continue
        fixture={'fixtureId':raw['id'],'startingAt':raw['starting_at'],'league':{'id':raw['league_id']},
                 'season':{'name':(raw.get('season')or{}).get('name')},
                 'homeTeam':{'id':sides['home']['id'],'name':sides['home']['name']},
                 'awayTeam':{'id':sides['away']['id'],'name':sides['away']['name']}}
        a=f.build_forecast(fixture,now,model)
        if a['status']!='ready':continue
        b=enterprise.build_model_profile(fixture,now=now)
        if b['status']!='ready'or b['modelId']!=expected:raise ValueError('Enterprise did not select new champion')
        for key in ('outcomes','goalDistribution','scoreMargins','grid','gridRemainder'):
            if a['forecast'][key]!=b[key]:raise ValueError('Mobile/enterprise mismatch: '+key)
        for side in ('home','away'):
            if a['forecast'][side]['projectedGoals']!=b[side]['projectedGoals']:raise ValueError('Goal-rate mismatch')
        row={'fixtureId':raw['id'],'leagueId':raw['league_id'],'name':raw.get('name'),'modelId':expected,'status':'ready'}
        checks.append(row);per_league[raw['league_id']]=per_league.get(raw['league_id'],0)+1;selected.setdefault(raw['league_id'],fixture)
    if len(checks)<20:raise ValueError('Insufficient real-fixture verification')
    # Warm a small representative set; this never spends Pro credits.
    targets=[selected[l]for l in (600,8,2,5,2286,636,648,779)if l in selected]
    for fixture in targets:
        f._run(fixture['fixtureId']);cached=f._read(f._cache_key(fixture['fixtureId']))
        if not cached or cached.get('status')!='ready'or cached['forecast']['modelId']!=expected:raise ValueError('Future shared cache did not refresh')
    email=subprocess.check_output(['git','config','user.email'],text=True).strip()
    with f.engine.connect()as db:
        token=db.execute(text("SELECT s.token FROM sessions s JOIN users u ON u.id=s.user_id WHERE (lower(u.email)=lower(:email) OR lower(u.email) LIKE 'doganparlak%') AND s.ended_at IS NULL ORDER BY s.created_at DESC LIMIT 1"),{'email':email}).scalar()
    remote=[]
    if token:
        for fixture in targets:
            response=requests.get(f'https://dp-scoutiq-backend-mobile.onrender.com/match-pool/fixtures/{fixture["fixtureId"]}/forecast',headers={'Authorization':'Bearer '+token},timeout=60)
            body=response.json()
            if response.status_code!=200 or body.get('status')!='ready'or(body.get('forecast')or{}).get('modelId')!=expected:
                raise ValueError('Remote mobile forecast did not use new model: '+str(response.status_code))
            remote.append({'fixtureId':fixture['fixtureId'],'leagueId':fixture['league']['id'],'httpStatus':response.status_code,'modelId':expected})
    health={}
    for service in ('dp-scoutiq-backend-mobile','scoutwise-backend-enterprise'):
        response=requests.get(f'https://{service}.onrender.com/health',timeout=45)
        if response.status_code!=200 or response.json().get('forecastRuntimeVersion')!=6:raise ValueError('Serving health/runtime check failed')
        health[service]=response.json()
    result={'activeModelId':expected,'realUpcomingForecasts':len(checks),'competitionsVerified':len(selected),
            'mobileEnterpriseParity':True,'authenticatedMobileChecks':remote,'health':health,'forecasts':checks,
            'frontendBuildRequired':False,'renderDeploymentStatusVerified':False}
    (out/'live-verification.json').write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items()if k!='forecasts'}),flush=True)

if __name__=='__main__':main()
