"""Nested chronological, paired main/timing review. No live database mutations."""
import argparse
from collections import defaultdict
from datetime import timedelta
import hashlib
import html
import json
import math
from pathlib import Path
from time import perf_counter

import numpy as np
from scipy.special import gammaln
from scipy.stats import poisson, skellam
from match_pool_module import forecast as f, forecast_candidates as c, forecast_learning as l
from match_pool_module import forecast_multiseason as multi, forecast_timing as timing

FOLDS=('2026-10-11','2026-10-18','2026-10-25')
DECAYS=(180.,540.)
PENALTIES=(80.,320.)
TREES=((2,40,200,100.),(3,60,100,100.))
BLENDS=(0.,.5,1.)
TIMING_OPTIONS=(('logistic',10.,2),('logistic',100.,2),('tree',0.,1),('tree',0.,2))
TIMING_BLENDS=(.25,.5,1.)


def save(path,value):path.write_text(json.dumps(value,allow_nan=False,indent=2))

def positions(indices):return np.asarray([[2*i,2*i+1]for i in indices],dtype=int).ravel()

def subset(data,ids):return [i for i,identity in enumerate(data['ids'])if identity in ids]

def cached_features(output,name,matches,strategy,competitions,prior):
    root=output/f'features-{name}'
    if root.with_suffix('.json').exists()and root.with_suffix('.npz').exists():
        meta=json.loads(root.with_suffix('.json').read_text())
        with np.load(root.with_suffix('.npz'))as arrays:return {**meta,**{k:arrays[k]for k in arrays.files}}
    print('FEATURES',name,len(matches),flush=True)
    data=multi.make_examples(matches,strategy,competitions,prior,season_aware=name.startswith('multi'))
    arrays={k:v for k,v in data.items()if isinstance(v,np.ndarray)}
    meta={k:v for k,v in data.items()if k not in arrays}
    np.savez_compressed(root.with_suffix('.npz'),**arrays);save(root.with_suffix('.json'),meta)
    print('FEATURES_DONE',name,len(data['ids']),flush=True)
    return data


def train_options(data,train,tune,cutoff,linear_cols,tree_cols):
    raw=data['raw'][positions(tune)];target=data['goals'][positions(tune)]
    best=None;audit=[]
    for decay in DECAYS:
        for penalty in PENALTIES:
            for spec in TREES:
                linear,tree=multi.fit_components(data,train,cutoff,decay,linear_cols,tree_cols,penalty,spec)
                a,b=c.linear_predict(linear,raw),c.tree_predict(tree,raw)
                for blend in BLENDS:
                    prediction=(1-blend)*a+blend*b
                    loss=float(np.mean(prediction-target*np.log(prediction)))
                    settings={'decayDays':decay,'penalty':penalty,'tree':spec,'blend':blend}
                    audit.append({**settings,'loss':loss})
                    if best is None or loss<best[0]:best=(loss,settings)
                print('TUNE_MAIN',cutoff.date().isoformat(),len(train),decay,penalty,spec[0],flush=True)
    return best[1],audit


def fit_main(data,indices,cutoff,settings,linear_cols,tree_cols):
    linear,tree=multi.fit_components(data,indices,cutoff,settings['decayDays'],linear_cols,tree_cols,settings['penalty'],settings['tree'])
    return {'linear':linear,'nonlinear':tree,'blend':settings['blend'],'settings':settings}


def predict_main(model,data,indices):
    return multi.predict_components(model['linear'],model['nonlinear'],data['raw'][positions(indices)],model['blend']).reshape(-1,2)


