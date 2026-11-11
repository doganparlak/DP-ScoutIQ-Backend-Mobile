from __future__ import annotations

from typing import Any, Dict, List
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from player_pool_module.utilities import player_pool_table


DEFAULT_LIMIT = 10
logger = logging.getLogger(__name__)


def record_player_search(db: Session, player_id: str, world_cup_mode: bool = False) -> None:
    player_id_int = int(player_id)
    if world_cup_mode:
        # World Cup search tracking has been retired.
        return

    table_name = player_pool_table(False)
    player = db.execute(
        text(f"""
        SELECT
            metadata->>'player_name' AS player_name,
            NULLIF(metadata->>'player_id', '')::bigint AS metadata_player_id
        FROM {table_name}
        WHERE id = :player_id
        LIMIT 1
        """),
        {"player_id": player_id_int},
    ).mappings().first()
    if not player or player.get("metadata_player_id") is None:
        logger.warning(
            "Skipping weekly popular search hit without metadata.player_id: player_data_id=%s",
            player_id_int,
        )
        return

    player_name = player["player_name"]
    stable_player_id = int(player["metadata_player_id"])

    logger.info(
        "Recording weekly popular player search hit: player_data_id=%s metadata_player_id=%s player_name=%s",
        player_id_int,
        stable_player_id,
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
        {"player_id": stable_player_id},
    )


def get_weekly_popular_players(db: Session, limit: int = DEFAULT_LIMIT, world_cup_mode: bool = False) -> List[Dict[str, Any]]:
    if world_cup_mode:
        # Popularity rankings are unavailable without World Cup tracking.
        return []

    table_name = player_pool_table(False)
    rows = db.execute(
        text(f"""
        SELECT
            current_pd.id,
            current_pd.metadata AS content,
            epi.image_url
        FROM player_pool_weekly_searches pws
        JOIN LATERAL (
            SELECT pd.id, pd.metadata
            FROM {table_name} pd
            WHERE (CASE WHEN (pd.metadata->>'player_id') ~ '^[0-9]+([.]0+)?$'
                        THEN trunc((pd.metadata->>'player_id')::numeric)::text
                        ELSE NULL END) = pws.player_id::text
            ORDER BY pd.id DESC
            LIMIT 1
        ) current_pd ON TRUE
        LEFT JOIN enterprise_player_images epi
          ON epi.player_id = pws.player_id
         AND epi.image_status = 'available'
        WHERE pws.week_start = DATE_TRUNC('week', NOW())::date
        ORDER BY pws.search_count DESC, pws.last_searched_at DESC, current_pd.id DESC
        LIMIT :limit
        """),
        {"limit": int(limit or DEFAULT_LIMIT)},
    ).mappings().all()

    return [
        {
            "id": row["id"],
            "content": {
                **dict(row["content"] or {}),
                **({"image_url": str(row["image_url"])} if row.get("image_url") else {}),
            },
        }
        for row in rows
    ]


def record_weekly_popular_reveal(db: Session, user_id: int, world_cup_mode: bool = False) -> None:
    if world_cup_mode:
        return

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
