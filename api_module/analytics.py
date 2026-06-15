from __future__ import annotations

from typing import Any, Dict
import json
import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api_module.database import SessionLocal
from player_pool_module.utilities import player_pool_table


logger = logging.getLogger(__name__)


def analytics_mode(world_cup_mode: bool | None = False) -> str:
    return "world_cup" if bool(world_cup_mode) else "club"


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _metadata_value(metadata: Dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def get_player_snapshot(db: Session, player_id: int | str | None, world_cup_mode: bool = False) -> Dict[str, Any]:
    if player_id is None:
        return {}

    try:
        player_id_int = int(player_id)
    except (TypeError, ValueError):
        return {"player_id": str(player_id)}

    table_name = player_pool_table(world_cup_mode)
    row = db.execute(
        text(f"""
            SELECT id, metadata
            FROM {table_name}
            WHERE id = :player_id
            LIMIT 1
        """),
        {"player_id": player_id_int},
    ).mappings().first()
    if not row:
        return {"player_id": str(player_id)}

    metadata = dict(row["metadata"] or {})
    return {
        "player_id": str(row["id"]),
        "player_name": _metadata_value(metadata, "player_name", "name"),
        "player_team": _metadata_value(metadata, "team_name", "team"),
        "player_league": _metadata_value(metadata, "league_name", "league"),
        "player_nationality": _metadata_value(metadata, "nationality_name", "nationality"),
    }


def get_favorite_player_snapshot(
    db: Session,
    favorite_id: str | None,
    user_id: int | None = None,
) -> Dict[str, Any]:
    if not favorite_id:
        return {}

    params: Dict[str, Any] = {"favorite_id": favorite_id}
    user_filter = ""
    if user_id is not None:
        user_filter = "AND user_id = :user_id"
        params["user_id"] = int(user_id)

    row = db.execute(
        text(f"""
            SELECT id, name, team, league, nationality
            FROM favorite_players
            WHERE id = :favorite_id
            {user_filter}
            LIMIT 1
        """),
        params,
    ).mappings().first()
    if not row:
        return {"favorite_player_id": favorite_id}

    return {
        "favorite_player_id": row["id"],
        "player_name": row["name"],
        "player_team": row["team"],
        "player_league": row["league"],
        "player_nationality": row["nationality"],
    }


def record_analytics_event(
    *,
    user_id: int | None,
    event_type: str,
    section: str = "player_pool",
    mode: str = "club",
    source: str | None = None,
    player_table: str | None = None,
    player_id: str | int | None = None,
    player_name: str | None = None,
    player_team: str | None = None,
    player_league: str | None = None,
    player_nationality: str | None = None,
    secondary_player_id: str | int | None = None,
    secondary_player_name: str | None = None,
    secondary_player_team: str | None = None,
    secondary_player_league: str | None = None,
    secondary_player_nationality: str | None = None,
    score_kind: str | None = None,
    score_value: int | None = None,
    score_source: str | None = None,
    report_id: str | None = None,
    favorite_player_id: str | None = None,
    search_filters: Dict[str, Any] | None = None,
    result_count: int | None = None,
    metadata: Dict[str, Any] | None = None,
) -> None:
    """
    Best-effort analytics write. Analytics must never break product flows, so it
    uses its own short session and swallows database errors after logging them.
    """
    db = SessionLocal()
    try:
        user_email = None
        if user_id is not None:
            user = db.execute(
                text("SELECT email FROM users WHERE id = :user_id"),
                {"user_id": int(user_id)},
            ).mappings().first()
            user_email = user["email"] if user else None

        db.execute(
            text("""
                INSERT INTO app_user_activity_events (
                    user_id,
                    user_email,
                    event_type,
                    section,
                    mode,
                    source,
                    player_table,
                    player_id,
                    player_name,
                    player_team,
                    player_league,
                    player_nationality,
                    secondary_player_id,
                    secondary_player_name,
                    secondary_player_team,
                    secondary_player_league,
                    secondary_player_nationality,
                    score_kind,
                    score_value,
                    score_source,
                    report_id,
                    favorite_player_id,
                    search_filters,
                    result_count,
                    metadata
                )
                VALUES (
                    :user_id,
                    :user_email,
                    :event_type,
                    :section,
                    :mode,
                    :source,
                    :player_table,
                    :player_id,
                    :player_name,
                    :player_team,
                    :player_league,
                    :player_nationality,
                    :secondary_player_id,
                    :secondary_player_name,
                    :secondary_player_team,
                    :secondary_player_league,
                    :secondary_player_nationality,
                    :score_kind,
                    :score_value,
                    :score_source,
                    :report_id,
                    :favorite_player_id,
                    CAST(:search_filters AS jsonb),
                    :result_count,
                    CAST(:metadata AS jsonb)
                )
            """),
            {
                "user_id": int(user_id) if user_id is not None else None,
                "user_email": user_email,
                "event_type": event_type,
                "section": section,
                "mode": mode,
                "source": source,
                "player_table": player_table,
                "player_id": str(player_id) if player_id is not None else None,
                "player_name": player_name,
                "player_team": player_team,
                "player_league": player_league,
                "player_nationality": player_nationality,
                "secondary_player_id": str(secondary_player_id) if secondary_player_id is not None else None,
                "secondary_player_name": secondary_player_name,
                "secondary_player_team": secondary_player_team,
                "secondary_player_league": secondary_player_league,
                "secondary_player_nationality": secondary_player_nationality,
                "score_kind": score_kind,
                "score_value": int(score_value) if score_value is not None else None,
                "score_source": score_source,
                "report_id": report_id,
                "favorite_player_id": favorite_player_id,
                "search_filters": _json_dumps(search_filters),
                "result_count": int(result_count) if result_count is not None else None,
                "metadata": _json_dumps(metadata),
            },
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("analytics_event_write_failed event_type=%s error=%s", event_type, exc)
    finally:
        db.close()