def timing_metrics(goals,first,rates,shares):
    valid=np.isfinite(first).all(axis=1);goals=goals[valid];first=first[valid];rates=rates[valid];shares=shares[valid]
    first_rate=rates*shares;second_rate=rates-first_rate;second=goals-first
    a,b=first_rate.sum(axis=1),second_rate.sum(axis=1)
    p=np.column_stack([skellam.sf(0,a,b),skellam.pmf(0,a,b),skellam.cdf(-1,a,b)])
    target=np.where(first.sum(axis=1)>second.sum(axis=1),0,np.where(first.sum(axis=1)==second.sum(axis=1),1,2))
    btts=(1-np.exp(-first_rate[:,0]))*(1-np.exp(-first_rate[:,1]));actual=np.all(first>0,axis=1)
    return {'halfLogLoss':-np.log(np.clip(p[np.arange(len(p)),target],1e-12,1)),
        'halfBrier':((p-np.eye(3)[target])**2).sum(axis=1),'halfAccuracy':(p.argmax(axis=1)==target).astype(float),
        'firstHalfBTTSAccuracy':((btts>=.5)==actual).astype(float),
        'firstHalfBTTSLogLoss':-(actual*np.log(np.clip(btts,1e-12,1))+(1-actual)*np.log(np.clip(1-btts,1e-12,1))),
        'halfGoalMAE':(np.abs(first-first_rate).sum(axis=1)+np.abs(second-second_rate).sum(axis=1))/4.,
        'firstFavorites':(p.argmax(axis=1)==0).astype(float),'equalFavorites':(p.argmax(axis=1)==1).astype(float),
        'halfProbabilities':p,'firstHalfShares':shares,'validMask':valid}


def train_timing(data,train,tune,cutoff,tune_rates):
    ptrain=positions(train);ptune=positions(tune)
    valid=np.isfinite(data['first'][ptrain]);goals=data['goals'][ptune].reshape(-1,2);first=data['first'][ptune].reshape(-1,2)
    best=None;audit=[]
    for decay in DECAYS:
        weights=multi.weights(data,train,cutoff,decay)[valid]
        for method,penalty,depth in TIMING_OPTIONS:
            model=timing.fit(data['halfx'][ptrain][valid],data['halfprior'][ptrain][valid],data['first'][ptrain][valid],
                data['goals'][ptrain][valid],method,penalty,depth,sample_weight=weights)
            for blend in TIMING_BLENDS:
                shares=timing.predict_matrix({**model,'blend':blend},data['halfx'][ptune],data['halfprior'][ptune]).reshape(-1,2)
                loss=float(timing_metrics(goals,first,tune_rates,shares)['halfLogLoss'].mean())
                setting={'decayDays':decay,'method':method,'penalty':penalty,'depth':depth,'blend':blend}
                audit.append({**setting,'loss':loss})
                if best is None or loss<best[0]:best=(loss,setting)
            print('TUNE_HALF',cutoff.date().isoformat(),method,decay,penalty,depth,flush=True)
    return best[1],audit


def fit_timing(data,indices,cutoff,setting):
    ps=positions(indices);valid=np.isfinite(data['first'][ps]);w=multi.weights(data,indices,cutoff,setting['decayDays'])[valid]
    model=timing.fit(data['halfx'][ps][valid],data['halfprior'][ps][valid],data['first'][ps][valid],data['goals'][ps][valid],
                    setting['method'],setting['penalty'],setting['depth'],sample_weight=w)
    return {**model,'blend':setting['blend'],'settings':setting}


