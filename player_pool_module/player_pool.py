from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.orm import Session

from report_module.utilities import norm_name


SEARCH_LIMIT = 100
TURKISH_CHAR_MAP_FROM = "çğıöşüÇĞİÖŞÜIİı"
TURKISH_CHAR_MAP_TO = "cgiosuCGIOSUiii"


def _clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _numeric_filter_sql(field_name: str, param_name: str, operator: str) -> str:
    value_expr = f"""
    CASE
        WHEN COALESCE(metadata->>'{field_name}', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
            THEN (metadata->>'{field_name}')::numeric
        ELSE NULL
    END
    """
    return f"(:{param_name} IS NULL OR ({value_expr}) {operator} :{param_name})"


def _folded_text_sql(field_name: str) -> str:
    return f"LOWER(TRANSLATE(COALESCE(metadata->>'{field_name}', ''), '{TURKISH_CHAR_MAP_FROM}', '{TURKISH_CHAR_MAP_TO}'))"


def search_players(db: Session, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = _clean_str(filters.get("name"))
    gender = _clean_str(filters.get("gender"))
    nationality = _clean_str(filters.get("nationality"))
    team = _clean_str(filters.get("team"))
    position = _clean_str(filters.get("position"))
    name_norm = norm_name(name) if name else None
    team_norm = norm_name(team) if team else None
    nationality_norm = norm_name(nationality) if nationality else None
    position_norm = norm_name(position) if position else None

    query = text(f"""
        SELECT
            id,
            metadata AS content
        FROM player_data
        WHERE (
                :name_q IS NULL
                OR metadata->>'player_name' ILIKE :name_q
                OR metadata->>'player_name_norm' ILIKE :name_norm_q
                OR {_folded_text_sql("player_name")} LIKE :name_folded_q
              )
          AND (:gender IS NULL OR LOWER(COALESCE(metadata->>'gender', '')) = LOWER(:gender))
          AND (
                :nationality IS NULL
                OR LOWER(COALESCE(metadata->>'nationality_name', '')) = LOWER(:nationality)
                OR {_folded_text_sql("nationality_name")} = :nationality_folded
              )
          AND (
                :team IS NULL
                OR LOWER(COALESCE(metadata->>'team_name', '')) = LOWER(:team)
                OR LOWER(COALESCE(metadata->>'team_name_norm', '')) = LOWER(:team_norm)
                OR {_folded_text_sql("team_name")} = :team_folded
              )
          AND (
                :position_q IS NULL
                OR metadata->>'position_name' ILIKE :position_q
                OR {_folded_text_sql("position_name")} LIKE :position_folded_q
              )
          AND {_numeric_filter_sql("age", "min_age", ">=")}
          AND {_numeric_filter_sql("age", "max_age", "<=")}
          AND {_numeric_filter_sql("height", "min_height", ">=")}
          AND {_numeric_filter_sql("height", "max_height", "<=")}
          AND {_numeric_filter_sql("weight", "min_weight", ">=")}
          AND {_numeric_filter_sql("weight", "max_weight", "<=")}
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


def get_player_pool_filter_options(db: Session) -> Dict[str, List[str]]:
    teams = db.execute(text("""
        SELECT DISTINCT metadata->>'team_name' AS value
        FROM player_data
        WHERE COALESCE(metadata->>'team_name', '') <> ''
        ORDER BY value
    """)).scalars().all()

    nationalities = db.execute(text("""
        SELECT DISTINCT metadata->>'nationality_name' AS value
        FROM player_data
        WHERE COALESCE(metadata->>'nationality_name', '') <> ''
        ORDER BY value
    """)).scalars().all()

    positions = db.execute(text("""
        SELECT DISTINCT metadata->>'position_name' AS value
        FROM player_data
        WHERE COALESCE(metadata->>'position_name', '') <> ''
        ORDER BY value
    """)).scalars().all()

    return {
        "teams": [value for value in teams if value],
        "nationalities": [value for value in nationalities if value],
        "positions": [value for value in positions if value],
    }
