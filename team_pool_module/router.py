from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from api_module.database import get_db
from api_module.utilities import require_auth
from .models import MatchAnalysisOptionsIn, MatchAnalysisOptionsOut, TeamPoolSearchIn, TeamPoolSearchRow
from .team_pool import get_match_filter_options, search_team_pool, SportMonksError
import requests

router = APIRouter(prefix="/team-pool", tags=["team-pool"])

@router.post("/options", response_model=MatchAnalysisOptionsOut)
def options(payload: MatchAnalysisOptionsIn, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    return get_match_filter_options(db, payload.country, payload.league)

@router.post("/search", response_model=list[TeamPoolSearchRow])
def search(payload: TeamPoolSearchIn, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    try:
        return search_team_pool(db, payload.team, payload.country, payload.league, payload.limit)
    except SportMonksError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Team data service is temporarily unavailable") from None


@router.get("/teams/{team_id}", response_model=TeamPoolSearchRow)
def team_card(team_id: int, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    try:
        rows = search_team_pool(db, limit=1, team_id=team_id)
        if not rows:
            raise HTTPException(404, "Team not found")
        return rows[0]
    except (SportMonksError, requests.RequestException):
        raise HTTPException(502, "Team data service is temporarily unavailable") from None


@router.post("/selector-options")
def selector_options(payload: dict, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    from .current_leagues import load_current_teams
    teams = load_current_teams(db)
    leagues = {t['league_id']: {"id": t['league_id'], "name": t['league_name'], "country": t['league_country_name']}
               for t in teams if t['league_id'] is not None}
    league_id = payload.get('leagueId')
    return {"leagues": sorted(leagues.values(), key=lambda l: (l['country'] or '', l['name'])),
            "teams": [{"id": t['team_id'], "name": t['team_name'], "leagueId": t['league_id']}
                      for t in teams if league_id is None or t['league_id'] == league_id]}