def main_metrics(goals,rates):
    k=np.arange(31);hp=poisson.pmf(k[None,:],rates[:,0,None]);ap=poisson.pmf(k[None,:],rates[:,1,None])
    grid=hp[:,:,None]*ap[:,None,:];h,a=np.indices((31,31));flat=grid.reshape(len(rates),-1)
    outcomes=np.column_stack([grid[:,h>a].sum(axis=1),grid[:,h==a].sum(axis=1),grid[:,h<a].sum(axis=1)])
    y=np.where(goals[:,0]>goals[:,1],0,np.where(goals[:,0]==goals[:,1],1,2))
    top=np.argsort(-flat,axis=1)[:,:3];exact=goals[:,0]*31+goals[:,1]
    display=top[:,0].copy();leader=outcomes.argmax(axis=1)
    for i in range(len(top)):
        if np.count_nonzero(outcomes[i]==outcomes[i].max())!=1:continue
        for score in top[i]:
            sh,sa=divmod(int(score),31);category=0 if sh>sa else 1 if sh==sa else 2
            if category==leader[i]:display[i]=score;break
    margins=np.column_stack([grid[:,a-h>=2].sum(axis=1),grid[:,a-h==1].sum(axis=1),outcomes[:,1],grid[:,h-a==1].sum(axis=1),grid[:,h-a>=2].sum(axis=1)])
    ym=np.clip(goals[:,0]-goals[:,1],-2,2).astype(int)+2
    total=np.column_stack([grid[:,h+a<=1].sum(axis=1),grid[:,(h+a>=2)&(h+a<=3)].sum(axis=1),grid[:,h+a>=4].sum(axis=1)])
    yt=np.where(goals.sum(axis=1)<=1,0,np.where(goals.sum(axis=1)<=3,1,2))
    btts=(1-np.exp(-rates[:,0]))*(1-np.exp(-rates[:,1]));by=np.all(goals>0,axis=1)
    clean=np.exp(-rates[:,::-1]);cy=goals[:,::-1]==0
    return {'outcomeAccuracy':(outcomes.argmax(axis=1)==y).astype(float),
        'outcomeLogLoss':-np.log(np.clip(outcomes[np.arange(len(y)),y],1e-12,1)),
        'outcomeBrier':((outcomes-np.eye(3)[y])**2).sum(axis=1),
        'scoreLogLoss':(rates-goals*np.log(rates)+gammaln(goals+1)).sum(axis=1),
        'exactScoreAccuracy':(top[:,0]==exact).astype(float),'displayExactAccuracy':(display==exact).astype(float),
        'topThreeScoreAccuracy':(top==exact[:,None]).any(axis=1).astype(float),
        'goalMAE':abs(rates-goals).mean(axis=1),'marginAccuracy':(margins.argmax(axis=1)==ym).astype(float),
        'totalGoalRangeAccuracy':(total.argmax(axis=1)==yt).astype(float),
        'BTTSAccuracy':((btts>=.5)==by).astype(float),'cleanSheetAccuracy':((clean>=.5)==cy).mean(axis=1),
        'BTTSLogLoss':-(by*np.log(np.clip(btts,1e-12,1))+(1-by)*np.log(np.clip(1-btts,1e-12,1))),
        'cleanSheetLogLoss':-(cy*np.log(np.clip(clean,1e-12,1))+(1-cy)*np.log(np.clip(1-clean,1e-12,1))).mean(axis=1),
        'outcomeProbabilities':outcomes,'rates':rates}


def summarize(metrics):
    first=next(v for v in metrics.values()if isinstance(v,np.ndarray)and v.ndim==1 and v.dtype!=bool)
    return {'matches':len(first),**{key:float(value.mean())for key,value in metrics.items()if isinstance(value,np.ndarray)and value.ndim==1 and value.dtype!=bool},
            'correct':{key:int(round(value.sum()))for key,value in metrics.items()if key.endswith('Accuracy')and key!='cleanSheetAccuracy'},
            'firstHalfFavorites':int(metrics.get('firstFavorites',np.array([])).sum())}


