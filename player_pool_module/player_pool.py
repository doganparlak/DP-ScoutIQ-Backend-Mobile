from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.orm import Session

from potential_form_module.form import reveal_player_form
from potential_form_module.potential import reveal_player_potential
from report_module.utilities import norm_name
from player_pool_module.utilities import (
    clean_str,
    folded_text_sql,
    numeric_filter_sql,
    player_pool_table,
)


SEARCH_LIMIT = 100


def search_players(db: Session, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    table_name = player_pool_table(bool(filters.get("worldCupMode")))
    name = clean_str(filters.get("name"))
    gender = clean_str(filters.get("gender"))
    nationality = None if filters.get("worldCupMode") else clean_str(filters.get("nationality"))
    league = None if filters.get("worldCupMode") else clean_str(filters.get("league"))
    team = clean_str(filters.get("team"))
    position = clean_str(filters.get("position"))
    name_norm = norm_name(name) if name else None
    team_norm = norm_name(team) if team else None
    league_norm = norm_name(league) if league else None
    nationality_norm = norm_name(nationality) if nationality else None
    position_norm = norm_name(position) if position else None

    query = text(f"""
        SELECT
            id,
            metadata AS content
        FROM {table_name}
        WHERE (
                :name_q IS NULL
                OR metadata->>'player_name' ILIKE :name_q
                OR metadata->>'player_name_norm' ILIKE :name_norm_q
                OR {folded_text_sql("player_name")} LIKE :name_folded_q
              )
          AND (:gender IS NULL OR LOWER(COALESCE(metadata->>'gender', '')) = LOWER(:gender))
          AND (
                :nationality IS NULL
                OR LOWER(COALESCE(metadata->>'nationality_name', '')) = LOWER(:nationality)
                OR {folded_text_sql("nationality_name")} = :nationality_folded
              )
          AND (
                :league IS NULL
                OR LOWER(COALESCE(metadata->>'league_name', '')) = LOWER(:league)
                OR LOWER(COALESCE(metadata->>'league_name_norm', '')) = LOWER(:league_norm)
                OR {folded_text_sql("league_name")} = :league_folded
              )
          AND (
                :team IS NULL
                OR LOWER(COALESCE(metadata->>'team_name', '')) = LOWER(:team)
                OR LOWER(COALESCE(metadata->>'team_name_norm', '')) = LOWER(:team_norm)
                OR {folded_text_sql("team_name")} = :team_folded
              )
          AND (
                :position_q IS NULL
                OR metadata->>'position_name' ILIKE :position_q
                OR {folded_text_sql("position_name")} LIKE :position_folded_q
              )
          AND {numeric_filter_sql("age", "min_age", ">=")}
          AND {numeric_filter_sql("age", "max_age", "<=")}
          AND {numeric_filter_sql("height", "min_height", ">=")}
          AND {numeric_filter_sql("height", "max_height", "<=")}
          AND {numeric_filter_sql("weight", "min_weight", ">=")}
          AND {numeric_filter_sql("weight", "max_weight", "<=")}
        ORDER BY
            COALESCE(metadata->>'player_name', ''),
            COALESCE(metadata->>'team_name', ''),
            id DESC
        LIMIT :limit
    """)

    rows = db.execute(
        query,
        {
            "name_q": f"%{name}%" if name else None,
            "name_norm_q": f"%{name_norm}%" if name_norm else None,
            "name_folded_q": f"%{name_norm}%" if name_norm else None,
            "gender": gender,
            "nationality": nationality,
            "nationality_folded": nationality_norm,
            "league": league,
            "league_norm": league_norm,
            "league_folded": league_norm,
            "team": team,
            "team_norm": team_norm,
            "team_folded": team_norm,
            "position_q": f"%{position}%" if position else None,
            "position_folded_q": f"%{position_norm}%" if position_norm else None,
            "min_age": filters.get("minAge"),
            "max_age": filters.get("maxAge"),
            "min_height": filters.get("minHeight"),
            "max_height": filters.get("maxHeight"),
            "min_weight": filters.get("minWeight"),
            "max_weight": filters.get("maxWeight"),
            "limit": int(filters.get("limit") or SEARCH_LIMIT),
        },
    ).mappings().all()

    return [{"id": row["id"], "content": row["content"] or {}} for row in rows]


def get_player_pool_filter_options(db: Session, world_cup_mode: bool = False) -> Dict[str, List[str]]:
    table_name = player_pool_table(world_cup_mode)
    teams = db.execute(text("""
        SELECT DISTINCT metadata->>'team_name' AS value
        FROM {table_name}
        WHERE COALESCE(metadata->>'team_name', '') <> ''
        ORDER BY value
    """.format(table_name=table_name))).scalars().all()

    nationalities = db.execute(text("""
        SELECT DISTINCT metadata->>'nationality_name' AS value
        FROM {table_name}
        WHERE COALESCE(metadata->>'nationality_name', '') <> ''
        ORDER BY value
    """.format(table_name=table_name))).scalars().all()

    leagues = db.execute(text("""
        SELECT DISTINCT metadata->>'league_name' AS value
        FROM {table_name}
        WHERE COALESCE(metadata->>'league_name', '') <> ''
        ORDER BY value
    """.format(table_name=table_name))).scalars().all()

    positions = db.execute(text("""
        SELECT DISTINCT metadata->>'position_name' AS value
        FROM {table_name}
        WHERE COALESCE(metadata->>'position_name', '') <> ''
        ORDER BY value
    """.format(table_name=table_name))).scalars().all()

    return {
        "teams": [value for value in teams if value],
        "leagues": [value for value in leagues if value],
        "nationalities": [value for value in nationalities if value],
        "positions": [value for value in positions if value],
    }
