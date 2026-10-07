from __future__ import annotations

import json
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.orm import Session

from player_pool_module.utilities import ROLE_SEARCH_SHORT_ALIASES, ROLE_SHORT_TO_LONG
from player_pool_module.utilities import (
    clean_str,
    folded_text_sql,
    norm_name,
    numeric_filter_sql,
    player_pool_table,
)


SEARCH_LIMIT = 100

# Shared by normal full-profile search and final-winner hydration.
PLAYER_IMAGE_JOIN = """
        -- An indexed lookup per candidate avoids repeated full image scans when
        -- combined filters make the planner underestimate the candidate count.
        LEFT JOIN LATERAL (
            SELECT image_url FROM enterprise_player_images images
            WHERE images.player_id = CASE
                WHEN COALESCE(metadata->>'player_id', '') ~ '^[0-9]+$'
                THEN (metadata->>'player_id')::bigint ELSE NULL END
              AND images.image_status = 'available'
            OFFSET 0
        ) epi ON TRUE
"""


def role_value_short_sql(value_expr: str) -> str:
    position = f"LOWER(TRIM(COALESCE({value_expr}, '')))"
    return f"""
    CASE
        WHEN {position} IN ('g', 'gk', 'goalkeeper', 'goal keeper') THEN 'GK'
        WHEN {position} = 'lwb' THEN 'LWB'
        WHEN {position} = 'left wing back' THEN 'LWB'
        WHEN {position} = 'lb' THEN 'LB'
        WHEN {position} = 'left back' THEN 'LB'
        WHEN {position} = 'lcb' THEN 'LCB'
        WHEN {position} = 'left center back' THEN 'LCB'
        WHEN {position} IN ('cb', 'center back', 'centre back') THEN 'CB'
        WHEN {position} = 'rcb' THEN 'RCB'
        WHEN {position} = 'right center back' THEN 'RCB'
        WHEN {position} = 'rb' THEN 'RB'
        WHEN {position} = 'right back' THEN 'RB'
        WHEN {position} = 'rwb' THEN 'RWB'
        WHEN {position} = 'right wing back' THEN 'RWB'
        WHEN {position} = 'lm' THEN 'LM'
        WHEN {position} = 'left midfield' THEN 'LM'
        WHEN {position} = 'ldm' THEN 'LDM'
        WHEN {position} = 'left defensive midfield' THEN 'LDM'
        WHEN {position} = 'lcm' THEN 'LCM'
        WHEN {position} = 'left center midfield' THEN 'LCM'
        WHEN {position} = 'lam' THEN 'LAM'
        WHEN {position} = 'left attacking midfield' THEN 'LAM'
        WHEN {position} IN ('cm', 'center midfield', 'central midfield') THEN 'CM'
        WHEN {position} IN ('cam', 'center attacking midfield', 'attacking midfield') THEN 'CAM'
        WHEN {position} IN ('cdm', 'center defensive midfield', 'defensive midfield') THEN 'CDM'
        WHEN {position} = 'rdm' THEN 'RDM'
        WHEN {position} = 'right defensive midfield' THEN 'RDM'
        WHEN {position} = 'rcm' THEN 'RCM'
        WHEN {position} = 'right center midfield' THEN 'RCM'
        WHEN {position} = 'ram' THEN 'RAM'
        WHEN {position} = 'right attacking midfield' THEN 'RAM'
        WHEN {position} = 'rm' THEN 'RM'
        WHEN {position} = 'right midfield' THEN 'RM'
        WHEN {position} IN ('a', 'f', 'cf', 'center forward', 'centre forward', 'attacker', 'forward') THEN 'CF'
        WHEN {position} = 'rcf' THEN 'RCF'
        WHEN {position} = 'right center forward' THEN 'RCF'
        WHEN {position} = 'lcf' THEN 'LCF'
        WHEN {position} = 'left center forward' THEN 'LCF'
        WHEN {position} = 'lw' THEN 'LW'
        WHEN {position} = 'left wing' THEN 'LW'
        WHEN {position} = 'rw' THEN 'RW'
        WHEN {position} = 'right wing' THEN 'RW'
        ELSE UPPER(TRIM(COALESCE({value_expr}, '')))
    END
    """


