"""Offline half experiments: causal historical inputs, fixed full-time grid, no activation."""
import json, math, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import numpy as np
from scipy.special import softmax
from scipy.stats import binom, poisson
from scipy.optimize import minimize
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from match_pool_module import forecast as f
from scripts.evaluate_multiseason_forecasts import timing_metrics, paired
ROOT=Path('/Users/doganparlak/Desktop/ScoutWise/Forecast Reviews'); SRC=ROOT/'multiseason-2026-11-04'; OUT=ROOT/'half-allocation-2026-11-04'; OUT.mkdir(exist_ok=True)
def save(p,x):p.write_text(json.dumps(x,allow_nan=False,indent=2))
def collect():
 coverage=json.loads((ROOT/'historical-coverage-2026-11-04/coverage.json').read_text())
 def season(s):
  p=OUT/'periods'/f'{s["id"]}.json';p.parent.mkdir(exist_ok=True)
  if p.exists():return json.loads(p.read_text())
  rows={}
  for page in range(1,101):
   cache=p.parent/f'{s["id"]}-{page}.json'
   if cache.exists():payload=json.loads(cache.read_text())
   else:
    for attempt in range(5):
     try:payload=f._request('fixtures',{'filters':f'fixtureSeasons:{s["id"]};fixtureStates:5','include':'periods.statistics;events','per_page':100,'page':page});break
     except RuntimeError:
      if attempt==4:raise
      time.sleep(2**attempt)
    compact=[]
    for fixture in payload.get('data',[]):
     periods=fixture.get('periods')or[]; kinds={x['id']:x['type_id']for x in periods}; stats={}
     for period in periods:
      if period['type_id']not in (1,2):continue
      for v in period.get('statistics')or[]:
       val=(v.get('data')or{}).get('value')
       if isinstance(val,(int,float)):stats[f'{v["participant_id"]}:{period["type_id"]}:{v["type_id"]}']=val
     events=[{'team':e.get('participant_id'),'half':kinds.get(e.get('period_id')),'minute':e.get('minute'),'type':e.get('type_id'),'result':e.get('result'),'rescinded':e.get('rescinded')}for e in fixture.get('events')or[]if e.get('type_id')in (14,15,16)]
     compact.append({'id':fixture['id'],'stats':stats,'events':events,'periods':len(periods)})
    payload={'data':compact,'pagination':payload.get('pagination')};cache.write_text(json.dumps(payload))
   rows.update({str(r['id']):r for r in payload['data']})
   if not(payload.get('pagination')or{}).get('has_more'):break
  p.write_text(json.dumps(rows));return rows
 tasks=[s for l in coverage['leagues']for s in l['seasons']];allrows={}
 with ThreadPoolExecutor(max_workers=4)as pool:
  futures=[pool.submit(season,s)for s in tasks]
  for i,future in enumerate(as_completed(futures)):
   allrows.update(future.result());print('PERIODS',i+1,len(tasks),len(allrows),flush=True)
 save(OUT/'period-data.json',allrows)

