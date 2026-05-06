from __future__ import annotations

from typing import Any, Dict, List
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session


DEFAULT_LIMIT = 10
logger = logging.getLogger(__name__)


def record_player_search(db: Session, player_id: str) -> None:
    player_id_int = int(player_id)
    player = db.execute(
        text("""
        SELECT metadata->>'player_name' AS player_name
        FROM player_data
        WHERE id = :player_id
        LIMIT 1
        """),
        {"player_id": player_id_int},
    ).mappings().first()
    player_name = player["player_name"] if player else None

    logger.info(
        "Recording weekly popular player search hit: player_id=%s player_name=%s",
        player_id_int,
        player_name or "unknown",
    )

    db.execute(
        text("""
        INSERT INTO player_pool_weekly_searches (
            week_start,
            player_id,
            search_count,
            last_searched_at
        )
        VALUES (DATE_TRUNC('week', NOW())::date, :player_id, 1, NOW())
        ON CONFLICT (week_start, player_id) DO UPDATE
        SET search_count = player_pool_weekly_searches.search_count + 1,
            last_searched_at = NOW()
        """),
        {"player_id": player_id_int},
    )


def get_weekly_popular_players(db: Session, limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    rows = db.execute(
        text("""
        SELECT
            pd.id,
            pd.metadata AS content
        FROM player_pool_weekly_searches pws
        JOIN player_data pd ON pd.id = pws.player_id
        WHERE pws.week_start = DATE_TRUNC('week', NOW())::date
        ORDER BY pws.search_count DESC, pws.last_searched_at DESC, pd.id DESC
        LIMIT :limit
        """),
        {"limit": int(limit or DEFAULT_LIMIT)},
    ).mappings().all()

    return [{"id": row["id"], "content": row["content"] or {}} for row in rows]


def record_weekly_popular_reveal(db: Session, user_id: int) -> None:
    db.execute(
        text("""
        INSERT INTO player_pool_weekly_popular_reveals (
            week_start,
            user_id,
            reveal_count,
            last_revealed_at
        )
        VALUES (DATE_TRUNC('week', NOW())::date, :user_id, 1, NOW())
        ON CONFLICT (week_start, user_id) DO UPDATE
        SET reveal_count = player_pool_weekly_popular_reveals.reveal_count + 1,
            last_revealed_at = NOW()
        """),
        {"user_id": int(user_id)},
    )
