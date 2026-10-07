"""Mobile Pro workspace actions; reuse enterprise evidence and mobile credits."""
import hashlib
import json
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from api_module.database import get_db
from api_module.utilities import require_auth, is_user_pro, normalize_lang
from api_module.chat_trial import reserve_message, finish_message, refund_message
from matchup_module.comparison import _fetch_player_metadata
from mobile_pro_module.comparison_insights import ProComparisonInsightsIn, get_comparison_insights
from mobile_pro_module.team_fit import TeamFitIn, get_team_fit, target_metrics, metric_families
from mobile_pro_module.strategy_fit import StrategyFitIn, strategy_fit
from mobile_pro_module.league_fit import LeagueFitIn, LeagueFitInsightsIn, league_fit_data, league_fit_insights

from mobile_pro_module.league_metrics import build_league_categories
from mobile_pro_module.discovery import DiscoveryIn, discover_players, discovery_config
from mobile_pro_module.comparison_insights import DiscoveryAnalysisContext

router = APIRouter(prefix='/pro', tags=['Mobile Pro'])


class WorkspaceRequest(BaseModel):
    requestId: str = Field(min_length=1, max_length=128)
    sessionId: str = Field(min_length=1, max_length=128)


class InspectIn(WorkspaceRequest):
    discoveryContext: DiscoveryAnalysisContext | None = None
    playerId: int = Field(gt=0)
    strategy: str = Field(default='', max_length=50000)


class TeamRequest(TeamFitIn, WorkspaceRequest):
    pass


class StrategyRequest(StrategyFitIn, WorkspaceRequest):
    pass


class LeagueRequest(LeagueFitIn, WorkspaceRequest):
    pass


class DiscoveryRequest(DiscoveryIn, WorkspaceRequest):
    pass


@router.post('/discovery-config')
def discovery_options(user_id: int = Depends(require_auth)):
    return discovery_config()


@router.post('/discovery')
def discovery(payload: DiscoveryRequest, user_id: int = Depends(require_auth), db: Session = Depends(get_db), accept_language: str | None = Header(default=None)):
    return credited(db, user_id, 'discovery', payload, lambda: discover_players(db, user_id, payload, accept_language), language=accept_language)


def trial_analysis_identity(action, payload, language):
    """Identify the same analysis across workspaces without storing new data."""
    criteria = payload.model_dump(mode='json', exclude={'requestId', 'sessionId'})
    if action == 'inspect':
        # Inspection uses discovery context, not the workspace's strategy.
        criteria.pop('strategy', None)
    if action == 'discovery':
        # Multi-select order does not change the discovery criteria.
        for key in ('roles', 'excludedPlayerKeys'):
            criteria[key] = sorted(criteria.get(key, []))
        for key in ('nationality', 'team', 'league'):
            criteria['filters'][key] = sorted(criteria['filters'].get(key, []))
    identity = json.dumps({'version': 1, 'action': action,
                           'language': normalize_lang(language), 'criteria': criteria},
                          sort_keys=True, ensure_ascii=False)
    request_id = 'pro-analysis-v1:' + hashlib.sha256(identity.encode()).hexdigest()
    return request_id, identity


def credited(db, user_id, action, payload, run, language=None):
    """Charge once per trial analysis; completed results survive new workspaces."""
    if is_user_pro(db, user_id):
        return {**run(), 'freeChatMessagesRemaining': None}
    request_id, identity = trial_analysis_identity(action, payload, language)
    cached = reserve_message(db, user_id, request_id, identity, 'pro-analysis-v1', None)
    if cached is not None:
        return cached
    try:
        return finish_message(db, user_id, request_id, run())
    except BaseException:
        refund_message(db, user_id, request_id)
        raise


def inspect_player(db, payload, language):
    try:
        row = _fetch_player_metadata(db, str(payload.playerId))
    except ValueError as exc:
        raise HTTPException(404, 'Selected player not found') from exc
    metadata = dict(row['content'])
    image = db.execute(text("SELECT image_url FROM enterprise_player_images WHERE player_id = :id AND image_status = 'available' LIMIT 1"), {'id': metadata.get('player_id')}).scalar()
    if image:
        metadata['image_url'] = image
    values = target_metrics(metadata)
    groups = metric_families(values)
    categories = [{'key': key, 'metrics': [{'metric': name, 'values': [values[name]]} for name in names]}
                  for key, names in groups.items() if key in {'contribution_impact', 'goalkeeping', 'shooting', 'passing', 'defending', 'errors_discipline'} and names]
    insights = get_comparison_insights(db, ProComparisonInsightsIn(mode='single', playerIds=[payload.playerId], categories=categories, discoveryContext=payload.discoveryContext), language)['insights'] if categories else {}
    return {'row': {'id': row['id'], 'content': metadata}, 'stats': [{'metric': name, 'value': value} for name, value in values.items()], 'insights': insights, 'categories': categories}


@router.post('/inspect')
def inspect(payload: InspectIn, user_id: int = Depends(require_auth), db: Session = Depends(get_db), accept_language: str | None = Header(default=None)):
    return credited(db, user_id, 'inspect', payload, lambda: inspect_player(db, payload, accept_language), language=accept_language)


@router.post('/team-fit')
def team_fit(payload: TeamRequest, user_id: int = Depends(require_auth), db: Session = Depends(get_db), accept_language: str | None = Header(default=None)):
    return credited(db, user_id, 'team-fit', payload, lambda: get_team_fit(db, payload, accept_language), language=accept_language)


@router.post('/strategy-fit')
def strategy(payload: StrategyRequest, user_id: int = Depends(require_auth), db: Session = Depends(get_db), accept_language: str | None = Header(default=None)):
    return credited(db, user_id, 'strategy-fit', payload, lambda: strategy_fit(db, user_id, payload, accept_language), language=accept_language)


def assess_league(db, payload, language):
    context = league_fit_data(db, payload)
    candidate = _fetch_player_metadata(db, str(payload.playerId))['content']
    categories = build_league_categories(candidate, context['league']['content'].get('stats', {}))
    if not categories:
        raise HTTPException(422, 'Comparable league metrics unavailable')
    result = league_fit_insights(db, LeagueFitInsightsIn(**payload.model_dump(), categories=categories), language, context=context)
    return {**result, 'league': context['league'], 'roles': context['roles']}


@router.post('/league-fit')
def league(payload: LeagueRequest, user_id: int = Depends(require_auth), db: Session = Depends(get_db), accept_language: str | None = Header(default=None)):
    return credited(db, user_id, 'league-fit', payload, lambda: assess_league(db, payload, accept_language), language=accept_language)