# Protocol frozen before new outer-test results are computed.
PROTOCOL={'folds':['2026-10-11','2026-10-18','2026-10-25'],'innerDays':21,'classifier':['logistic C=.01,.1,1','hist depth=2,3; iterations=100; leaf=100; l2=10'], 'allocationRidge':[10,100,1000], 'selection':['inner log loss','inner accuracy; loss breaks ties'],'data':'Only previous settled matches at daily 04:00 UTC; 120 day history; windows 5 and 10','allocation':'Binomial half-score allocation exponentially tilted by higher-half class and first-half BTTS; normalized per full-time score cell','liveChanges':False,'limitation':'Retrospective dates already examined in prior research; no prospective or untouched-test claim'}
def make_features():
 source=json.loads((SRC/'dataset.json').read_text());periods=json.loads((OUT/'period-data.json').read_text());matches=source['matches']; features={}; history=defaultdict(list);quality=defaultdict(int)
 # Fixed statistic IDs: shots total/on target, attacks/dangerous attacks, possession, corners.
 types=(42,86,43,44,45,34)
 processed=[]
 for m in matches:
  p=periods.get(str(m['id']),{}); stat=p.get('stats',{}); bins=np.zeros((2,6));valid=p.get('periods',0)>=2
  # Use score progression to credit own goals correctly. Validate every event against final and half scores.
  previous=(0,0)
  for event in p.get('events',[]):
   if event.get('rescinded'):continue
   result=event.get('result')or''
   try:score=tuple(int(v.strip())for v in result.split('-'))
   except ValueError:valid=False;continue
   if len(score)!=2:valid=False;continue
   delta=np.array(score)-previous;previous=score
   if event['half']not in(1,2)or delta.sum()!=1 or delta.min()<0:valid=False;continue
   minute=event.get('minute')
   if minute is None:valid=False;continue
   bucket=min(2,max(0,(int(minute)-(1 if event['half']==1 else 46))//15))+(0 if event['half']==1 else 3)
   bins[int(delta.argmax()),bucket]+=1
  expected=np.array([m['hg'],m['ag']]);first=np.array([m.get('hhg'),m.get('hag')])
  if None in first or not np.array_equal(bins.sum(axis=1),expected)or not np.array_equal(bins[:,:3].sum(axis=1),first):valid=False
  quality['eventValidated'if valid else'eventMissingOrMismatch']+=1
  vals=[]
  for side,team in enumerate((m['home'],m['away'])):
   v=list(bins[side]) + list(bins[1-side]) if valid else [np.nan]*12
   v.extend([float(first.sum()>expected.sum()-first.sum()),float(first.sum()==expected.sum()-first.sum())]if None not in first else [np.nan]*2)
   for typ in types:
    for half in(1,2):v.append(stat.get(f'{team}:{half}:{typ}',np.nan))
   vals.append(np.array(v,dtype=float))
  quality['pairedHalfStatistics']+=int(all(any(f'{team}:1:{t}'in stat and f'{team}:2:{t}'in stat for t in types)for team in(m['home'],m['away'])))
  processed.append((m,vals))
 # Build rolling features before adding any match from that update day.
 cursor=0
 for m,_ in processed:
  at=f.utc(m['at']);update=at.replace(hour=4,minute=0,second=0,microsecond=0)
  if at<update:update-=__import__('datetime').timedelta(days=1)
  while cursor<len(processed)and f.utc(processed[cursor][0]['at'])+__import__('datetime').timedelta(hours=3)<=update:
   past,values=processed[cursor]
   for side,team in enumerate((past['home'],past['away'])):history[team].append((f.utc(past['at']),values[side]))
   cursor+=1
  x=[]
  for team in(m['home'],m['away']):
   rows=[v for t,v in history[team]if (at-t).days<=120]
   for window in(5,10):
    a=np.array(rows[-window:])
    if len(a):
     count=np.isfinite(a).sum(axis=0);mean=np.nansum(a,axis=0)/np.maximum(count,1)
    else:count=np.zeros(26);mean=np.zeros(26)
    x.extend(mean);x.extend(count/window)
  features[str(m['id'])]=x
 save(OUT/'quality.json',dict(quality));save(OUT/'event-features.json',features)
 print('QUALITY',dict(quality),flush=True)

def groupmass(h,a,shares,moments=False):
 n=len(shares);mass=np.zeros((n,6));mh=np.zeros_like(mass);ma=np.zeros_like(mass)
 hp=binom.pmf(np.arange(h+1)[None,:],h,shares[:,0,None]);ap=binom.pmf(np.arange(a+1)[None,:],a,shares[:,1,None])
 for k in range(h+1):
  for l in range(a+1):
   category=0 if 2*(k+l)>h+a else 1 if 2*(k+l)==h+a else 2
   group=2*category+int(k>0 and l>0)
   p=hp[:,k]*ap[:,l];mass[:,group]+=p;mh[:,group]+=p*k;ma[:,group]+=p*l
 return (mass,mh,ma)if moments else mass
GROUP=np.array([[int(g//2==0),int(g//2==1),g%2]for g in range(6)],dtype=float)
def observed_mass(goals,shares):
 out=np.zeros((len(goals),6))
 for h,a in set(map(tuple,goals.astype(int))):
  mask=(goals[:,0]==h)&(goals[:,1]==a);out[mask]=groupmass(h,a,shares[mask])
 return out

def fit_allocation(x,base,target,weight,penalty):
 logbase=np.log(np.maximum(base,1e-300)); impossible=base==0
 def objective(flat):
  w=flat.reshape(x.shape[1],3);eta=x@w;logit=logbase+eta@GROUP.T;logit[impossible]=-1e300
  prob=softmax(logit,axis=1);loss=-np.sum(weight*np.log(np.maximum(prob[np.arange(len(x)),target],1e-300)))+penalty*np.square(w[1:]).sum()/2
  residual=prob;residual[np.arange(len(x)),target]-=1
  gradient=x.T@((residual@GROUP)*weight[:,None]);gradient[1:]+=penalty*w[1:]
  return loss,gradient.ravel()
 result=minimize(objective,np.zeros(x.shape[1]*3),jac=True,method='L-BFGS-B',options={'maxiter':1200,'ftol':1e-11,'gtol':1e-5,'maxls':40})
 return result.x.reshape(x.shape[1],3),{'success':bool(result.success),'iterations':int(result.nit),'gradientMax':float(abs(result.jac).max())}
def allocate(x,w,rates,shares):
 factor=np.exp(np.clip((x@w)@GROUP.T,-30,30));prob=np.zeros((len(x),3));btts=np.zeros(len(x));first=np.zeros((len(x),2));total=np.zeros(len(x));error=0.
 # Thirty goals per side leaves negligible mass for the serving rate limits.
 for h in range(31):
  ph=poisson.pmf(h,rates[:,0])
  for a in range(31):
   grid=ph*poisson.pmf(a,rates[:,1])
   if grid.max()<1e-13:continue
   base,mh,ma=groupmass(h,a,shares,True);normal=(base*factor).sum(axis=1);q=base*factor/normal[:,None]
   error=max(error,float(abs(q.sum(axis=1)-1).max()));prob+=grid[:,None]*q.reshape(-1,3,2).sum(axis=2)
   btts+=grid*q[:,1::2].sum(axis=1);first[:,0]+=grid*(mh*factor).sum(axis=1)/normal;first[:,1]+=grid*(ma*factor).sum(axis=1)/normal;total+=grid
 return prob,btts,first,{'cellNormalizationMaxError':error,'omittedGridMassMax':float(abs(1-total).max())}
def scores(prob,target):
 return {'accuracy':float((prob.argmax(axis=1)==target).mean()),'logLoss':float(-np.log(np.maximum(prob[np.arange(len(target)),target],1e-12)).mean()),'brier':float(np.square(prob-np.eye(3)[target]).sum(axis=1).mean()),'favorites':np.bincount(prob.argmax(axis=1),minlength=3).tolist(),'correct':int((prob.argmax(axis=1)==target).sum()),'classRecall':[float((prob.argmax(axis=1)[target==c]==c).mean())for c in range(3)],'classPrecision':[float((target[prob.argmax(axis=1)==c]==c).mean())if (prob.argmax(axis=1)==c).any()else None for c in range(3)]}

def evaluate():
 from datetime import timedelta
 from scripts.evaluate_multiseason_forecasts import fit_main,predict_main
 from match_pool_module import forecast_multiseason as multi
 protocol_path=OUT/'protocol.json'
 if protocol_path.exists():assert json.loads(protocol_path.read_text())==PROTOCOL
 else:save(protocol_path,PROTOCOL)
 if not(OUT/'event-features.json').exists():make_features()
 events=json.loads((OUT/'event-features.json').read_text());records=json.loads((SRC/'test-predictions.json').read_text());allresults=defaultdict(list);details=[];audits=[];diagnostics=[]
 for day in PROTOCOL['folds']:
  artifact=json.loads((SRC/f'fold-{day}.json').read_text())['artifacts'];strategy='legacy'if day=='2026-10-11'else'opponent';meta=json.loads((SRC/f'features-multi-{strategy}.json').read_text())
  with np.load(SRC/f'features-multi-{strategy}.npz')as a:data={**meta,**{k:a[k]for k in a.files}}
  n=len(data['ids']);lookup={identity:i for i,identity in enumerate(data['ids'])};times=[f.utc(t)for t in data['times']];cutoff=f.utc(day);inner=cutoff-timedelta(days=21)
  valid=np.isfinite(data['first'].reshape(n,2)).all(axis=1);train=np.array([i for i,t in enumerate(times)if t+timedelta(hours=3)<=inner and valid[i]]);outer=np.array([i for i,t in enumerate(times)if t+timedelta(hours=3)<=cutoff and valid[i]]);tune=np.array([i for i,t in enumerate(times)if inner<=t and t+timedelta(hours=3)<=cutoff and valid[i]])
  rows=[r for r in records if r['fold']==day];test=np.array([lookup[r['id']]for r in rows]);rates=np.array([r['models']['main_upgrade']['rates']for r in rows]);oldshares=np.array([r['models']['main_upgrade']['shares']for r in rows])
  raw=data['raw'].reshape(n,2,-1)[:,:,:20].reshape(n,-1)
  x=np.column_stack([data['halfx'].reshape(n,-1),raw,np.array([events[str(i)]for i in data['ids']])]);x=np.nan_to_num(x,nan=0,posinf=0,neginf=0)
  goals=data['goals'].reshape(n,2);first=data['first'].reshape(n,2);target=np.where(2*first.sum(axis=1)>goals.sum(axis=1),0,np.where(2*first.sum(axis=1)==goals.sum(axis=1),1,2));group=target*2+np.all(first>0,axis=1)
  shares=data['halfprior'].reshape(n,2)
  weights=lambda indices,when:multi.weights(data,indices,when,540.).reshape(-1,2).mean(axis=1)
  scaler=StandardScaler().fit(x[train]);xt=np.clip(scaler.transform(x),-8,8);xt=np.column_stack([np.ones(n),xt]);wt=weights(train,inner)
  # Freeze main settings previously selected within this fold's training history.
  main=fit_main(data,train,inner,artifact['selected']['multi'],artifact['linearColumns'],artifact['treeColumns']);tunerates=predict_main(main,data,tune)
  base=observed_mass(goals[train],shares[train]);assert np.all(base[np.arange(len(train)),group[train]]>0)
  candidates=[]
  for penalty in PROTOCOL['allocationRidge']:
   w,status=fit_allocation(xt[train],base,group[train],wt,penalty);p,b,m,check=allocate(xt[tune],w,tunerates,shares[tune]);metrics=scores(p,target[tune]);candidates.append({'kind':'allocation','penalty':penalty,**metrics});diagnostics.append({'fold':day,'stage':'inner','penalty':penalty,**status,**check});print('TUNE_ALLOC',day,penalty,metrics,flush=True)
  def classifier(spec):
   if spec[0]=='logistic':return LogisticRegression(C=spec[1],max_iter=500)
   return HistGradientBoostingClassifier(max_depth=spec[1],max_iter=100,min_samples_leaf=100,l2_regularization=10,learning_rate=.05,early_stopping=False,random_state=20261104)
  specs=[('logistic',.01),('logistic',.1),('logistic',1.),('hist',2),('hist',3)]
  for spec in specs:
   model=classifier(spec);model.fit(xt[train,1:],target[train],sample_weight=wt);p=model.predict_proba(xt[tune,1:]);metrics=scores(p,target[tune]);candidates.append({'kind':'classifier','spec':spec,**metrics});print('TUNE_CLASS',day,spec,metrics,flush=True)
  scaler=StandardScaler().fit(x[outer]);xt=np.column_stack([np.ones(n),np.clip(scaler.transform(x),-8,8)]);wo=weights(outer,cutoff);base=observed_mass(goals[outer],shares[outer])
  result={'current':timing_metrics(goals[test],first[test],rates,oldshares)['halfProbabilities'],'always_second':np.tile([0.,0.,1.],(len(test),1))};extra={}
  for kind in('allocation','classifier'):
   options=[c for c in candidates if c['kind']==kind]
   for selection in('loss','accuracy'):
    chosen=min(options,key=lambda c:(c['logLoss'],))if selection=='loss'else min(options,key=lambda c:(-c['accuracy'],c['logLoss']))
    name=f'{kind}_{selection}';audits.append({'fold':day,'variant':name,'selected':chosen,'candidates':options})
    if kind=='allocation':
     w,status=fit_allocation(xt[outer],base,group[outer],wo,chosen['penalty']);p,b,m,check=allocate(xt[test],w,rates,shares[test]);extra[name]={'btts':b,'first':m};diagnostics.append({'fold':day,'stage':'outer','variant':name,**status,**check})
     np.savez_compressed(OUT/f'{day}-{name}.npz',weights=w,mean=scaler.mean_,scale=scaler.scale_)
    else:
     model=classifier(chosen['spec']);model.fit(xt[outer,1:],target[outer],sample_weight=wo);p=model.predict_proba(xt[test,1:])
    result[name]=p
  extra['current']={'btts':(1-np.exp(-rates[:,0]*oldshares[:,0]))*(1-np.exp(-rates[:,1]*oldshares[:,1])),'first':rates*oldshares}
  for i,r in enumerate(rows):
   details.append({'id':r['id'],'at':r['at'],'fold':day,'league':r['league'],'target':int(target[test[i]]),'probabilities':{name:p[i].tolist()for name,p in result.items()}})
  for name,p in result.items():
   entry={'fold':day,'prob':p,'target':target[test],'dates':[r['at'][:10]for r in rows]}
   if name in extra:
    b=extra[name]['btts'];m=extra[name]['first'];y=np.all(first[test]>0,axis=1);entry['bttsLoss']=-(y*np.log(np.clip(b,1e-12,1))+(1-y)*np.log(np.clip(1-b,1e-12,1)));entry['bttsCorrect']=(b>=.5)==y;entry['goalMAE']=(abs(first[test]-m).sum(axis=1)+abs(goals[test]-first[test]-(rates-m)).sum(axis=1))/4
   allresults[name].append(entry)
  save(OUT/'selection-audit.json',audits);save(OUT/'diagnostics.json',diagnostics);save(OUT/'test-predictions.json',details);print('FOLD_DONE',day,flush=True)
 results={};baseline=np.concatenate([r['prob']for r in allresults['current']]);y=np.concatenate([r['target']for r in allresults['current']]);dates=sum([r['dates']for r in allresults['current']],[])
 for name,entries in allresults.items():
  p=np.concatenate([r['prob']for r in entries]);report=scores(p,y);report['folds']=[{'fold':r['fold'],**scores(r['prob'],r['target'])}for r in entries];report['accuracyVsCurrent']=paired((p.argmax(axis=1)==y).astype(float)-(baseline.argmax(axis=1)==y),dates)
  if name!='always_second':report['logLossVsCurrent']=paired(-np.log(np.clip(p[np.arange(len(y)),y],1e-12,1))+np.log(baseline[np.arange(len(y)),y]),dates)
  if 'bttsLoss'in entries[0]:report.update(firstHalfBTTSLogLoss=float(np.concatenate([r['bttsLoss']for r in entries]).mean()),firstHalfBTTSAccuracy=float(np.concatenate([r['bttsCorrect']for r in entries]).mean()),halfGoalMAE=float(np.concatenate([r['goalMAE']for r in entries]).mean()))
  results[name]=report
 save(OUT/'results.json',{'matches':len(y),'actualClasses':np.bincount(y,minlength=3).tolist(),'models':results,'protocol':PROTOCOL,'quality':json.loads((OUT/'quality.json').read_text())})
 print('RESULTS',json.dumps(results),flush=True)

def render_report():
 import html
 result=json.loads((OUT/'results.json').read_text());rows=json.loads((OUT/'test-predictions.json').read_text());dates=[r['at'][:10]for r in rows];y=np.array([r['target']for r in rows]);models=result['models']
 labels={'current':'Current half model + approved main model','always_second':'Always second half','allocation_loss':'Coherent allocation: probability selection','allocation_accuracy':'Coherent allocation: accuracy selection','classifier_loss':'Direct classifier: probability selection','classifier_accuracy':'Direct classifier: accuracy selection'}
 for name,m in models.items():
  p=np.array([r['probabilities'][name]for r in rows]);m['accuracyVsAlwaysSecond']=paired((p.argmax(axis=1)==y).astype(float)-(y==2),dates);matrix=np.zeros((3,3),dtype=int)
  for actual,predicted in zip(y,p.argmax(axis=1)):matrix[actual,predicted]+=1
  m['confusionActualRowsPredictedColumns']=matrix.tolist()
 save(OUT/'results.json',result)
 table=''
 for name,m in models.items():
  loss='—'if name=='always_second'else f'{m["logLoss"]:.5f}'
  table+=f'<tr><td>{html.escape(labels[name])}</td><td>{100*m["accuracy"]:.2f}% ({m["correct"]}/{result["matches"]})</td><td>{loss}</td><td>{m["favorites"][0]}</td><td>{m["favorites"][1]}</td><td>{m["favorites"][2]}</td></tr>'
 content=f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>ScoutWise half-model experiments</title><style>body{{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;color:#152532}}table{{border-collapse:collapse;width:100%}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #ddd}}pre{{white-space:pre-wrap;font-size:13px;background:#f3f5f7;padding:20px}}h1{{font-size:28px}}</style><h1>Half-model experiments · 4 November 2026</h1><p>{result['matches']} historical fixtures, 11 October–3 November, across three rolling test windows. All models use the same fixtures. Actual outcomes: first {result['actualClasses'][0]}, equal {result['actualClasses'][1]}, second {result['actualClasses'][2]}.</p><p>Hyperparameters selected on the preceding 21 days only; refit before each test window. Dates were examined in earlier research, so this is retrospective evidence, not a fresh prospective test. No live model changed.</p><table><tr><th>Method</th><th>Accuracy</th><th>Log loss ↓</th><th>First favorites</th><th>Equal favorites</th><th>Second favorites</th></tr>{table}</table><h2>What was tested</h2><p>Direct three-class classification with regularized logistic regression and gradient-boosted trees. Coherent allocation tilts the binomial distribution of half scores within each possible full-time score, then normalizes separately within that score cell. This preserves the full-time distribution and derives higher-half probabilities, first-half BTTS and projected half goals consistently.</p><p>Inputs include existing causal form and timing features, historical goal-minute bins, and first/second-half shots, attacks, dangerous attacks, possession and corners where available. Historical events reconcile with recorded scores for {result['quality']['eventValidated']:,} of 33,925 matches; paired half statistics exist for {result['quality']['pairedHalfStatistics']:,}. Missing coverage has explicit indicators. Histories use only prior settled results available at the daily update.</p><h2>Detailed metrics and paired uncertainty</h2><p>Confidence intervals resample matchdays (4,000 samples). Accuracy differences are proportions; multiply by 100 for percentage points. First-half BTTS and goal MAE apply only to coherent allocation and current methods. The direct classifier is a benchmark, not a consistent replacement for every half market.</p><pre>{html.escape(json.dumps(result,indent=2))}</pre></html>'''
 (OUT/'report.html').write_text(content)
 print('REPORT',OUT/'report.html',flush=True)

if __name__=="__main__":
 import argparse
 parser=argparse.ArgumentParser();parser.add_argument("--collect-only",action="store_true");parser.add_argument("--evaluate-only",action="store_true");args=parser.parse_args()
 if not args.evaluate_only:collect()
 if not args.collect_only:evaluate();render_report()
