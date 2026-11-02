"""The same 78 report metrics used in the earlier offline forecast review."""
import math
from collections import defaultdict
import numpy as np
LABELS = {}
KINDS = {}
SECTIONS = {}
expanded = {}

def prepare(records):
    expanded.clear()
    for row in records:
        expanded[row['id']] = {name: entries(row, distribution) for name, distribution in row['models'].items()}

def entries(r,d):
 hg,ag=r['hg'],r['ag'];h,a=d['rates'];result={}
 def add(key,label,section,p,y,kind='classification'):
  LABELS[key]=label;KINDS[key]=kind;SECTIONS[key]=section
  result[key]={'p':p,'y':y}
 def binary(key,label,section,p,y):add(key,label,section,[1-p,p],int(y))
 def multi(key,label,section,prob,y,classes):
  add(key,label,section,prob,y)
  for index,cl in enumerate(classes):binary(key+'_'+str(index),label+': '+cl,section,prob[index],y==index)
 outcome=0 if hg>ag else 1 if hg==ag else 2
 multi('outcome','Match result','Match Outcome',[d['outcomes'][k] for k in ('home','draw','away')],outcome,['Home win','Draw','Away win'])
 add('projected_home','Projected home goals — MAE','Match Outcome',h,hg,'regression')
 add('projected_away','Projected away goals — MAE','Match Outcome',a,ag,'regression')
 for rank,score in enumerate(d['topScores']):
  hit=score['home']==hg and score['away']==ag
  add('top'+str(rank+1),'Exact score — rank '+str(rank+1),'Score Probabilities',score['probability'],int(hit),'retrieval')
 top=d['topScores'];add('top3_hit','Actual score inside top three','Score Probabilities',sum(x['probability'] for x in top),int(any(x['home']==hg and x['away']==ag for x in top)),'retrieval')
 pexact=math.exp(-h-a+hg*math.log(h)+ag*math.log(a)-math.lgamma(hg+1)-math.lgamma(ag+1))
 add('score_joint','Exact-score distribution — log loss','Score Probabilities',pexact,1,'likelihood')
 grid=d['grid'];prob=[x['probability'] for x in grid]+[d['gridRemainder']]
 y=next((i for i,x in enumerate(grid) if x['home']==hg and x['away']==ag),len(grid))
 add('score_grid','Score map (36 cells + other)','Score Probabilities',prob,y)
 for index,p in enumerate(prob):
  label=f"{grid[index]['home']}–{grid[index]['away']}" if index<len(grid) else 'Other scores'
  binary('grid_'+str(index),'Score-map cell '+label,'Score Probability Cells',p,y==index)
 total=hg+ag
 multi('total_range','Total goals: 0–1 / 2–3 / 4+','Goal Distribution',d['totalGoalRanges'],0 if total<=1 else 1 if total<=3 else 2,['0–1','2–3','4+'])
 binary('over25','3+ total goals','Goal Distribution',d['over25'],total>=3)
 for side,goals in (('home',hg),('away',ag)):
  multi('goals_'+side,side.title()+' goal count (0–5 / 6+)','Goal Distribution',d['goalDistribution'][side],min(goals,6),['0','1','2','3','4','5','6+'])
 binary('btts','Both teams score — full match','Goal Scenarios',d['bothTeamsScore'],hg>0 and ag>0)
 binary('clean_home','Home clean sheet','Goal Scenarios',d['cleanSheet']['home'],ag==0)
 binary('clean_away','Away clean sheet','Goal Scenarios',d['cleanSheet']['away'],hg==0)
 half=d.get('halfScenarios')
 if half and r.get('hhg') is not None and r.get('hag') is not None:
  first=r['hhg']+r['hag'];second=total-first
  binary('btts_first','Both teams score — first half','Goal Scenarios',half['firstHalfBothTeamsScore'],r['hhg']>0 and r['hag']>0)
  multi('higher_half','Higher-scoring half','Goal Scenarios',[half['moreGoalsHalf'][k] for k in ('first','equal','second')],0 if first>second else 1 if first==second else 2,['First half','Equal','Second half'])
 return result

def summarize(rows,names):
 out={}
 for name in names:
  collected=defaultdict(list)
  for r in rows:
   for key,e in expanded[r['id']][name].items():collected[key].append((e,r))
  result={}
  for key,items in collected.items():
   kind=KINDS[key];n=len(items)
   if kind=='regression':
    p=np.array([e['p'] for e,r in items]);y=np.array([e['y'] for e,r in items]);result[key]={'n':n,'mae':float(abs(p-y).mean()),'rmse':float(np.sqrt(np.mean((p-y)**2))),'meanPredicted':float(p.mean()),'meanActual':float(y.mean())}
   elif kind=='likelihood':result[key]={'n':n,'logLoss':float(np.mean([-math.log(max(e['p'],1e-15)) for e,r in items]))}
   else:
    if kind=='retrieval':p=np.array([[1-e['p'],e['p']] for e,r in items]);y=np.array([e['y'] for e,r in items]);accuracy=float(y.mean())
    else:p=np.array([e['p'] for e,r in items]);y=np.array([e['y'] for e,r in items]);accuracy=float((p.argmax(axis=1)==y).mean())
    obs=np.eye(p.shape[1])[y];brier=float(np.mean((p[:,1]-y)**2)) if p.shape[1]==2 else float(np.mean(np.sum((p-obs)**2,axis=1)))
    stats={'n':n,'accuracy':accuracy,'brier':brier,'logLoss':float(-np.log(np.maximum(p[np.arange(n),y],1e-15)).mean()),'majorityReference':float(np.bincount(y,minlength=p.shape[1]).max()/n)}
    if p.shape[1]==2:
     stats.update(meanPredicted=float(p[:,1].mean()),eventRate=float(y.mean()))
    confidence=p.max(axis=1);correct=(p.argmax(axis=1)==y).astype(float);ece=0
    for lo in np.arange(0.,1.,.1):
     mask=(confidence>=lo)&(confidence<lo+.1 if lo<.9 else confidence<=1.)
     if mask.any():ece+=mask.mean()*abs(confidence[mask].mean()-correct[mask].mean())
    stats['confidenceECE']=float(ece)
    result[key]=stats
  out[name]=result
 return out
