from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from api_module.database import get_db
from api_module.utilities import require_auth
from league_pool_module.router import LeaguePoolFilters
from .insights import enqueue_season, player_profile, request_summary, start_worker, stop_worker
from .access import restrict_insights
from .search import get_league_performance_options, search_league_performance
from .standings import StandingsError, get_league_standings

router = APIRouter(tags=['league-performance'], dependencies=[Depends(require_auth)])


class LeagueSearch(LeaguePoolFilters):
    limit: int = Field(default=100, ge=1, le=200)


class StandingsIn(BaseModel):
    leagueId: int = Field(gt=0)


class InsightsIn(StandingsIn):
    seasonId: int = Field(gt=0)
    periodStart: date | None = None


@router.on_event('startup')
def startup():
    start_worker()


@router.on_event('shutdown')
def shutdown():
    stop_worker()


@router.post('/league-performance/options')
def options(payload: LeaguePoolFilters, db: Session = Depends(get_db)):
    return get_league_performance_options(db, payload.leagues, payload.countries)


@router.post('/league-performance/search')
def search(payload: LeagueSearch, db: Session = Depends(get_db)):
    return search_league_performance(db, payload.leagues, payload.countries, payload.limit)


@router.post('/league-standings')
def standings(payload: StandingsIn, db: Session = Depends(get_db)):
    try:
        result = get_league_standings(payload.leagueId)
    except StandingsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (ValueError, TypeError):
        raise HTTPException(status_code=502, detail='League standings data is temporarily unavailable') from None
    rows = [row for table in result['tables'] for row in table['rows']]
    ids = list({row['teamId'] for row in rows if row['teamId']})
    images = dict(db.execute(text("""SELECT team_id, image_url FROM enterprise_team_images
        WHERE team_id=ANY(CAST(:ids AS bigint[])) AND image_status='available'"""), {'ids': ids}).all()) if ids else {}
    for row in rows:
        row['teamImageUrl'] = images.get(row['teamId']) or row['teamImageUrl']
    enqueue_season(db, result)
    return result


@router.post('/league-insights')
def insights(payload: InsightsIn, db: Session = Depends(get_db), user_id: int = Depends(require_auth)):
    try:
        plan = db.execute(text('SELECT plan FROM users WHERE id=:uid'), {'uid': user_id}).scalar()
        if payload.periodStart and plan not in {'No Ads Monthly', 'Pro Monthly', 'Pro Yearly'}:
            raise HTTPException(status_code=403, detail='Plus or Pro is required for two-week performance')
        return restrict_insights(request_summary(db, payload.leagueId, payload.seasonId, payload.periodStart), plan)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get('/league-insights/players/{player_id}')
def player(player_id: int, db: Session = Depends(get_db)):
    if player_id <= 0:
        raise HTTPException(status_code=422, detail='Invalid player ID')
    result = player_profile(db, player_id)
    if result is None:
        raise HTTPException(status_code=404, detail='This player is not yet available in the player pool')
    return result
