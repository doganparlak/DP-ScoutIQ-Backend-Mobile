"""Offline timing review; never writes the database, model registry or caches.

Train before Oct 11/18/25, select on the first two weeks, confirm on the last.
Archived causal full-match forecasts remain fixed for every timing challenger.
"""
import argparse
from collections import defaultdict
from datetime import timedelta
import hashlib
import html
import json
from pathlib import Path

import numpy as np
from scipy.stats import skellam
from match_pool_module import forecast as f, forecast_learning as learning, forecast_timing as timing
from match_pool_module.forecast_history import daily_boundary

FOLDS = ('2026-10-11', '2026-10-18', '2026-10-25')
OPTIONS = [('logistic', 1., 2), ('logistic', 10., 2), ('logistic', 100., 2),
           ('tree', 0., 1), ('tree', 0., 2)]


def examples(matches):
    histories, totals, ratings = defaultdict(list), {}, {}
    pending, index, rows = [], 0, []
    for m in sorted(matches, key=lambda r: (f.utc(r['at']), r['id'])):
        boundary = daily_boundary(f.utc(m['at']))
        while index < len(pending) and f.utc(pending[index]['at']) + timedelta(hours=3) <= boundary:
            prev = pending[index]
            learning.append_match(histories, prev, ratings)
            f._update_half_totals(prev, totals)
            f._update_context(prev, {}, ratings)
            index += 1
        own, opp = histories[m['home']][-10:], histories[m['away']][-10:]
        if min(len(own), len(opp)) >= f.MIN_HISTORY and all(
            m.get(first) is not None and 0 <= m[first] <= m[full]
            for first, full in (('hhg','hg'), ('hag','ag'))):
            priors = [f._half_prior(totals, m['league'], home) for home in (True, False)]
            rows.append({'id':m['id'], 'at':m['at'], 'league':m['league'],
                         'x': [timing.features(own,opp,True,priors[0]), timing.features(opp,own,False,priors[1])],
                         'histories': [own,opp], 'prior':priors,
                         'first':[m['hhg'],m['hag']], 'full':[m['hg'],m['ag']]})
        pending.append(m)
    return rows


def infer(model, rows):
    shares=[]
    for r in rows:
        a,b=r['histories']
        shares.append([timing.predict_share(model,a,b,True,r['prior'][0]),
                       timing.predict_share(model,b,a,False,r['prior'][1])])
    return np.asarray(shares)


def losses(rows, rates, shares):
    goals=np.asarray([r['full'] for r in rows]);first=np.asarray([r['first'] for r in rows]);second=goals-first
    first_rates=rates*shares;second_rates=rates-first_rates
    a,b=first_rates.sum(axis=1),second_rates.sum(axis=1)
    probabilities=np.column_stack([skellam.sf(0,a,b),skellam.pmf(0,a,b),skellam.cdf(-1,a,b)])
    target=np.where(first.sum(axis=1)>second.sum(axis=1),0,np.where(first.sum(axis=1)==second.sum(axis=1),1,2))
    btts=(1-np.exp(-first_rates[:,0]))*(1-np.exp(-first_rates[:,1]))
    actual_btts=np.all(first>0,axis=1)
    ll=-np.log(np.clip(probabilities[np.arange(len(rows)),target],1e-12,1))
    brier=((probabilities-np.eye(3)[target])**2).sum(axis=1)
    btts_ll=-(actual_btts*np.log(np.clip(btts,1e-12,1))+(1-actual_btts)*np.log(np.clip(1-btts,1e-12,1)))
    poisson=(first_rates-first*np.log(first_rates)+second_rates-second*np.log(second_rates)).sum(axis=1)
    return {'halfLogLoss':ll,'halfBrier':brier,'firstHalfBTTSLogLoss':btts_ll,'halfGoalPoissonLoss':poisson,
            'probabilities':probabilities,'firstHalfBTTS':btts,'targets':target,'shares':shares}


def summary(loss):
    winners=loss['probabilities'].argmax(axis=1)
    return {**{k:float(loss[k].mean()) for k in ('halfLogLoss','halfBrier','firstHalfBTTSLogLoss','halfGoalPoissonLoss')},
            'matches':len(winners),'accuracy':float((winners==loss['targets']).mean()),
            'favorites':dict(zip(('first','equal','second'),np.bincount(winners,minlength=3).tolist())),
            'actualOutcomes':dict(zip(('first','equal','second'),np.bincount(loss['targets'],minlength=3).tolist())),
            'firstHalfShareRange':[float(loss['shares'].min()),float(loss['shares'].max())],
            'firstHalfFavoriteAccuracy':float((loss['targets'][winners==0]==0).mean()) if (winners==0).any() else None}


