from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from api_module.database import get_db
from api_module.utilities import require_auth
from matchup_module.comparison import get_player_comparison_sources, _fetch_player_metadata, _selected_comp_metadata, _fetch_player_by_sportmonks_id

router = APIRouter(prefix="/matchup/players", tags=["matchup"])

class SourceSelection(BaseModel):
    sportmonksId: int | None = Field(default=None, gt=0)
    sources: List[str] = Field(default_factory=list, max_length=100)

@router.get("/{player_id}/sources")
def sources(player_id: str, sportmonksId: int | None = Query(default=None, gt=0), user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        return get_player_comparison_sources(db, player_id, sportmonksId)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

@router.post("/{player_id}/data")
def data(player_id: str, payload: SourceSelection, user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        player = _fetch_player_by_sportmonks_id(db, payload.sportmonksId) if payload.sportmonksId is not None else _fetch_player_metadata(db, player_id)
        if payload.sources:
            player["content"] = _selected_comp_metadata(db, str(player["id"]), payload.sources, player["content"], payload.sportmonksId)
        return player
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
