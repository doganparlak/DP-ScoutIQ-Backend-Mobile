"""Season-aware offline forecast challenger. No live route imports this module."""
from datetime import timedelta
import math
import numpy as np
from . import forecast as f, forecast_candidates as c, forecast_learning as l
from .forecast_history import daily_boundary
from .forecast_league_offsets import AssociationPrior, LeagueOffsetEstimator, COUNTRY_ALIASES, INTERNATIONAL_COMPETITIONS, apply_elo_gap
from . import forecast_timing as timing


class SeasonHistory(c.History):
    """Reset league baselines at season boundaries; retain recent cross-league form."""
    def __init__(self):
        super().__init__();self.seasons={};self.last_played={}

    def prepare(self,match):
        kickoff=f.utc(match['at']);league=match['league'];season=match['season']
        if self.seasons.get(league) not in (None,season):
            self.totals.pop(league,None);self.half_totals.pop(league,None)
        self.seasons[league]=season
        for team in (match['home'],match['away']):
            self.histories[team]=[r for r in self.histories[team] if kickoff-f.utc(r['at'])<=timedelta(days=120)]

    def consume(self,match):
        if match['id'] in self.seen:return
        league=match['league'];season=match['season'];kickoff=f.utc(match['at'])
        if self.seasons.get(league) not in (None,season):
            self.totals.pop(league,None);self.half_totals.pop(league,None)
        self.seasons[league]=season
        for team in (match['home'],match['away']):
            self.histories[team]=[r for r in self.histories[team] if kickoff-f.utc(r['at'])<=timedelta(days=120)]
            last=self.last_played.get(team)
            if last and kickoff-last>timedelta(days=90):
                self.ratings[team]=1500.+.5*(self.ratings.get(team,1500.)-1500.)
                self.strengths[team]=tuple(.5*v for v in self.strengths.get(team,(0.,0.)))
            self.last_played[team]=kickoff
        super().consume(match)

    def dump(self):
        return {**super().dump(),'seasonAware':True,'seasons':self.seasons,
                'lastPlayed':{str(k):v.isoformat()for k,v in self.last_played.items()}}


def half_features(state,match):
    own,opp=state.histories[match['home']][-10:],state.histories[match['away']][-10:]
    prior=[f._half_prior(state.half_totals,match['league'],home)for home in (True,False)]
    x=[timing.features(own,opp,True,prior[0]),timing.features(opp,own,False,prior[1])]
    shares=[]
    for strength in (5.,15.,30.,60.,None):
        shares.append([f._half_share(own,opp,prior[0],strength),f._half_share(opp,own,prior[1],strength)])
    return x,prior,shares


def make_examples(matches,strategy,competitions,prior_payload,season_aware=False):
    """All inputs use only labels settled by the last 04:00 UTC refresh."""
    state=SeasonHistory()if season_aware else c.History()
    domestic={r['leagueId']:COUNTRY_ALIASES.get(r.get('country'),r.get('country'))for r in competitions if r['leagueId']not in INTERNATIONAL_COMPETITIONS}
    countries=sorted(set(domestic.values())&set(prior_payload['coefficients']))
    prior=AssociationPrior({k:prior_payload['coefficients'][k]for k in countries},prior_payload['availableAt'],prior_payload['source'])
    estimator=LeagueOffsetEstimator(prior);associations={};observations=[];last_boundary=None
    raw=[];goals=[];first=[];times=[];leagues=[];ids=[];halfx=[];halfprior=[];halfshares=[];cards=[];pending=[];index=0
    for match in sorted(matches,key=lambda m:(f.utc(m['at']),m['id'])):
        kickoff=f.utc(match['at']);boundary=daily_boundary(kickoff)
        while index<len(pending)and f.utc(pending[index]['at'])+timedelta(hours=3)<=boundary:
            prev=pending[index];index+=1
            country=domestic.get(prev['league'])
            if country:
                for team in (prev['home'],prev['away']):associations[team]=country
            a,b=associations.get(prev['home']),associations.get(prev['away'])
            # The 2025 coefficient prior must never enter pre-publication examples.
            if prev['league']in INTERNATIONAL_COMPETITIONS and a!=b and prior.contrast(a,b)is not None and f.utc(prev['at'])>f.utc(prior.available_at):
                observations.append({'homeAssociation':a,'awayAssociation':b,
                    'eloDifference':state.ratings.get(prev['home'],1500.)-state.ratings.get(prev['away'],1500.),
                    'result':1. if prev['hg']>prev['ag']else .5 if prev['hg']==prev['ag']else 0.})
            state.consume(prev)
        if boundary!=last_boundary:
            if kickoff>f.utc(prior.available_at):estimator.fit(observations)
            last_boundary=boundary
        if season_aware:state.prepare(match)
        own,opp=state.histories[match['home']],state.histories[match['away']]
        if min(len(own),len(opp))>=f.MIN_HISTORY:
            pair=np.asarray([c.features(state,match['home'],match['away'],True,match['league'],match['at'],strategy),
                             c.features(state,match['away'],match['home'],False,match['league'],match['at'],strategy)],float)
            gap=estimator.gap(associations.get(match['home']),associations.get(match['away']))if match['league']in INTERNATIONAL_COMPETITIONS and kickoff>f.utc(prior.available_at)else 0.
            names,_=c.feature_names(strategy);pair=apply_elo_gap(pair,names,[gap]);raw.extend(pair)
            hx,hp,hs=half_features(state,match);halfx.extend(hx);halfprior.extend(hp);halfshares.append(hs)
            goals.extend([match['hg'],match['ag']]);first.extend([match.get('hhg'),match.get('hag')])
            times.append(match['at']);leagues.append(match['league']);ids.append(match['id'])
            exposure=max((match.get('cardContext')or{}).get('exposure',{}).values(),default=0.)
            cards.extend([1-.5*exposure]*2)
        pending.append(match)
    for match in pending[index:]:state.consume(match)
    names,blocks=c.feature_names(strategy)
    return {'raw':np.asarray(raw,float),'goals':np.asarray(goals,float),'first':np.asarray(first,float),
            'halfx':np.asarray(halfx,float),'halfprior':np.asarray(halfprior,float),'halfshares':np.asarray(halfshares,float),
            'stateDump':state.dump(),'times':times,'leagues':leagues,'ids':ids,'cardWeights':np.asarray(cards,float),'names':names,'blocks':blocks}


