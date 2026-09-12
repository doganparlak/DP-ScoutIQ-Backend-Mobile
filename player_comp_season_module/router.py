from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from api_module.database import get_db
from api_module.utilities import require_auth
from .models import *
from .season_data import search_season_players, get_season_player_nationalities, get_player_season_rows, aggregate_player_seasons

router = APIRouter(prefix="/player-comp-season", tags=["season-data"])

@router.post(
    "/search",
    response_model=list[PlayerCompSeasonCandidateOut],
)
def player_comp_season_search(
    payload: PlayerCompSeasonSearchIn,
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    del user_id
    return search_season_players(
        db,
        payload.query,
        payload.limit,
        payload.nationality,
    )


@router.get(
    "/options",
    response_model=PlayerCompSeasonOptionsOut,
)
def player_comp_season_options(
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    del user_id
    return {"nationalities": get_season_player_nationalities(db)}


@router.get(
    "/players/{player_id}/rows",
    response_model=PlayerCompSeasonRowsOut,
)
def player_comp_season_rows(
    player_id: int,
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    del user_id
    try:
        return get_player_season_rows(db, player_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/aggregate",
    response_model=PlayerCompSeasonAggregateOut,
)
def player_comp_season_aggregate(
    payload: PlayerCompSeasonAggregateIn,
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    del user_id
    try:
        return aggregate_player_seasons(
            db,
            payload.playerId,
            [source.model_dump() for source in payload.sources],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


