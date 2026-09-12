from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.orm import Session

from potential_form_module.form import reveal_player_form
from potential_form_module.potential import reveal_player_potential
from player_pool_module.utilities import (
    ROLE_SEARCH_SHORT_ALIASES,
    ROLE_SHORT_TO_LONG,
    SEARCH_LIMIT,
    clean_str,
    folded_text_sql,
    norm_name,
    numeric_filter_sql,
    player_pool_table,
    role_short_sql,
    role_value_short_sql,
)


def _sportmonks_id(value: Any) -> int | None:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number != number.to_integral_value():
            return None
        return int(number)
    except (InvalidOperation, ValueError, TypeError):
        return None


def search_players(db: Session, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    world_cup_mode = bool(filters.get("worldCupMode"))
    table_name = player_pool_table(world_cup_mode)
    name = clean_str(filters.get("name"))
    gender = clean_str(filters.get("gender"))
    contract_status = None if world_cup_mode else clean_str(filters.get("contractStatus"))
    loan_end_date = None if world_cup_mode else clean_str(filters.get("loanEndDate"))
    contract_end_date = None if world_cup_mode else clean_str(filters.get("contractEndDate"))
    nationality = None if world_cup_mode else clean_str(filters.get("nationality"))
    league = None if world_cup_mode else clean_str(filters.get("league"))
    team = clean_str(filters.get("team"))
    position = clean_str(filters.get("position"))
    position_short = position.upper() if position and position.upper() in ROLE_SHORT_TO_LONG else None
    position_short = ROLE_SEARCH_SHORT_ALIASES.get(position_short, position_short)
    position_search = None if position_short else position
    name_norm = norm_name(name) if name else None
    team_norm = norm_name(team) if team else None
    league_norm = norm_name(league) if league else None
    nationality_norm = norm_name(nationality) if nationality else None
    position_norm = norm_name(position_search) if position_search else None

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
                OR {folded_text_sql("nationality_name")} LIKE :nationality_folded_q
              )
          AND (
                :league IS NULL
                OR LOWER(COALESCE(metadata->>'league_name', '')) = LOWER(:league)
                OR LOWER(COALESCE(metadata->>'league_name_norm', '')) ILIKE :league_norm_q
                OR {folded_text_sql("league_name")} LIKE :league_folded_q
              )
          AND (
                :team IS NULL
                OR LOWER(COALESCE(metadata->>'team_name', '')) = LOWER(:team)
                OR LOWER(COALESCE(metadata->>'team_name_norm', '')) ILIKE :team_norm_q
                OR {folded_text_sql("team_name")} LIKE :team_folded_q
              )
          AND (
                :position_filter IS NULL
                OR (:position_short IS NOT NULL AND {role_short_sql()} = :position_short)
                OR (
                    :position_short IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(
                            CASE
                                WHEN jsonb_typeof(metadata->'position_names_seen') = 'array'
                                THEN metadata->'position_names_seen'
                                ELSE '[]'::jsonb
                            END
                        ) AS seen_position(value)
                        WHERE {role_value_short_sql("seen_position.value")} = :position_short
                    )
                )
                OR (
                    :position_short IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                        FROM jsonb_object_keys(
                            CASE
                                WHEN jsonb_typeof(metadata->'position_counts') = 'object'
                                THEN metadata->'position_counts'
                                ELSE '{{}}'::jsonb
                            END
                        ) AS counted_position(value)
                        WHERE {role_value_short_sql("counted_position.value")} = :position_short
                    )
                )
                OR metadata->>'position_name' ILIKE :position_q
                OR {folded_text_sql("position_name")} LIKE :position_folded_q
              )
          AND {numeric_filter_sql("age", "min_age", ">=")}
          AND {numeric_filter_sql("age", "max_age", "<=")}
          AND {numeric_filter_sql("height", "min_height", ">=")}
          AND {numeric_filter_sql("height", "max_height", "<=")}
          AND {numeric_filter_sql("weight", "min_weight", ">=")}
          AND {numeric_filter_sql("weight", "max_weight", "<=")}
          AND (
                :contract_status IS NULL
                OR (
                    :contract_status = 'loan'
                    AND LOWER(COALESCE(metadata->>'is_on_loan', '')) IN ('true', '1', 'yes')
                )
                OR (
                    :contract_status = 'permanent'
                    AND LOWER(COALESCE(metadata->>'is_on_loan', '')) IN ('false', '0', 'no')
                )
              )
          AND (
                :loan_end_date IS NULL
                OR (
                    LOWER(COALESCE(metadata->>'is_on_loan', '')) IN ('true', '1', 'yes')
                    AND
                    COALESCE(metadata->>'loan_end_date', '') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
                    AND SUBSTRING(metadata->>'loan_end_date' FROM 1 FOR 10)::date <= CAST(:loan_end_date AS date)
                )
              )
          AND (
                :contract_end_date IS NULL
                OR (
                    COALESCE(metadata->>'contract_end_date', '') ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
                    AND SUBSTRING(metadata->>'contract_end_date' FROM 1 FOR 10)::date <= CAST(:contract_end_date AS date)
                )
              )
        ORDER BY
            CASE
                WHEN :position_short IS NOT NULL THEN COALESCE((
                    SELECT MAX(
                        CASE
                            WHEN counted_position.count_value ~ '^-?[0-9]+([.][0-9]+)?$'
                            THEN counted_position.count_value::numeric
                            ELSE 0
                        END
                    )
                    FROM jsonb_each_text(
                        CASE
                            WHEN jsonb_typeof(metadata->'position_counts') = 'object'
                            THEN metadata->'position_counts'
                            ELSE '{{}}'::jsonb
                        END
                    ) AS counted_position(role_value, count_value)
                    WHERE {role_value_short_sql("counted_position.role_value")} = :position_short
                ), 0)
                ELSE 0
            END DESC,
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
            "contract_status": contract_status,
            "loan_end_date": loan_end_date,
            "contract_end_date": contract_end_date,
            "nationality": nationality,
            "nationality_folded_q": f"%{nationality_norm}%" if nationality_norm else None,
            "league": league,
            "league_norm_q": f"%{league_norm}%" if league_norm else None,
            "league_folded_q": f"%{league_norm}%" if league_norm else None,
            "team": team,
            "team_norm_q": f"%{team_norm}%" if team_norm else None,
            "team_folded_q": f"%{team_norm}%" if team_norm else None,
            "position_filter": position,
            "position_short": position_short,
            "position_q": f"%{position_search}%" if position_search else None,
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

    provider_ids = {
        provider_id
        for row in rows
        if (provider_id := _sportmonks_id((row["content"] or {}).get("player_id"))) is not None
    }
    image_rows = db.execute(
        text("""
            SELECT player_id, image_url
            FROM enterprise_player_images
            WHERE player_id = ANY(:provider_ids)
              AND image_status = 'available'
        """),
        {"provider_ids": list(provider_ids)},
    ).mappings().all() if provider_ids else []
    images = {
        int(row["player_id"]): str(row["image_url"]).strip()
        for row in image_rows
        if row.get("image_url")
    }

    results = []
    for row in rows:
        content = dict(row["content"] or {})
        provider_id = _sportmonks_id(content.get("player_id"))
        image_url = images.get(provider_id, "")
        if image_url:
            content["image_url"] = image_url
        results.append({"id": row["id"], "content": content})
    return results