def role_short_sql() -> str:
    position = "LOWER(COALESCE(metadata->>'position_name', ''))"
    return f"""
    CASE
        WHEN {position} IN ('g', 'gk', 'goalkeeper', 'goal keeper') THEN 'GK'
        WHEN {position} = 'left wing back' THEN 'LWB'
        WHEN {position} = 'left back' THEN 'LB'
        WHEN {position} = 'left center back' THEN 'LCB'
        WHEN {position} IN ('center back', 'centre back') THEN 'CB'
        WHEN {position} = 'right center back' THEN 'RCB'
        WHEN {position} = 'right back' THEN 'RB'
        WHEN {position} = 'right wing back' THEN 'RWB'
        WHEN {position} = 'left midfield' THEN 'LM'
        WHEN {position} = 'left defensive midfield' THEN 'LDM'
        WHEN {position} = 'left center midfield' THEN 'LCM'
        WHEN {position} = 'left attacking midfield' THEN 'LAM'
        WHEN {position} IN ('center midfield', 'central midfield') THEN 'CM'
        WHEN {position} IN ('center attacking midfield', 'attacking midfield') THEN 'CAM'
        WHEN {position} IN ('center defensive midfield', 'defensive midfield') THEN 'CDM'
        WHEN {position} = 'right defensive midfield' THEN 'RDM'
        WHEN {position} = 'right center midfield' THEN 'RCM'
        WHEN {position} = 'right attacking midfield' THEN 'RAM'
        WHEN {position} = 'right midfield' THEN 'RM'
        WHEN {position} IN ('a', 'f', 'center forward', 'centre forward', 'attacker', 'forward') THEN 'CF'
        WHEN {position} = 'right center forward' THEN 'RCF'
        WHEN {position} = 'left center forward' THEN 'LCF'
        WHEN {position} = 'left wing' THEN 'LW'
        WHEN {position} = 'right wing' THEN 'RW'
        ELSE metadata->>'position_name'
    END
    """


