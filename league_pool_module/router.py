from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from api_module.database import get_db
from api_module.utilities import require_auth
from league_pool_module.league_pool import get_league_pool_options, search_league_pool

router = APIRouter(prefix="/league-pool", tags=["league-pool"])

class LeaguePoolFilters(BaseModel):
    leagues: List[str] = Field(default_factory=list)
    countries: List[str] = Field(default_factory=list)

class LeaguePoolSearch(LeaguePoolFilters):
    positions: List[str] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=200)

class LeaguePoolOptions(BaseModel):
    leagues: List[str]
    countries: List[str]
    positions: List[str]

class LeaguePoolRow(BaseModel):
    id: str
    content: Dict[str, Any]

@router.post("/options", response_model=LeaguePoolOptions)
def options(payload: LeaguePoolFilters, user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    return get_league_pool_options(db, payload.leagues, payload.countries)

@router.post("/search", response_model=List[LeaguePoolRow])
def search(payload: LeaguePoolSearch, user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    return search_league_pool(db, payload.model_dump())
