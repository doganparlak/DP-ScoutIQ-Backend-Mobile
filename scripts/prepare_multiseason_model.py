"""Refit an explicitly approved main model into the existing v6 serving schema.

Does not activate or write to the database. Keeps the incumbent timing recipe.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import numpy as np
from sqlalchemy import text
from match_pool_module import forecast as f, forecast_candidates as c, forecast_learning as l
from match_pool_module import forecast_multiseason as multi
from scripts.evaluate_multiseason_forecasts import cached_features


def main():
    p=argparse.ArgumentParser();p.add_argument('--review',type=Path,required=True);args=p.parse_args();out=args.review
    source=json.loads((out/'dataset.json').read_text());result=json.loads((out/'results.json').read_text())
    final=json.loads((out/'fold-2026-10-25.json').read_text());artifacts=final['artifacts'];settings=artifacts['selected']['multi']
    if settings['blend']!=0.:raise ValueError('This compatibility export supports the selected linear-only champion; nonlinear winners need a separate verified export')
    with f.engine.connect()as db:
        row=db.execute(text('SELECT id,artifact FROM mobile_match_forecast_models WHERE is_active')).mappings().one()
    old={**row['artifact'],'modelId':row['id']};(out/'previous-active-model.json').write_text(json.dumps(old,allow_nan=False))
    prior=json.loads((out.parent/'league-offset-2026-11-02'/'association-prior-2025.json').read_text())
    data=cached_features(out,'multi-opponent',source['matches'],'opponent',source['competitions'],prior)
    cutoff=f.utc(source['asOf']);indices=list(range(len(data['ids'])));w=multi.weights(data,indices,cutoff,settings['decayDays'])
    columns=artifacts['linearColumns'];x,prep=c._matrix(data['raw'],columns,w)
    beta=f._fit(x,data['goals'],settings['penalty'],w).tolist()
    names=[data['names'][i]for i in columns];schema=l.schema('opponent',l.STAT_NAMES)
    serving_columns=[schema.index(name)for name in names]
    offsets,state=multi.build_serving_offsets(source['matches'],source['competitions'],prior,cutoff)
    # The serving state must match the actual feature-generation state.
    stored={int(k):v for k,v in data['stateDump']['ratings'].items()}
    for team,rating in state.ratings.items():np.testing.assert_allclose(rating,stored[team],atol=1e-10,rtol=1e-10)
    selected={name.split(':',1)[1]for name in names if ':'in name}
    histories={str(team):[{**r,**{side:{k:v for k,v in r[side].items()if k in selected}for side in ('own','opponent')}}for r in rows[-f.WINDOW:]]for team,rows in state.histories.items()}
    current_ids={s['seasonId']for s in old['competitions']}
    competitions=[s for s in source['competitions']if s['seasonId']in current_ids]
    if len(competitions)!=44:raise ValueError('Expected the 44 current competitions')
    # Keep the exact incumbent timing parameters and history window. Only its
    # normal current-season input totals advance to the new data cutoff.
    half=deepcopy(old['halfTimeModel']);half['leagueTotals']={str(k):v for k,v in state.half_totals.items()}
    now=datetime.now(timezone.utc).isoformat()
    model={'version':6,'featureSchema':3,'modelId':None,'asOf':cutoff.isoformat(),'dataCutoff':cutoff.isoformat(),
        'historyInputCutoff':cutoff.isoformat(),'historyAsOf':cutoff.isoformat(),
        'scope':'three_seasons_recency_weighted','trainingRecipe':'multiseason_linear_v1','trainingPolicy':'offline_refit_only',
        'strategy':'opponent','featureNames':schema,'columns':serving_columns,'features':names,
        **prep,'beta':beta,'goalRateCalibration':{'home':{'factor':1.},'away':{'factor':1.}},
        'rho':old.get('rho',0.),'regularization':settings['penalty'],'leagueBalancePower':.5,
        'usesTeamStats':True,'dailyHistoryRefresh':True,'inputMatches':len(source['matches']),
        'trainingMatches':len(indices),'validationMatches':result['testFixtures'],
        'trainingByCompetition':{str(k):v for k,v in Counter(data['leagues']).items()},
        'competitions':competitions,'historicalTrainingCompetitions':source['competitions'],
        'histories':histories,'leagueTotals':{str(k):v for k,v in state.totals.items()},
        'ratings':{str(k):v for k,v in state.ratings.items()},'halfTimeModel':half,'leagueOffsets':offsets,
        'metricAudit':{'teamHistoryWindow':10,'labelHalfLifeDays':settings['decayDays'],'recentHistoryMaxAgeDays':120,
            'leagueSeasonReset':True,'offseasonEloShrink':.5,'selectedSettingsFrozenBeforeFinalRefit':True},
        'validationMetrics':result['metrics']['main_upgrade']['main'],
        'validationMultiseasonEvaluation':{'testFixtures':result['testFixtures'],'cutoffs':['2026-10-11','2026-10-18','2026-10-25'],
            'baseline':result['metrics']['existing']['main'],'candidate':result['metrics']['main_upgrade']['main'],
            'pairedComparisons':result['comparisons']['main_upgrade']['main'],'automaticQualityGatePassed':result['gates']['main'],
            'reportSHA256':hashlib.sha256((out/'results.json').read_bytes()).hexdigest()},
        'promotionEvaluation':{'passed':True,'approvalType':'explicit_user_decision','automaticQualityGatePassed':False,
            'approvedAt':now,'reason':'User explicitly requested replacement after reviewing +13 correct match results on 1,427 fixtures; statistical confidence still spans no improvement.',
            'previousModelId':old['modelId'],'timingModelChanged':False},'previousModelId':old['modelId']}
    model.pop('modelId')
    # Numeric parity against the trained representation for all current teams
    # with enough history, plus every covered league (including UEFA offsets).
    checks=[]
    original={'columns':columns,**prep,'beta':beta}
    for competition in competitions:
        league=competition['leagueId'];eligible=[team for team,rows in state.histories.items()if len(rows)>=5]
        # Real same-competition participants where possible.
        teams=sorted({m[side]for m in source['matches']if m['season']==competition['seasonId']for side in ('home','away')} & set(eligible))
        for home,away in zip(teams[:3],teams[-3:]):
            if home==away:continue
            fixture={'fixtureId':0,'startingAt':now,'league':{'id':league},'homeTeam':{'id':home,'name':'home'},'awayTeam':{'id':away,'name':'away'}}
            gap=__import__('match_pool_module.forecast_league_offsets',fromlist=['fixture_elo_gap']).fixture_elo_gap(model,fixture)
            raw=np.array([c.features(state,home,away,True,league,now,'opponent'),c.features(state,away,home,False,league,now,'opponent')],float)
            raw=__import__('match_pool_module.forecast_league_offsets',fromlist=['apply_elo_gap']).apply_elo_gap(raw,data['names'],[gap])
            expected=c.linear_predict(original,raw)
            forecast=f.build_forecast(fixture,f.utc(now),model)
            if forecast['status']!='ready':raise ValueError(('Forecast not ready',forecast))
            actual=np.array([forecast['forecast'][side]['projectedGoals']for side in ('home','away')])
            np.testing.assert_allclose(actual,expected,rtol=1e-11,atol=1e-11)
            checks.append({'leagueId':league,'home':home,'away':away,'maximumRateDifference':float(abs(actual-expected).max())})
    if len({r['leagueId']for r in checks})!=44:raise ValueError('Incomplete competition parity coverage')
    (out/'approved-main-model.json').write_text(json.dumps(model,allow_nan=False))
    verification={'trainingFixtures':len(source['matches']),'trainingExamples':len(indices),'competitions':44,'historicalCompetitionSeasons':len(source['competitions']),
        'selectedSettings':settings,'servingParity':checks,'maximumRateDifference':max(r['maximumRateDifference']for r in checks),
        'timingParametersPreserved':all(half[k]==old['halfTimeModel'][k]for k in half if k!='leagueTotals'),
        'schemaVersion':6,'previousModelId':old['modelId'],'automaticGatePassed':False,'userApproved':True,
        'modelSHA256':hashlib.sha256((out/'approved-main-model.json').read_bytes()).hexdigest()}
    (out/'approved-model-verification.json').write_text(json.dumps(verification,indent=2))
    print(json.dumps({k:v for k,v in verification.items()if k!='servingParity'}),flush=True)

if __name__=='__main__':main()