def search_players(db: Session, filters: Dict[str, Any], *, all_matches: bool = False, candidate_roles: List[str] | None = None, candidate_choices: Dict[str, List[str]] | None = None, metadata_fields: List[str] | None = None, batch_size: int | None = None, discovery_metric_names: Dict[str, str] | None = None):
    world_cup_mode = bool(filters.get("worldCupMode"))
    table_name = player_pool_table(world_cup_mode)
    name = clean_str(filters.get("name"))
    gender = clean_str(filters.get("gender"))
    nationality = None if world_cup_mode else clean_str(filters.get("nationality"))
    league = None if world_cup_mode else clean_str(filters.get("league"))
    league_country = None if world_cup_mode else clean_str(filters.get("leagueCountry"))
    team = clean_str(filters.get("team"))
    position = clean_str(filters.get("position"))
    contract_status = None if world_cup_mode else clean_str(filters.get("contractStatus"))
    loan_end_date = None if world_cup_mode else clean_str(filters.get("loanEndDate"))
    contract_end_date = None if world_cup_mode else clean_str(filters.get("contractEndDate"))
    position_short = position.upper() if position and position.upper() in ROLE_SHORT_TO_LONG else None
    position_short = ROLE_SEARCH_SHORT_ALIASES.get(position_short, position_short)
    position_search = None if position_short else position
    name_norm = norm_name(name) if name else None
    team_norm = norm_name(team) if team else None
    league_norm = norm_name(league) if league else None
    nationality_norm = norm_name(nationality) if nationality else None
    position_norm = norm_name(position_search) if position_search else None

    league_choices = []
    for value in (candidate_choices or {}).get("league", []):
        parts = value.rsplit(" | ", 1)
        league_choices.append({"name": norm_name(parts[0]), "country": norm_name(parts[1]) if len(parts) == 2 else ""})
    context_league_sql = folded_text_sql("league_name").replace("metadata->>'league_name'", "choice_context.league_name")
    context_country_sql = folded_text_sql("league_country_name").replace("metadata->>'league_country_name'", "choice_context.league_country_name")

    # Projection is internal-only. SQL filters/order still inspect complete stored
    # metadata; only the fields sent to Python change. Photo lookup is deferred.
    content_sql = "metadata"
    image_sql = "epi.image_url"
    image_join = PLAYER_IMAGE_JOIN
    if metadata_fields is not None:
        content_sql = """(SELECT COALESCE(jsonb_object_agg(field.key, field.value), '{}'::jsonb)
            FROM jsonb_each(metadata) AS field(key, value)
            WHERE field.key = ANY(CAST(:metadata_fields AS text[])))"""
        image_sql = "NULL::text"
        image_join = ""

    # Discovery eligibility runs before projection/transfer. Other searches keep
    # their existing behavior. Numeric zero is available; aliases count once.
    eligibility_sql = ""
    eligibility_params = {}
    eligibility_join = ""
    if discovery_metric_names is not None:
        numeric_pattern = r"^[[:space:]]*[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?[[:space:]]*$"
        def finite_number(field):
            return f"(CASE WHEN metadata->>'{field}' ~ :eligibility_numeric_pattern THEN (metadata->>'{field}')::numeric ELSE NULL END)"
        minutes = finite_number('Minutes Played')
        matches = finite_number('match_count')
        age = finite_number('age')
        role_names = {name.lower(): code for code, name in ROLE_SHORT_TO_LONG.items()}
        role_names.update({'central midfield':'CM', 'defensive midfield':'CDM', 'attacking midfield':'CAM', 'centre back':'CB'})
        eligibility_join = """
        CROSS JOIN LATERAL (
          WITH normalized AS (
            SELECT COALESCE(CAST(:eligibility_role_names AS jsonb)->>LOWER(BTRIM(field.key)), UPPER(BTRIM(field.key))) AS code,
              CASE WHEN field.value ~ :eligibility_numeric_pattern THEN field.value::numeric ELSE NULL END AS count
            FROM jsonb_each_text(CASE WHEN jsonb_typeof(metadata->'position_counts') = 'object'
                 THEN metadata->'position_counts' ELSE '{}'::jsonb END) AS field(key, value)
          ), counts AS (
            SELECT code, SUM(count) AS count FROM normalized
            WHERE code = ANY(CAST(:eligibility_role_codes AS text[])) AND count > 0
            GROUP BY code
          ), shares AS (
            SELECT code, count, SUM(count) OVER () AS total, MAX(count) OVER () AS highest,
              ROW_NUMBER() OVER (ORDER BY count DESC, code) AS rank
            FROM counts
          ), eligible AS (
            SELECT code FROM shares WHERE rank <= 2 AND (highest-count)*100/total <= 20.00000001
            UNION ALL
            SELECT UPPER(metadata->>'primary_position_code')
            WHERE NOT EXISTS (SELECT 1 FROM counts)
              AND UPPER(metadata->>'primary_position_code') = ANY(CAST(:eligibility_role_codes AS text[]))
          )
          SELECT ARRAY(SELECT code FROM eligible WHERE CAST(:candidate_roles AS text[]) IS NULL
            OR code = ANY(CAST(:candidate_roles AS text[]))) AS roles
        ) discovery_eligibility
        """
        eligibility_sql = f"""
          AND COALESCE(NULLIF(BTRIM(metadata->>'player_name'), ''), NULLIF(BTRIM(metadata->>'name'), '')) IS NOT NULL
          AND NULLIF(BTRIM(metadata->>'team_name'), '') IS NOT NULL
          AND NULLIF(BTRIM(metadata->>'league_name'), '') IS NOT NULL
          AND NULLIF(BTRIM(metadata->>'nationality_name'), '') IS NOT NULL
          AND {age} > 0 AND {age} <= 1.7976931348623157e308::numeric
          AND CARDINALITY(discovery_eligibility.roles) > 0
          AND {minutes} > 0 AND {matches} > 0
          AND {minutes} * {matches} >= 90
          AND (SELECT COUNT(DISTINCT CAST(:eligibility_metric_names AS jsonb)->>field.key)
               FROM jsonb_each_text(metadata) AS field(key, value)
               WHERE CAST(:eligibility_metric_names AS jsonb) ? field.key
                 AND CASE WHEN field.value ~ :eligibility_numeric_pattern
                     THEN ABS(field.value::numeric) <= 1.7976931348623157e308::numeric
                     ELSE FALSE END) >= CASE WHEN 'GK' = ANY(discovery_eligibility.roles) THEN 15 ELSE 25 END
        """
        eligibility_params = {
            'eligibility_role_names': json.dumps(role_names),
            'eligibility_role_codes': list(ROLE_SHORT_TO_LONG),
            'eligibility_metric_names': json.dumps(discovery_metric_names),
            'eligibility_numeric_pattern': numeric_pattern,
        }

    query = text(f"""
        SELECT id, {content_sql} AS content, {image_sql} AS image_url
        FROM {table_name}
        {image_join}
        {eligibility_join}
        WHERE TRUE {eligibility_sql}
          AND (
                :name_q IS NULL
                OR metadata->>'player_name' ILIKE :name_q
                OR metadata->>'player_name_norm' ILIKE :name_norm_q
                OR {folded_text_sql("player_name")} LIKE :name_folded_q
              )
          AND (
                CAST(:candidate_roles AS text[]) IS NULL
                OR {"TRUE" if discovery_metric_names is not None else "FALSE"}
                OR {role_short_sql()} = ANY(CAST(:candidate_roles AS text[]))
                OR EXISTS (
                    SELECT 1 FROM jsonb_object_keys(
                        CASE WHEN jsonb_typeof(metadata->'position_counts') = 'object'
                        THEN metadata->'position_counts' ELSE '{{}}'::jsonb END
                    ) AS discovery_role(value)
                    WHERE {role_value_short_sql("discovery_role.value")} = ANY(CAST(:candidate_roles AS text[]))
                )
              )
          AND (CAST(:choices_nationality AS text[]) IS NULL OR REGEXP_REPLACE(TRIM({folded_text_sql("nationality_name")}), '[[:space:]]+', ' ', 'g') = ANY(CAST(:choices_nationality AS text[])))
          AND (CAST(:choices_team AS text[]) IS NULL OR REGEXP_REPLACE(TRIM({folded_text_sql("team_name")}), '[[:space:]]+', ' ', 'g') = ANY(CAST(:choices_team AS text[])))
          AND (
              CAST(:choices_league AS jsonb) IS NULL
              OR REGEXP_REPLACE(TRIM({folded_text_sql("league_name")}), '[[:space:]]+', ' ', 'g') IN (
                  SELECT name FROM jsonb_to_recordset(CAST(:choices_league AS jsonb)) AS chosen_league(name text, country text)
                  WHERE country = ''
              )
              OR (
                  REGEXP_REPLACE(TRIM({folded_text_sql("league_name")}), '[[:space:]]+', ' ', 'g') IN (
                      SELECT name FROM jsonb_to_recordset(CAST(:choices_league AS jsonb)) AS chosen_league(name text, country text)
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM player_comp_data choice_context
                      JOIN jsonb_to_recordset(CAST(:choices_league AS jsonb)) AS chosen_league(name text, country text)
                        ON REGEXP_REPLACE(TRIM({context_league_sql}), '[[:space:]]+', ' ', 'g') = chosen_league.name
                       AND REGEXP_REPLACE(TRIM({context_country_sql}), '[[:space:]]+', ' ', 'g') = chosen_league.country
                      WHERE choice_context.player_id = CASE
                          WHEN COALESCE(metadata->>'player_id', '') ~ '^-?[0-9]+$'
                          THEN (metadata->>'player_id')::bigint ELSE NULL END
                        AND choice_context.player_id::text = COALESCE(metadata->>'player_id', '')
                        AND REGEXP_REPLACE(TRIM({context_league_sql}), '[[:space:]]+', ' ', 'g') = REGEXP_REPLACE(TRIM({folded_text_sql("league_name")}), '[[:space:]]+', ' ', 'g')
                      -- Keep the indexed per-player lookup; avoid a hashed subplan
                      -- that normalizes every player_comp_data row for each search.
                      OFFSET 0
                  )
              )
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
                OR (
                    :league_country IS NULL
                    AND (
                        LOWER(COALESCE(metadata->>'league_name_norm', '')) ILIKE :league_norm_q
                        OR {folded_text_sql("league_name")} LIKE :league_folded_q
                    )
                )
              )
          AND (
                :league_country IS NULL
                OR EXISTS (
                    SELECT 1
                    FROM player_comp_data league_context
                    WHERE league_context.player_id = CASE
                        WHEN COALESCE(metadata->>'player_id', '') ~ '^-?[0-9]+$'
                        THEN (metadata->>'player_id')::bigint ELSE NULL END
                      AND league_context.player_id::text = COALESCE(metadata->>'player_id', '')
                      AND LOWER(COALESCE(league_context.league_name, '')) = LOWER(:league)
                      AND LOWER(COALESCE(league_context.league_country_name, '')) = LOWER(:league_country)
                    -- Preserve an indexed per-player lookup, as for multi-league discovery.
                    OFFSET 0
                )
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

    result = db.execute(
        query,
        {
            **eligibility_params,
            "metadata_fields": metadata_fields,
            "name_q": f"%{name}%" if name else None,
            "name_norm_q": f"%{name_norm}%" if name_norm else None,
            "name_folded_q": f"%{name_norm}%" if name_norm else None,
            "gender": gender,
            "candidate_roles": candidate_roles,
            **{f"choices_{key}": [norm_name(value) for value in (candidate_choices or {}).get(key, [])] or None for key in ("nationality", "team")},
            "choices_league": json.dumps(league_choices) if league_choices else None,
            "nationality": nationality,
            "nationality_folded_q": f"%{nationality_norm}%" if nationality_norm else None,
            "league": league,
            "league_country": league_country,
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
            "contract_status": contract_status,
            "loan_end_date": loan_end_date,
            "contract_end_date": contract_end_date,
            "limit": None if all_matches else int(filters.get("limit") or SEARCH_LIMIT),
        },
        execution_options={"stream_results": True, "yield_per": batch_size} if batch_size else {},
    ).mappings()

    if batch_size:
        def batches():
            try:
                for partition in result.partitions(batch_size):
                    yield from _player_rows(partition)
            finally:
                result.close()
        return batches()
    return _player_rows(result.all())


def _player_rows(rows) -> List[Dict[str, Any]]:
    results = []
    for row in rows:
        content = dict(row["content"] or {})
        image_url = str(row.get("image_url") or "").strip()
        if image_url:
            content["image_url"] = image_url
        results.append({"id": row["id"], "content": content})
    return results


def fetch_player_rows_by_ids(db: Session, player_ids: List[int]) -> List[Dict[str, Any]]:
    """Hydrate the final similarity winners by row ID in one bounded query."""
    ids = list(dict.fromkeys(player_ids))
    if not ids:
        return []
    if len(ids) > 50:
        raise ValueError("At most 50 similarity winners can be hydrated")
    rows = db.execute(text(f"""
        SELECT id, metadata AS content, epi.image_url
        FROM player_data
        {PLAYER_IMAGE_JOIN}
        WHERE id = ANY(CAST(:ids AS bigint[]))
    """), {"ids": ids}).mappings().all()
    return _player_rows(rows)