def paired(delta,dates):
    unique=sorted(set(dates));sums=np.array([delta[np.array(dates)==d].sum()for d in unique]);counts=np.array([sum(x==d for x in dates)for d in unique])
    rng=np.random.default_rng(20261104);sample=rng.integers(0,len(unique),size=(4000,len(unique)))
    values=sums[sample].sum(axis=1)/counts[sample].sum(axis=1)
    return {'difference':float(delta.mean()),'dateCluster95CI':np.quantile(values,[.025,.975]).tolist(),'matchdays':len(unique)}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();out=args.output
    out.mkdir(parents=True,exist_ok=True)
    protocol={'folds':FOLDS,'testEndExclusive':'2026-11-04T00:00:00Z','innerValidationDays':21,'decayHalfLifeDays':DECAYS,
        'regularization':PENALTIES,'trees':TREES,'mainBlends':BLENDS,'timingOptions':TIMING_OPTIONS,'timingBlends':TIMING_BLENDS,
        'mainSelection':'mean Poisson loss on common inner validation fixtures','timingSelection':'three-way higher-scoring-half log loss on common inner validation fixtures',
        'testPolicy':'Test labels are never used to choose settings. Weekly refits may use earlier settled test weeks, as in rolling production.',
        'comparison':'same eligible fixture IDs across existing archived recipe, matched-budget current-season refit, multi-season main only, timing only, and combined',
        'priorPolicy':'2025 UEFA prior disabled before publication; no future-season totals or unsettled fixtures',
        'mainGate':'paired score log-loss 95% CI below zero; result accuracy no worse; displayed exact score no more than 0.5pp worse; MAE no more than .01 worse; no material large-league regression; confirmation score loss no more than .01 worse',
        'timingGate':'paired half log-loss 95% CI below zero; half accuracy no more than .2pp worse; BTTS loss no more than .002 worse; half MAE no more than .01 worse; confirmation loss no more than .01 worse',
        'activationPerformed':False,'limitations':'Retrospective evaluation; dates used in previous full-match reviews. No claim of prospective verification.'}
    protocol_path=out/'backtest-protocol.json'
    if protocol_path.exists():
        if json.loads(protocol_path.read_text())!=json.loads(json.dumps(protocol)):raise ValueError('Frozen protocol changed')
    else:save(protocol_path,protocol)
    source=json.loads((out/'dataset.json').read_text())
    coverage=json.loads((out.parent/'historical-coverage-2026-11-04'/'coverage.json').read_text())
    current_seasons={s['id']for league in coverage['leagues']for s in league['seasons']if s.get('is_current')}
    current_matches=[m for m in source['matches']if m['season']in current_seasons]
    prior=json.loads((out.parent/'league-offset-activation-2026-11-02'/'..'/'league-offset-2026-11-02'/'association-prior-2025.json').read_text())
    templates={day:json.loads(Path(f'/tmp/scoutwise-forecast-refresh-v4/improved44-{day}.json').read_text())for day in FOLDS}
    caches={}
    for strategy in sorted({t['strategy']for t in templates.values()}):
        caches[strategy]={'current':cached_features(out,'current-'+strategy,current_matches,strategy,source['competitions'],prior),
                          'multi':cached_features(out,'multi-'+strategy,source['matches'],strategy,source['competitions'],prior)}
    match_by={m['id']:m for m in source['matches']}
    names=('existing','current_retuned','main_upgrade','timing_upgrade','combined');all_records=[];reports=[];final_models={};metric_rows=defaultdict(list)
    for fold,day in enumerate(FOLDS):
        data_by=caches[templates[day]['strategy']];current=data_by['current'];expanded=data_by['multi']
        lookups={key:{identity:i for i,identity in enumerate(data['ids'])}for key,data in data_by.items()}
        common=set(current['ids'])&set(expanded['ids'])
        checkpoint=out/f'fold-{day}.json'
        if checkpoint.exists():
            result=json.loads(checkpoint.read_text());all_records.extend(result['predictions']);reports.append(result['report']);final_models=result['artifacts']
            print('CACHED_FOLD',day,flush=True);continue
        cutoff=f.utc(day+'T00:00:00Z');inner=cutoff-timedelta(days=21)
        end=f.utc(FOLDS[fold+1]+'T00:00:00Z')if fold+1<len(FOLDS)else f.utc(protocol['testEndExclusive'])
        test_ids=sorted([identity for identity in common if cutoff<=f.utc(match_by[identity]['at'])<end],key=lambda identity:(f.utc(match_by[identity]['at']),identity))
        tune_ids=sorted([identity for identity in common if inner<=f.utc(match_by[identity]['at']) and f.utc(match_by[identity]['at'])+timedelta(hours=3)<=cutoff],key=lambda identity:(f.utc(match_by[identity]['at']),identity))
        old=json.loads(Path(f'/tmp/scoutwise-forecast-ensemble-v6/options-{day}.json').read_text())
        linear_cols=old['models']['incumbent']['columns'];tree_cols=old['models']['nonlinear']['columns']
        print('FOLD',day,'test',len(test_ids),'inner validation',len(tune_ids),flush=True)
        fitted={};audit={};tune_current_rates=None;selected={}
        for scope,data in data_by.items():
            train=[i for i,at in enumerate(data['times'])if f.utc(at)+timedelta(hours=3)<=inner]
            tune=[lookups[scope][identity]for identity in tune_ids]
            outer=[i for i,at in enumerate(data['times'])if f.utc(at)+timedelta(hours=3)<=cutoff]
            settings,log=train_options(data,train,tune,inner,linear_cols,tree_cols)
            selected[scope]=settings;audit[scope]=log
            # Rebuild the inner current model with frozen selected settings to supply causal timing tuning rates.
            if scope=='current':
                inner_model=fit_main(data,train,inner,settings,linear_cols,tree_cols);tune_current_rates=predict_main(inner_model,data,tune)
            fitted[scope]=fit_main(data,outer,cutoff,settings,linear_cols,tree_cols)
        timing_train=[i for i,at in enumerate(expanded['times'])if f.utc(at)+timedelta(hours=3)<=inner]
        timing_tune=[lookups['multi'][identity]for identity in tune_ids]
        timing_settings,timing_audit=train_timing(expanded,timing_train,timing_tune,inner,tune_current_rates)
        timing_outer=[i for i,at in enumerate(expanded['times'])if f.utc(at)+timedelta(hours=3)<=cutoff]
        fitted_timing=fit_timing(expanded,timing_outer,cutoff,timing_settings)
        selected['timing']=timing_settings
        ic=[lookups['current'][identity]for identity in test_ids];im=[lookups['multi'][identity]for identity in test_ids]
        pcurrent=positions(ic);pmulti=positions(im)
        archived=json.loads(Path(f'/tmp/scoutwise-league-offset-review/candidate-{day}.json').read_text())
        if archived['features']!=current['names']:raise ValueError('Archived feature order mismatch')
        archived['linear']['calibration']=templates[day]['goalRateCalibration']
        existing_rates=(archived['weights']['incumbent']*c.linear_predict(archived['linear'],current['raw'][pcurrent])+archived['weights']['nonlinear']*c.tree_predict(archived['nonlinear'],current['raw'][pcurrent])).reshape(-1,2)
        control_rates=predict_main(fitted['current'],current,ic);new_rates=predict_main(fitted['multi'],expanded,im)
        strengths=(5.,15.,30.,60.,None);strength=templates[day]['halfTimeModel']['priorStrength']
        old_shares=current['halfshares'][ic,strengths.index(strength),:]
        new_shares=timing.predict_matrix(fitted_timing,expanded['halfx'][pmulti],expanded['halfprior'][pmulti]).reshape(-1,2)
        goals=current['goals'][pcurrent].reshape(-1,2);first=current['first'][pcurrent].reshape(-1,2)
        configs={'existing':(existing_rates,old_shares),'current_retuned':(control_rates,old_shares),
                 'main_upgrade':(new_rates,old_shares),'timing_upgrade':(existing_rates,new_shares),'combined':(new_rates,new_shares)}
        predictions=[];metrics={};times=[match_by[identity]['at']for identity in test_ids]
        for name,(rates,shares)in configs.items():
            full=main_metrics(goals,rates);half=timing_metrics(goals,first,rates,shares)
            metrics[name]={'main':summarize(full),'half':summarize(half)}
        for i,identity in enumerate(test_ids):
            predictions.append({'id':identity,'at':times[i],'fold':day,'league':match_by[identity]['league'],
                'goals':goals[i].tolist(),'first':[float(v)if np.isfinite(v)else None for v in first[i]],'models':{name:{'rates':rates[i].tolist(),'shares':shares[i].tolist()}for name,(rates,shares)in configs.items()}})
        artifacts={'main':fitted['multi'],'control':fitted['current'],'timing':fitted_timing,'selected':selected,'linearColumns':linear_cols,'treeColumns':tree_cols}
        report={'cutoff':day,'innerCutoff':inner.isoformat(),'testEnd':end.isoformat(),'trainingMatches':{key:sum(f.utc(at)+timedelta(hours=3)<=cutoff for at in data['times'])for key,data in data_by.items()},'innerValidationMatches':len(tune_ids),'metrics':metrics,'selected':selected}
        save(out/f'tuning-{day}.json',{'main':audit,'timing':timing_audit})
        save(checkpoint,{'report':report,'predictions':predictions,'artifacts':artifacts})
        all_records.extend(predictions);reports.append(report);final_models=artifacts
        print('FOLD_DONE',day,json.dumps(metrics),flush=True)
    evaluate(out,source,all_records,reports,final_models,data_by,protocol)


