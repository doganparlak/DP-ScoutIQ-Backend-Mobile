"""Activate the user-approved, runtime-compatible main model and verify rollback.

Preserves historical forecasts and paid unlocks. Does not alter the timing
recipe, frontend schemas, user balances, or subscription records.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from sqlalchemy import text
from match_pool_module import forecast as f, forecast_history as history


def fingerprints(now):
    queries={
        'historicalCache':"SELECT count(*),md5(COALESCE(string_agg(cache_key||':'||md5(payload::text),','ORDER BY cache_key),''))FROM mobile_match_forecast_cache WHERE payload->>'status'='ready' AND (payload->'forecast'->>'startingAt')::timestamptz<=:now",
        'historicalSnapshots':"SELECT count(*),md5(COALESCE(string_agg(fixture_id::text||':'||md5(payload::text),','ORDER BY fixture_id),''))FROM mobile_match_forecast_snapshots WHERE (payload->'forecast'->>'startingAt')::timestamptz<=:now",
        'historicalUnlocks':"SELECT count(*),md5(COALESCE(string_agg(user_id::text||':'||fixture_id::text||':'||md5(payload::text)||':'||credits_spent::text,','ORDER BY user_id,fixture_id),''))FROM mobile_pro_forecast_unlocks WHERE (payload->'forecast'->>'startingAt')::timestamptz<=:now",
        'unlockCredits':"SELECT count(*),COALESCE(sum(credits_spent),0)::text FROM mobile_pro_forecast_unlocks"}
    with f.engine.connect()as db:return {key:list(db.execute(text(query),{'now':now}).one())for key,query in queries.items()}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--review',type=Path,required=True);parser.add_argument('--expected-model',type=int,required=True);args=parser.parse_args();out=args.review
    f._schema();history._schema()
    with f.engine.connect()as db:
        active=db.execute(text('SELECT id,artifact FROM mobile_match_forecast_models WHERE is_active')).mappings().one()
    if active['id']!=args.expected_model:raise ValueError('Active model changed; review before activation')
    model=json.loads((out/'approved-main-model.json').read_text())
    if model.get('previousModelId')!=args.expected_model:raise ValueError('Rollback model does not match prepared artifact')
    proof=json.loads((out/'approved-model-verification.json').read_text());parity=json.loads((out/'runtime-parity.json').read_text())
    if not (proof['timingParametersPreserved']and proof['schemaVersion']==6 and parity['mobileEnterpriseParity']):raise ValueError('Missing compatibility evidence')
    now=datetime.now(timezone.utc);original_parameters={k:v for k,v in model['halfTimeModel'].items()if k!='leagueTotals'}
    since=f.utc(model['asOf'])-timedelta(hours=3);cutoff=now-timedelta(hours=3);updates=[]
    leagues=','.join(str(c['leagueId'])for c in model['competitions'])
    for page in range(1,31):
        payload=f._request(f'fixtures/between/{since.date()}/{cutoff.date()}',{'filters':f'fixtureLeagues:{leagues};fixtureStates:5',
            'include':'participants;state;scores;statistics.type;xGFixture.type;pressure','per_page':100,'page':page})
        for row in payload.get('data')or[]:
            if not since<=f.utc(row['starting_at'])<=cutoff:continue
            compact=f._compact(row)
            if compact:updates.append(compact)
        if not(payload.get('pagination')or{}).get('has_more'):break
    else:raise ValueError('Incremental refresh exceeded pagination limit')
    model=history.apply_matches(model,updates,now)
    if original_parameters!={k:v for k,v in model['halfTimeModel'].items()if k!='leagueTotals'}:raise ValueError('Timing parameters changed')
    activated=datetime.now(timezone.utc)
    model.update(asOf=activated.isoformat(),activatedAt=activated.isoformat(),historyAsOf=activated.isoformat(),historyInputCutoff=cutoff.isoformat())
    (out/'activated-main-model.json').write_text(json.dumps(model,allow_nan=False))
    before=fingerprints(activated)
    # store_shared_model holds the shared activation advisory lock, switches the
    # active row atomically, and invalidates only future shared cache responses.
    new_id=f.store_shared_model(model,activate=True)
    after=fingerprints(activated)
    if before!=after:raise ValueError('Historical forecasts or paid-credit records changed')
    loaded=f.load_saved_model(datetime.now(timezone.utc))
    if loaded['modelId']!=new_id:raise ValueError('New champion is not selected')
    previous=f.load_saved_model(activated-timedelta(seconds=1))
    if previous['modelId']!=args.expected_model:raise ValueError('Historical model selection changed')
    with f.engine.connect()as db:
        count=db.execute(text('SELECT count(*)FROM mobile_match_forecast_models WHERE is_active')).scalar_one()
    if count!=1:raise ValueError('Expected one active model')
    result={'activeModelId':new_id,'previousModelId':args.expected_model,'version':6,'activatedAt':activated.isoformat(),
        'trainingFixtures':model['inputMatches'],'trainingExamples':model['trainingMatches'],'incrementalMatchesAdded':model['historyMatchesAdded'],
        'userApproved':True,'automaticQualityGatePassed':False,'timingParametersPreserved':True,
        'historicalModelSelectionPreserved':True,'preservedRecords':{key:value[0]for key,value in before.items()},
        'rollbackModelRetained':True,'trainingPolicy':model['trainingPolicy'],
        'artifactSHA256':hashlib.sha256((out/'activated-main-model.json').read_bytes()).hexdigest()}
    (out/'activation.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)

if __name__=='__main__':main()