def weights(data,indices,cutoff,decay):
    league=l.league_weights([data['leagues'][i]for i in indices],.5)
    age=np.asarray([(cutoff-f.utc(data['times'][i])).total_seconds()/86400 for i in indices])
    if np.any(age<0):raise ValueError('Training label beyond cutoff')
    temporal=np.ones(len(age))if decay is None else 2.**(-age/decay)
    return np.repeat(league*temporal,2)


def fit_components(data,indices,cutoff,decay,linear_cols,tree_cols,penalty,tree_spec):
    from sklearn.ensemble import HistGradientBoostingRegressor
    positions=np.asarray([[2*i,2*i+1]for i in indices]).ravel();raw=data['raw'][positions];y=data['goals'][positions]
    w=weights(data,indices,cutoff,decay)
    x,prep=c._matrix(raw,linear_cols,w)
    linear={'columns':linear_cols,**prep,'beta':f._fit(x,y,penalty,w).tolist()}
    depth,iterations,leaf,l2=tree_spec
    estimator=HistGradientBoostingRegressor(loss='poisson',max_depth=depth,max_leaf_nodes=2**depth,
        max_iter=iterations,min_samples_leaf=leaf,l2_regularization=l2,learning_rate=.05,max_bins=64,
        early_stopping=False,categorical_features=None,random_state=20261104)
    estimator.fit(raw[:,tree_cols],y,sample_weight=w*data['cardWeights'][positions])
    tree={'columns':tree_cols,'hyperparameters':{'depth':depth,'trees':iterations,'minLeaf':leaf,'l2':l2},**c.export_trees(estimator)}
    # Assert exported, dependency-light inference matches training implementation.
    sample=raw[-min(100,len(raw)):]
    np.testing.assert_allclose(c.tree_predict(tree,sample),np.clip(estimator.predict(sample[:,tree_cols]),.05,8.),rtol=1e-10,atol=1e-10)
    return linear,tree


def predict_components(linear,tree,raw,blend):
    return (1-blend)*c.linear_predict(linear,raw)+blend*c.tree_predict(tree,raw)


def build_serving_offsets(matches,competitions,prior_payload,cutoff):
    """Recreate the final season-aware Elo state and its frozen league offsets."""
    from .forecast_league_offsets import observe_match, refresh_offsets
    domestic={str(r['leagueId']):COUNTRY_ALIASES.get(r.get('country'),r.get('country'))for r in competitions if r['leagueId']not in INTERNATIONAL_COMPETITIONS}
    countries=sorted(set(domestic.values())&set(prior_payload['coefficients']))
    component={'kind':'residual_league_elo_offset_v1','teamAssociations':{},'competitionAssociations':domestic,
        'internationalCompetitionIds':list(INTERNATIONAL_COMPETITIONS),
        'prior':{'coefficients':{k:prior_payload['coefficients'][k]for k in countries},'available_at':prior_payload['availableAt'],'source':prior_payload['source']},
        'priorPenalty':5.,'deviationPenalty':20.,'observations':[]}
    state=SeasonHistory()
    for match in sorted(matches,key=lambda m:(f.utc(m['at']),m['id'])):
        if f.utc(match['at'])+timedelta(hours=3)>cutoff:raise ValueError('Unsettled fixture in serving state')
        country=domestic.get(str(match['league']))
        if country:
            for team in (match['home'],match['away']):component['teamAssociations'][str(team)]=country
        if f.utc(match['at'])>f.utc(prior_payload['availableAt']):observe_match(component,match,state.ratings)
        state.consume(match)
    return refresh_offsets(component,cutoff),state
