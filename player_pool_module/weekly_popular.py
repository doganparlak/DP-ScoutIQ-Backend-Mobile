from __future__ import annotations

from typing import Any, Dict, List
import hashlib
import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from player_pool_module.utilities import player_pool_table


DEFAULT_LIMIT = 10
logger = logging.getLogger(__name__)


def _metadata_value(metadata: Dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _world_cup_player_key(metadata: Dict[str, Any]) -> str:
    parts = [
        _metadata_value(metadata, "player_name").lower(),
        _metadata_value(metadata, "gender").lower(),
        _metadata_value(metadata, "age"),
        _metadata_value(metadata, "height"),
        _metadata_value(metadata, "weight"),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _record_world_cup_player_search(db: Session, player_id_int: int) -> None:
    player = db.execute(
        text("""
        SELECT id, metadata
        FROM player_data_wc
        WHERE id = :player_id
        LIMIT 1
        """),
        {"player_id": player_id_int},
    ).mappings().first()
    if not player:
        return

    metadata = dict(player["metadata"] or {})
    player_key = _world_cup_player_key(metadata)
    player_name = _metadata_value(metadata, "player_name")

    logger.info(
        "Recording World Cup top search hit: player_id=%s player_name=%s",
        player_id_int,
        player_name or "unknown",
    )

    db.execute(
        text("""
        INSERT INTO player_pool_world_cup_searches (
            player_key,
            current_player_id,
            player_name,
            gender,
            age,
            height,
            weight,
            team,
            player_metadata,
            search_count,
            last_searched_at
        )
        VALUES (
            :player_key,
            :current_player_id,
            :player_name,
            :gender,
            :age,
            :height,
            :weight,
            :team,
            CAST(:player_metadata AS jsonb),
            1,
            NOW()
        )
        ON CONFLICT (player_key) DO UPDATE
        SET current_player_id = EXCLUDED.current_player_id,
            player_name = EXCLUDED.player_name,
            gender = EXCLUDED.gender,
            age = EXCLUDED.age,
            height = EXCLUDED.height,
            weight = EXCLUDED.weight,
            team = EXCLUDED.team,
            player_metadata = EXCLUDED.player_metadata,
            search_count = player_pool_world_cup_searches.search_count + 1,
            last_searched_at = NOW()
        """),
        {
            "player_key": player_key,
            "current_player_id": player_id_int,
            "player_name": player_name,
            "gender": _metadata_value(metadata, "gender"),
            "age": _metadata_value(metadata, "age"),
            "height": _metadata_value(metadata, "height"),
            "weight": _metadata_value(metadata, "weight"),
            "team": _metadata_value(metadata, "team"),
            "player_metadata": json.dumps(metadata),
        },
    )


def record_player_search(db: Session, player_id: str, world_cup_mode: bool = False) -> None:
    player_id_int = int(player_id)
    if world_cup_mode:
        _record_world_cup_player_search(db, player_id_int)
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
        rows = db.execute(
            text("""
            SELECT
                COALESCE(pd.id::text, wc.current_player_id::text, wc.player_key) AS id,
                COALESCE(pd.metadata, wc.player_metadata) AS content
            FROM player_pool_world_cup_searches wc
            LEFT JOIN LATERAL (
                SELECT id, metadata
                FROM player_data_wc pd
                WHERE LOWER(TRIM(pd.metadata->>'player_name')) = LOWER(TRIM(wc.player_name))
                  AND COALESCE(TRIM(pd.metadata->>'gender'), '') = COALESCE(TRIM(wc.gender), '')
                  AND COALESCE(TRIM(pd.metadata->>'age'), '') = COALESCE(TRIM(wc.age), '')
                  AND COALESCE(TRIM(pd.metadata->>'height'), '') = COALESCE(TRIM(wc.height), '')
                  AND COALESCE(TRIM(pd.metadata->>'weight'), '') = COALESCE(TRIM(wc.weight), '')
                ORDER BY CASE WHEN pd.id = wc.current_player_id THEN 0 ELSE 1 END, pd.id DESC
                LIMIT 1
            ) pd ON TRUE
            ORDER BY wc.search_count DESC, wc.last_searched_at DESC, wc.player_name ASC
            LIMIT :limit
            """),
            {"limit": int(limit or DEFAULT_LIMIT)},
        ).mappings().all()

        return [{"id": row["id"], "content": row["content"] or {}} for row in rows]

    table_name = player_pool_table(False)
    rows = db.execute(
        text(f"""
        SELECT
            current_pd.id,
            current_pd.metadata AS content
        FROM player_pool_weekly_searches pws
        JOIN LATERAL (
            SELECT pd.id, pd.metadata
            FROM {table_name} pd
            WHERE NULLIF(pd.metadata->>'player_id', '')::bigint = pws.player_id
            ORDER BY pd.id DESC
            LIMIT 1
        ) current_pd ON TRUE
        WHERE pws.week_start = DATE_TRUNC('week', NOW())::date
        ORDER BY pws.search_count DESC, pws.last_searched_at DESC, current_pd.id DESC
        LIMIT :limit
        """),
        {"limit": int(limit or DEFAULT_LIMIT)},
    ).mappings().all()

    return [{"id": row["id"], "content": row["content"] or {}} for row in rows]


def record_weekly_popular_reveal(db: Session, user_id: int, world_cup_mode: bool = False) -> None:
    if world_cup_mode:
        db.execute(
            text("""
            INSERT INTO player_pool_world_cup_top_search_reveals (
                user_id,
                reveal_count,
                last_revealed_at
            )
            VALUES (:user_id, 1, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET reveal_count = player_pool_world_cup_top_search_reveals.reveal_count + 1,
                last_revealed_at = NOW()
            """),
            {"user_id": int(user_id)},
        )
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