def evaluate(out,source,records,reports,artifacts,data_by,protocol):
    records=sorted(records,key=lambda r:(f.utc(r['at']),r['id']))
    if len({r['id']for r in records})!=len(records):raise ValueError('Test fixture appeared in multiple folds')
    goals=np.asarray([r['goals']for r in records]);first=np.asarray([r['first']for r in records],float)
    dates=[r['at'][:10]for r in records];valid=np.isfinite(first).all(axis=1);half_dates=[d for d,keep in zip(dates,valid)if keep]
    metrics={};full_arrays={};half_arrays={};names=list(records[0]['models'])
    for name in names:
        rates=np.asarray([r['models'][name]['rates']for r in records]);shares=np.asarray([r['models'][name]['shares']for r in records])
        full_arrays[name]=main_metrics(goals,rates);half_arrays[name]=timing_metrics(goals,first,rates,shares)
        metrics[name]={'main':summarize(full_arrays[name]),'half':summarize(half_arrays[name])}
        target=np.where(goals[:,0]>goals[:,1],0,np.where(goals[:,0]==goals[:,1],1,2));p=full_arrays[name]['outcomeProbabilities'];confidence=p.max(axis=1);hit=p.argmax(axis=1)==target
        ece=0.
        for left in np.arange(0,1,.1):
            chosen=(confidence>=left)&(confidence<left+.1)
            if chosen.any():ece+=chosen.mean()*abs(hit[chosen].mean()-confidence[chosen].mean())
        metrics[name]['main']['outcomeConfidenceECE']=float(ece)
    comparisons={}
    for name in names:
        if name=='existing':continue
        comparisons[name]={'main':{k:paired(full_arrays[name][k]-full_arrays['existing'][k],dates)for k in ('scoreLogLoss','outcomeAccuracy','displayExactAccuracy','outcomeBrier')},
            'half':{k:paired(half_arrays[name][k]-half_arrays['existing'][k],half_dates)for k in ('halfLogLoss','halfAccuracy','firstHalfBTTSLogLoss')}}
    by_league={};regressions=[]
    for league in sorted({r['league']for r in records}):
        mask=np.asarray([r['league']==league for r in records]);b=full_arrays['existing'];m=full_arrays['main_upgrade']
        by_league[str(league)]={'matches':int(mask.sum()),'existingAccuracy':float(b['outcomeAccuracy'][mask].mean()),'newAccuracy':float(m['outcomeAccuracy'][mask].mean()),
            'scoreLossDifference':float((m['scoreLogLoss'][mask]-b['scoreLogLoss'][mask]).mean())}
        if mask.sum()>=50 and by_league[str(league)]['scoreLossDifference']>.1:regressions.append(league)
    base=metrics['existing'];main=metrics['main_upgrade'];half=metrics['timing_upgrade'];control=metrics['current_retuned']
    # Require expanded data to also beat a control with the same bounded tuning budget.
    versus_control=paired(full_arrays['main_upgrade']['scoreLogLoss']-full_arrays['current_retuned']['scoreLogLoss'],dates)
    confirm=reports[-1]['metrics']
    main_gate=(comparisons['main_upgrade']['main']['scoreLogLoss']['dateCluster95CI'][1]<0
        and versus_control['difference']<0 and main['main']['outcomeAccuracy']>=base['main']['outcomeAccuracy']
        and main['main']['displayExactAccuracy']>=base['main']['displayExactAccuracy']-.005
        and main['main']['goalMAE']<=base['main']['goalMAE']+.01 and not regressions
        and confirm['main_upgrade']['main']['scoreLogLoss']<=confirm['existing']['main']['scoreLogLoss']+.01)
    timing_gate=(comparisons['timing_upgrade']['half']['halfLogLoss']['dateCluster95CI'][1]<0
        and half['half']['halfAccuracy']>=base['half']['halfAccuracy']-.002
        and half['half']['firstHalfBTTSLogLoss']<=base['half']['firstHalfBTTSLogLoss']+.002
        and half['half']['halfGoalMAE']<=base['half']['halfGoalMAE']+.01
        and confirm['timing_upgrade']['half']['halfLogLoss']<=confirm['existing']['half']['halfLogLoss']+.01)
    combined_gate=(main_gate and timing_gate
        and comparisons['combined']['half']['halfLogLoss']['dateCluster95CI'][1]<0
        and metrics['combined']['half']['halfAccuracy']>=base['half']['halfAccuracy']-.002)
    result={'protocol':protocol,'inputFixtures':len(source['matches']),'currentSeasonExamples':len(data_by['current']['ids']),
        'testFixtures':len(records),'halfTestFixtures':int(valid.sum()),'metrics':metrics,'comparisons':comparisons,
        'mainVersusMatchedControl':versus_control,'byLeague':by_league,'largeLeagueRegressions':regressions,
        'folds':reports,'gates':{'main':bool(main_gate),'timing':bool(timing_gate),'combined':bool(combined_gate)},
        'fullDataRefit':{},'activationPerformed':False,'datasetSHA256':hashlib.sha256((out/'dataset.json').read_bytes()).hexdigest()}
    save(out/'results.json',result);save(out/'test-predictions.json',records)
    # Once all tests are sealed, refit approved components only; never select on test labels.
    cutoff=f.utc(source['asOf']);data=data_by['multi'];all_indices=list(range(len(data['ids'])))
    if main_gate:
        fitted=fit_main(data,all_indices,cutoff,artifacts['selected']['multi'],artifacts['linearColumns'],artifacts['treeColumns'])
        payload={**fitted,'kind':'multiseason_rate_ensemble_v1','trainingCutoff':source['asOf'],'trainingExamples':len(all_indices),
                 'featureNames':data['names'],'history':data['stateDump'],'trainingScope':'three seasons, recent labels weighted, season-aware inputs',
                 'validationReportSHA256':hashlib.sha256((out/'results.json').read_bytes()).hexdigest()}
        save(out/'full-data-main-model.json',payload);result['fullDataRefit']['main']={'examples':len(all_indices),'artifact':'full-data-main-model.json'}
    if timing_gate:
        fitted=fit_timing(data,all_indices,cutoff,artifacts['selected']['timing'])
        payload={**fitted,'trainingCutoff':source['asOf'],'trainingExamples':len(all_indices),'history':data['stateDump'],
                 'validationReportSHA256':hashlib.sha256((out/'results.json').read_bytes()).hexdigest()}
        save(out/'full-data-timing-model.json',payload);result['fullDataRefit']['timing']={'examples':len(all_indices),'artifact':'full-data-timing-model.json'}
    save(out/'results.json',result)
    make_report(out,result)
    print('RESULT',json.dumps({'testFixtures':len(records),'metrics':metrics,'gates':result['gates'],'fullDataRefit':result['fullDataRefit']},allow_nan=False),flush=True)


