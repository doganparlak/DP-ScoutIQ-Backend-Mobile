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