def get_player_pool_filter_options(db: Session, world_cup_mode: bool = False) -> Dict[str, List[str]]:
    table_name = player_pool_table(world_cup_mode)

    def distinct_metadata_values(field_name: str) -> List[str]:
        rows = db.execute(text(f"""
            SELECT DISTINCT metadata->>'{field_name}' AS value
            FROM {table_name}
            WHERE COALESCE(metadata->>'{field_name}', '') <> ''
            ORDER BY value
        """)).scalars().all()
        return [value for value in rows if value]

    position_rows = db.execute(text(f"""
        SELECT DISTINCT value
        FROM (
            SELECT metadata->>'position_name' AS value
            FROM {table_name}
            WHERE COALESCE(metadata->>'position_name', '') <> ''
            UNION
            SELECT seen_position.value AS value
            FROM {table_name}
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE
                    WHEN jsonb_typeof(metadata->'position_names_seen') = 'array'
                    THEN metadata->'position_names_seen'
                    ELSE '[]'::jsonb
                END
            ) AS seen_position(value)
            WHERE COALESCE(seen_position.value, '') <> ''
        ) positions
        ORDER BY value
    """)).scalars().all()

    return {
        "teams": distinct_metadata_values("team_name"),
        "leagues": distinct_metadata_values("league_name"),
        "nationalities": distinct_metadata_values("nationality_name"),
        "positions": [value for value in position_rows if value],
    }
