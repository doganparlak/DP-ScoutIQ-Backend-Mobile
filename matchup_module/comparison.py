from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import text
from sqlalchemy.orm import Session

from player_pool_module.utilities import player_pool_table


def _fetch_player_metadata(db: Session, player_id: str, world_cup_mode: bool = False) -> Dict[str, Any]:
    try:
        player_id_int = int(player_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid player_id") from exc

    table_name = player_pool_table(world_cup_mode)
    row = db.execute(
        text(f"""
        SELECT id, metadata AS content
        FROM {table_name}
        WHERE id = :player_id
        LIMIT 1
        """),
        {"player_id": player_id_int},
    ).mappings().first()

    if not row:
        raise ValueError(f"Player not found: {player_id}")

    return {
        "id": row["id"],
        "content": row["content"] or {},
    }


def get_matchup_comparison(db: Session, player1_id: str, player2_id: str, world_cup_mode: bool = False) -> Dict[str, Any]:
    return {
        "player1": _fetch_player_metadata(db, player1_id, world_cup_mode),
        "player2": _fetch_player_metadata(db, player2_id, world_cup_mode),
    }