def make_report(out,result):
    columns=('outcomeAccuracy','displayExactAccuracy','topThreeScoreAccuracy','scoreLogLoss','goalMAE')
    hcolumns=('halfAccuracy','firstHalfBTTSAccuracy','halfLogLoss','halfGoalMAE','firstHalfFavorites')
    chunks=['<!doctype html><meta charset="utf-8"><title>Three-season forecast validation</title>',
        '<style>body{font:16px system-ui;max-width:1250px;margin:40px auto;padding:0 20px;color:#17251e}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{padding:10px;text-align:right;border-bottom:1px solid #ddd}td:first-child,th:first-child{text-align:left}small{color:#52625a}</style>',
        '<h1>Three-season forecast validation</h1>',f'<p>{result["inputFixtures"]:,} historical fixtures collected; {result["testFixtures"]:,} paired test fixtures; {result["halfTestFixtures"]:,} with half-time labels.</p>',
        '<p>Nested chronological tuning; three weekly outer cutoffs. Full-match and timing changes evaluated independently and together. No live model was changed.</p>']
    for title,section,keys in [('Full-match accuracy and probability quality','main',columns),('Half-based forecasts','half',hcolumns)]:
        chunks.append('<h2>'+title+'</h2><table><tr><th>Version</th>'+''.join('<th>'+k+'</th>'for k in keys)+'</tr>')
        for name,metrics in result['metrics'].items():
            chunks.append('<tr><td>'+name+'</td>')
            for key in keys:
                value=metrics[section].get(key)
                formatted=str(value)if key=='firstHalfFavorites'else f'{100*value:.2f}%'if 'Accuracy'in key else f'{value:.5f}'
                chunks.append('<td>'+formatted+'</td>')
            chunks.append('</tr>')
        chunks.append('</table>')
    chunks.append('<h2>Quality gates</h2><pre>'+html.escape(json.dumps(result['gates'],indent=2))+'</pre>')
    chunks.append('<h2>Uncertainty and controls</h2><pre>'+html.escape(json.dumps({'comparisons':result['comparisons'],'mainVersusMatchedControl':result['mainVersusMatchedControl']},indent=2))+'</pre>')
    chunks.append('<h2>Scope and limitations</h2><p>'+html.escape(result['protocol']['limitations'])+'</p><p>Historical accuracy is not the unseen accuracy of a final full-data refit. Extra time and penalty-decided fixtures are excluded. Missing xG and pressure remain missing.</p>')
    (out/'report.html').write_text(''.join(chunks))

if __name__=='__main__':main()