def bootstrap(delta,rows):
    days=sorted({r['at'][:10] for r in rows})
    grouped=[delta[[r['at'][:10]==d for r in rows]] for d in days]
    rng=np.random.default_rng(20261104)
    samples=[float(np.concatenate([grouped[i] for i in rng.integers(0,len(days),len(days))]).mean()) for _ in range(3000)]
    return {'meanDifference':float(delta.mean()),'dateCluster95CI':np.quantile(samples,[.025,.975]).tolist(),'dateClusters':len(days)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,default=Path('/tmp/scoutwise-forecast-upgrade/prepared.json'))
    parser.add_argument('--records',type=Path,default=Path('/tmp/scoutwise-forecast-ensemble-v6/records.json'))
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    protocol={'folds':FOLDS,'selectionWeeks':FOLDS[:2],'confirmationWeek':FOLDS[-1],
              'options':OPTIONS,'residualBlend':[.25,.5,1.], 'selectionMetric':'halfLogLoss',
              'fullMatchRates':'fixed archived rate_blend forecasts, trained before each week',
              'history':'last ten settled matches, 04:00 UTC refresh and three-hour settlement',
              'gate':'confirmation half log-loss date-cluster CI entirely below zero; Brier improves; BTTS and half goal loss do not worsen; first-half favorites exist',
              'sourceSHA256':hashlib.sha256(args.source.read_bytes()).hexdigest(),
              'forecastRecordsSHA256':hashlib.sha256(args.records.read_bytes()).hexdigest(),
              'scope':'Retrospective chronological review; final week excluded from this experiment selection, but used in previous full-match model reviews. No prospective claim.',
              'activationPerformed':False}
    (args.output/'protocol.json').write_text(json.dumps(protocol,indent=2))
    source=json.loads(args.source.read_text());rows=examples(source['matches'])
    archived={r['id']:r for r in json.loads(args.records.read_text())['records']}
    all_losses=defaultdict(list); fold_artifacts={}; evaluated=[]
    for day in FOLDS:
        cutoff=f.utc(day+'T00:00:00Z')
        train=[r for r in rows if f.utc(r['at'])+timedelta(hours=3)<=cutoff]
        test=[r for r in rows if r['id'] in archived and archived[r['id']]['fold']==day]
        rates=np.asarray([archived[r['id']]['models']['rate_blend']['rates'] for r in test])
        x=np.asarray([v for r in train for v in r['x']]);prior=np.asarray([v for r in train for v in r['prior']])
        first=np.asarray([v for r in train for v in r['first']]);full=np.asarray([v for r in train for v in r['full']])
        print('FOLD',day,'train',len(train),'test',len(test),flush=True)
        all_losses['baseline'].append(losses(test,rates,np.asarray([r['prior'] for r in test])))
        fold_artifacts[day]={}
        for method,penalty,depth in OPTIONS:
            name=f'{method}_{penalty:g}_{depth}'
            model=timing.fit(x,prior,first,full,method,penalty,depth)
            fold_artifacts[day][name]=model
            shares=infer(model,test)
            for blend in (.25,.5,1.):
                key=f'{name}_blend{blend:g}'
                blended=1/(1+np.exp(-(timing.logit(np.asarray([r['prior'] for r in test]))+blend*(timing.logit(shares)-timing.logit(np.asarray([r['prior'] for r in test]))))))
                all_losses[key].append(losses(test,rates,blended))
            print('FIT',name,flush=True)
        evaluated.extend(test)
    def joined(name,indices):
        return {key:np.concatenate([all_losses[name][i][key] for i in indices]) for key in all_losses[name][0]}
    tuning={name:summary(joined(name,[0,1])) for name in all_losses}
    selected=min((name for name in tuning if name!='baseline'),key=lambda name:tuning[name]['halfLogLoss'])
    baseline=all_losses['baseline'][2];candidate=all_losses[selected][2]
    confirm_rows=[r for r in evaluated if archived[r['id']]['fold']==FOLDS[-1]]
    b,c=summary(baseline),summary(candidate)
    uncertainty=bootstrap(candidate['halfLogLoss']-baseline['halfLogLoss'],confirm_rows)
    gate=tuning[selected]['halfLogLoss']<tuning['baseline']['halfLogLoss'] and uncertainty['dateCluster95CI'][1]<0 and c['halfBrier']<b['halfBrier'] and c['firstHalfBTTSLogLoss']<=b['firstHalfBTTSLogLoss'] and c['halfGoalPoissonLoss']<=b['halfGoalPoissonLoss'] and c['favorites']['first']>0
    result={'protocol':protocol,'selection':selected,'tuning':tuning,'confirmation':{'baseline':b,'candidate':c,'uncertainty':uncertainty},
            'byWeek':{day:{'baseline':summary(all_losses['baseline'][i]),'candidate':summary(all_losses[selected][i])} for i,day in enumerate(FOLDS)},
            'promotionPassed':bool(gate),'activationPerformed':False, 'selectionBeatsBaseline':tuning[selected]['halfLogLoss']<tuning['baseline']['halfLogLoss']}
    if selected!='baseline':
        name,blend=selected.rsplit('_blend',1)
        artifact={**fold_artifacts[FOLDS[-1]][name],'blend':float(blend),'trainedBefore':FOLDS[-1]}
        (args.output/'confirmation-model.json').write_text(json.dumps(artifact,allow_nan=False))
    # Include all candidates on confirmation for transparency, never reselect on these results.
    result['confirmationAllCandidates']={name:summary(all_losses[name][2]) for name in all_losses}
    (args.output/'results.json').write_text(json.dumps(result,indent=2,allow_nan=False))
    records=[]
    for r,p,q,actual in zip(confirm_rows,baseline['probabilities'],candidate['probabilities'],candidate['targets']):
        records.append({'fixtureId':r['id'],'at':r['at'],'league':r['league'],'baseline':p.tolist(),'candidate':q.tolist(),'actual':int(actual)})
    (args.output/'confirmation-predictions.json').write_text(json.dumps(records,indent=2))
    (args.output/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>Fixture timing validation</title><h1>Fixture timing validation</h1><pre>'+html.escape(json.dumps(result,indent=2))+'</pre>')
    print(json.dumps({'selection':selected,'confirmation':result['confirmation'],'promotionPassed':bool(gate)},indent=2),flush=True)

if __name__=='__main__':main()
