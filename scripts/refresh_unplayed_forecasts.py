"""Refresh only existing unplayed forecasts from older models. No credit charges."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
import json,os,sys,math
from collections import Counter
from sqlalchemy import text
from match_pool_module import forecast as f
OUT=Path('/Users/doganparlak/Desktop/ScoutWise/Forecast Reviews/refresh-unplayed-2026-11-04');OUT.mkdir(exist_ok=True)
EXPECTED=12

def dump(path,value,private=False):
 path.write_text(json.dumps(value,default=str,allow_nan=False,indent=2))
 if private:os.chmod(path,0o600)
def preserved():
 with f.engine.connect()as db:
  historical={}
  for table,field in [('mobile_match_forecast_snapshots','payload'),('mobile_pro_forecast_unlocks','payload'),('enterprise_favorite_matches','report_content')]:
   condition="(payload->'forecast'->>'startingAt')::timestamptz<=:now"if field=='payload'else'starting_at<=:now'
   historical[table]=list(db.execute(text(f"SELECT count(*),md5(COALESCE(string_agg(md5({field}::text),',' ORDER BY md5({field}::text)),'')) FROM {table} WHERE {condition}"),{'now':START}).one())
  credits=list(db.execute(text("SELECT count(*),sum(credits_spent),md5(string_agg(user_id::text||':'||fixture_id::text||':'||credits_spent::text||':'||created_at::text,',' ORDER BY user_id,fixture_id)) FROM mobile_pro_forecast_unlocks")).one())
  return {'historical':historical,'unlockCreditsAndOwnership':credits}
START=datetime.now(timezone.utc)
def equivalent(a,b):
 if isinstance(a,(int,float))and isinstance(b,(int,float)):return math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-12)
 if isinstance(a,dict)and isinstance(b,dict):return a.keys()==b.keys()and all(equivalent(a[k],b[k])for k in a)
 if isinstance(a,list)and isinstance(b,list):return len(a)==len(b)and all(equivalent(x,y)for x,y in zip(a,b))
 return a==b

def main():
 if f.active_model_id()!=EXPECTED:raise ValueError('Expected approved model 12')
 model=f.load_saved_model(START)
 assert model['modelId']==EXPECTED and model['scope']=='three_seasons_recency_weighted'and model['trainingPolicy']=='offline_refit_only'
 with f.engine.connect()as db:
  snapshots=[dict(r)for r in db.execute(text("SELECT fixture_id,payload FROM mobile_match_forecast_snapshots WHERE (payload->'forecast'->>'startingAt')::timestamptz>:now"),{'now':START}).mappings()]
  unlocks=[dict(r)for r in db.execute(text("SELECT user_id,fixture_id,payload FROM mobile_pro_forecast_unlocks WHERE (payload->'forecast'->>'startingAt')::timestamptz>:now AND payload->'forecast'->>'modelId'<>:model"),{'now':START,'model':str(EXPECTED)}).mappings()]
  reports=[dict(r)for r in db.execute(text("SELECT id,fixture_id,fixture_payload,report_content FROM enterprise_favorite_matches WHERE starting_at>:now AND report_status='ready' AND report_type='pre_match' AND report_content->'model_match_profile'->>'modelId'<>:model"),{'now':START,'model':str(EXPECTED)}).mappings()]
  caches=[dict(r)for r in db.execute(text("SELECT cache_key,payload FROM mobile_match_forecast_cache WHERE cache_key LIKE 'fixture:%' AND (payload->'forecast'->>'startingAt')::timestamptz>:now AND payload->'forecast'->>'modelId'<>:model"),{'now':START,'model':str(EXPECTED)}).mappings()]
 oldsnap=[r for r in snapshots if r['payload']['forecast'].get('modelId')!=EXPECTED]
 target=sorted({r['fixture_id']for r in oldsnap+unlocks+reports}|{r['payload']['forecast']['fixtureId']for r in caches})
 dump(OUT/'before.json',{'at':START,'snapshots':oldsnap,'unlocks':unlocks,'enterpriseReports':reports,'caches':caches},True)
 print('TARGETS',len(target),'snapshots',len(oldsnap),'unlocks',len(unlocks),'reports',len(reports),flush=True)
 before=preserved();fixtures={};results={};current={r['fixture_id']:r['payload']for r in snapshots if r['payload']['forecast'].get('modelId')==EXPECTED}
 def generate(identity):
  fixture=f.get_fixture(identity)
  if not fixture or f.utc(fixture['startingAt'])<=datetime.now(timezone.utc)or str((fixture.get('state')or{}).get('code','')).upper()not in('NS','TBA'):raise ValueError('Fixture is not unplayed: '+str(identity))
  result=current.get(identity)or f.build_forecast(fixture,START,model)
  if result['status']!='ready'or result['forecast']['modelId']!=EXPECTED:raise ValueError('New model unavailable: '+str(identity))
  return identity,fixture,result
 with ThreadPoolExecutor(max_workers=4)as pool:
  for future in as_completed([pool.submit(generate,i)for i in target]):
   identity,fixture,result=future.result();fixtures[identity]=fixture;results[identity]=result;print('PREPARED',len(results),'/',len(target),identity,flush=True)
 sys.path.insert(0,'/Users/doganparlak/Desktop/ScoutWise/ScoutWise-Backend-Enterprise')
 from match_analysis_module.model_profile import build_model_profile
 profiles={}
 for r in reports:
  fid=r['fixture_id'];p=build_model_profile(fixtures[fid],now=START)
  if p.get('modelId')!=EXPECTED or p.get('status')!='ready':raise ValueError('Enterprise profile unavailable')
  for key in('outcomes','goalDistribution','scoreMargins','grid'):
   if not equivalent(p[key],results[fid]['forecast'][key]):raise ValueError('Enterprise/mobile mismatch')
   p[key]=results[fid]['forecast'][key]
  profiles[fid]=p
 dump(OUT/'prepared.json',{'forecasts':results,'enterpriseProfiles':profiles},True)
 changes=Counter();numerical=set();details=[]
 # All provider calls and validation completed before this short transaction.
 with f.engine.begin()as db:
  db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('activate_shared_match_forecast',0))"))
  if db.execute(text('SELECT id FROM mobile_match_forecast_models WHERE is_active')).scalar()!=EXPECTED:raise ValueError('Active model changed')
  for r in oldsnap:
   fid=r['fixture_id'];new=results[fid]
   changed=db.execute(text("UPDATE mobile_match_forecast_snapshots SET payload=CAST(:new AS jsonb),updated_at=NOW() WHERE fixture_id=:fid AND payload=CAST(:old AS jsonb) AND (payload->'forecast'->>'startingAt')::timestamptz>NOW()"),{'new':json.dumps(new),'old':json.dumps(r['payload']),'fid':fid}).rowcount
   if changed!=1:raise ValueError('Snapshot changed concurrently')
   changes['forecastSnapshots']+=changed
   old=r['payload']['forecast'];newf=new['forecast'];different=any(old.get(k)!=newf.get(k)for k in('outcomes','topScores','goalDistribution','halfScenarios'))
   if different:numerical.add(fid)
   details.append({'fixtureId':fid,'oldModelId':old.get('modelId'),'newModelId':EXPECTED,'predictionValuesChanged':different})
  for r in unlocks:
   fid=r['fixture_id'];old=r['payload']['forecast'];new=results[fid]
   changed=db.execute(text("UPDATE mobile_pro_forecast_unlocks SET payload=CAST(:new AS jsonb) WHERE user_id=:uid AND fixture_id=:fid AND payload=CAST(:old AS jsonb) AND (payload->'forecast'->>'startingAt')::timestamptz>NOW()"),{'new':json.dumps(new),'old':json.dumps(r['payload']),'uid':r['user_id'],'fid':fid}).rowcount
   if changed!=1:raise ValueError('Unlock changed concurrently')
   changes['unlockedForecasts']+=changed
   if old.get('outcomes')!=new['forecast']['outcomes']:numerical.add(fid)
  for r in reports:
   fid=r['fixture_id'];content={**r['report_content'],'model_match_profile':profiles[fid]}
   changed=db.execute(text("UPDATE enterprise_favorite_matches SET report_content=CAST(:new AS jsonb),updated_at=NOW() WHERE id=:id AND report_content=CAST(:old AS jsonb) AND starting_at>NOW()"),{'new':json.dumps(content),'old':json.dumps(r['report_content']),'id':r['id']}).rowcount
   if changed!=1:raise ValueError('Enterprise report changed concurrently')
   changes['enterprisePreMatchReports']+=changed
  for fid,new in results.items():
   expiry=min(f.utc(new['forecast']['startingAt']),datetime.now(timezone.utc)+timedelta(hours=1))
   db.execute(text("INSERT INTO mobile_match_forecast_cache(cache_key,payload,expires_at) VALUES(:key,CAST(:new AS jsonb),:expiry) ON CONFLICT(cache_key) DO UPDATE SET payload=EXCLUDED.payload,expires_at=EXCLUDED.expires_at,updated_at=NOW()"),{'key':f._cache_key(fid),'new':json.dumps(new),'expiry':expiry})
   changes['servingCacheEntries']+=1
 after=preserved()
 if before!=after:raise ValueError('Historical records or credit metadata changed')
 with f.engine.connect()as db:
  remaining={}
  for table in('mobile_match_forecast_snapshots','mobile_pro_forecast_unlocks'):
   remaining[table]=db.execute(text(f"SELECT count(*) FROM {table} WHERE (payload->'forecast'->>'startingAt')::timestamptz>NOW() AND payload->'forecast'->>'modelId'<>:model"),{'model':str(EXPECTED)}).scalar_one()
  remaining['enterprise_favorite_matches']=db.execute(text("SELECT count(*) FROM enterprise_favorite_matches WHERE starting_at>NOW() AND report_status='ready' AND report_type='pre_match' AND report_content->'model_match_profile'->>'modelId'<>:model"),{'model':str(EXPECTED)}).scalar_one()
  if any(remaining.values()):raise ValueError('Older upcoming model forecasts remain')
 summary={'activeModelId':EXPECTED,'scope':model['scope'],'startedAt':START,'completedAt':datetime.now(timezone.utc),'uniqueUnplayedMatchesUpdated':len(target),'uniqueMatchesWithPredictionValuesChanged':len(numerical),'existingStoredRecordsUpdated':sum(changes[k]for k in('forecastSnapshots','unlockedForecasts','enterprisePreMatchReports')),'counts':dict(changes),'remainingOlderModelUnplayedRecords':remaining,'historicalAndCreditsPreserved':before==after,'details':details}
 dump(OUT/'result.json',summary);print('RESULT',json.dumps({k:v for k,v in summary.items()if k!='details'},default=str),flush=True)
if __name__=='__main__':main()
