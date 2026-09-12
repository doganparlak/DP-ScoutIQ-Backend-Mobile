from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


def _clean_many(values: List[str] | None) -> List[str]:
    return sorted({str(value).strip() for value in (values or []) if str(value).strip()})


def get_league_pool_options(
    db: Session,
    leagues: List[str] | None = None,
    countries: List[str] | None = None,
) -> Dict[str, List[str]]:
    selected_leagues = _clean_many(leagues)
    selected_countries = _clean_many(countries)
    query = text("""
        SELECT
            ARRAY(
                SELECT DISTINCT league_name
                FROM player_comp_data
                WHERE COALESCE(league_name, '') <> ''
                  AND (CAST(:countries AS text[]) = '{}' OR league_country_name = ANY(CAST(:countries AS text[])))
                ORDER BY league_name
            ) AS leagues,
            ARRAY(
                SELECT DISTINCT league_country_name
                FROM player_comp_data
                WHERE COALESCE(league_country_name, '') <> ''
                  AND (CAST(:leagues AS text[]) = '{}' OR league_name = ANY(CAST(:leagues AS text[])))
                ORDER BY league_country_name
            ) AS countries,
            ARRAY(
                SELECT DISTINCT role
                FROM player_comp_data pc
                CROSS JOIN LATERAL jsonb_object_keys(
                    CASE WHEN jsonb_typeof(pc.position_counts) = 'object' THEN pc.position_counts ELSE '{}'::jsonb END
                ) role
                WHERE COALESCE(role, '') <> ''
                  AND (CAST(:leagues AS text[]) = '{}' OR pc.league_name = ANY(CAST(:leagues AS text[])))
                  AND (CAST(:countries AS text[]) = '{}' OR pc.league_country_name = ANY(CAST(:countries AS text[])))
                ORDER BY role
            ) AS positions
    """)
    row = db.execute(query, {"leagues": selected_leagues, "countries": selected_countries}).mappings().one()
    return {key: list(row[key] or []) for key in ("leagues", "countries", "positions")}


def search_league_pool(db: Session, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    leagues = _clean_many(filters.get("leagues"))
    countries = _clean_many(filters.get("countries"))
    positions = [value.upper() for value in _clean_many(filters.get("positions"))]
    limit = min(max(int(filters.get("limit") or 100), 1), 200)

    query = text("""
        WITH eligible_rows AS (
            SELECT pc.*
            FROM player_comp_data pc
            WHERE (CAST(:leagues AS text[]) = '{}' OR pc.league_name = ANY(CAST(:leagues AS text[])))
              AND (CAST(:countries AS text[]) = '{}' OR pc.league_country_name = ANY(CAST(:countries AS text[])))
              AND (
                CAST(:positions AS text[]) = '{}'
                OR EXISTS (
                    SELECT 1
                    FROM jsonb_each_text(
                        CASE WHEN jsonb_typeof(pc.position_counts) = 'object' THEN pc.position_counts ELSE '{}'::jsonb END
                    ) position(role, appearances)
                    WHERE UPPER(position.role) = ANY(CAST(:positions AS text[]))
                      AND CASE
                            WHEN position.appearances ~ '^[0-9]+([.][0-9]+)?$'
                            THEN position.appearances::numeric
                            ELSE 0
                          END >= GREATEST(1, (
                            SELECT COALESCE(SUM(CASE WHEN value ~ '^[0-9]+([.][0-9]+)?$' THEN value::numeric ELSE 0 END), 0) * 0.20
                            FROM jsonb_each_text(
                                CASE WHEN jsonb_typeof(pc.position_counts) = 'object' THEN pc.position_counts ELSE '{}'::jsonb END
                            ) totals(key, value)
                          ))
                )
              )
        ), player_dimensions AS (
            SELECT
                league_id,
                league_name,
                MAX(league_country_id) AS league_country_id,
                MAX(league_country_name) AS league_country_name,
                MAX(league_image_path) AS league_image_path,
                player_id,
                MAX(player_name) AS player_name,
                MAX(nationality_name) AS nationality_name,
                AVG(age) FILTER (WHERE age > 0) AS age,
                AVG(height) FILTER (WHERE height > 0) AS height,
                AVG(weight) FILTER (WHERE weight > 0) AS weight,
                SUM(match_count) AS match_count
            FROM eligible_rows
            GROUP BY league_id, league_name, player_id
        ), league_team_counts AS (
            SELECT league_id, COUNT(DISTINCT team_id) AS team_count
            FROM eligible_rows
            GROUP BY league_id
        ), player_stat_values AS (
            SELECT er.league_id, er.player_id, stat.key, AVG(stat.value::numeric) AS value
            FROM eligible_rows er
            CROSS JOIN LATERAL jsonb_each_text(COALESCE(er.stats, '{}'::jsonb)) stat
            WHERE stat.value ~ '^-?[0-9]+([.][0-9]+)?$'
            GROUP BY er.league_id, er.player_id, stat.key
        ), player_stats AS (
            SELECT league_id, player_id, jsonb_object_agg(key, value) AS stats
            FROM player_stat_values
            GROUP BY league_id, player_id
        ), player_role_values AS (
            SELECT er.league_id, er.player_id, role.key, SUM(role.value::numeric) AS value
            FROM eligible_rows er
            CROSS JOIN LATERAL jsonb_each_text(COALESCE(er.position_counts, '{}'::jsonb)) role
            WHERE role.value ~ '^[0-9]+([.][0-9]+)?$'
            GROUP BY er.league_id, er.player_id, role.key
        ), player_roles AS (
            SELECT league_id, player_id, jsonb_object_agg(key, value) AS position_counts
            FROM player_role_values
            GROUP BY league_id, player_id
        ), player_league AS (
            SELECT pd.*, COALESCE(ps.stats, '{}'::jsonb) AS stats, COALESCE(pr.position_counts, '{}'::jsonb) AS position_counts
            FROM player_dimensions pd
            LEFT JOIN player_stats ps USING (league_id, player_id)
            LEFT JOIN player_roles pr USING (league_id, player_id)
        ), stat_values AS (
            SELECT pl.league_id, stat.key, AVG(stat.value::numeric) AS value
            FROM player_league pl
            CROSS JOIN LATERAL jsonb_each_text(pl.stats) stat
            WHERE stat.value ~ '^-?[0-9]+([.][0-9]+)?$'
            GROUP BY pl.league_id, stat.key
        ), league_stats AS (
            SELECT league_id, jsonb_object_agg(key, ROUND(value, 4)) AS stats
            FROM stat_values
            GROUP BY league_id
        ), role_values AS (
            SELECT pl.league_id, role.key, SUM(role.value::numeric) AS value
            FROM player_league pl
            CROSS JOIN LATERAL jsonb_each_text(pl.position_counts) role
            WHERE role.value ~ '^[0-9]+([.][0-9]+)?$'
              AND (CAST(:positions AS text[]) = '{}' OR UPPER(role.key) = ANY(CAST(:positions AS text[])))
            GROUP BY pl.league_id, role.key
        ), league_roles AS (
            SELECT league_id, jsonb_object_agg(key, ROUND(value, 0)) AS position_counts
            FROM role_values
            GROUP BY league_id
        )
        SELECT
            pl.league_id,
            pl.league_name,
            MAX(pl.league_country_id) AS league_country_id,
            MAX(pl.league_country_name) AS league_country_name,
            MAX(pl.league_image_path) AS league_image_path,
            COALESCE(MAX(eli.image_url), MAX(pl.league_image_path)) AS image_url,
            MAX(ltc.team_count) AS team_count,
            COUNT(*) AS player_count,
            ROUND(AVG(pl.match_count), 1) AS match_count,
            ROUND(AVG(pl.age), 1) AS age,
            ROUND(AVG(pl.height), 1) AS height,
            ROUND(AVG(pl.weight), 1) AS weight,
            COALESCE(lr.position_counts, '{}'::jsonb) AS position_counts,
            COALESCE(ls.stats, '{}'::jsonb) AS stats
        FROM player_league pl
        LEFT JOIN league_team_counts ltc ON ltc.league_id = pl.league_id
        LEFT JOIN league_roles lr ON lr.league_id = pl.league_id
        LEFT JOIN league_stats ls ON ls.league_id = pl.league_id
        LEFT JOIN enterprise_league_images eli
          ON eli.league_id = pl.league_id
         AND eli.image_status = 'available'
        GROUP BY pl.league_id, pl.league_name, lr.position_counts, ls.stats
        ORDER BY pl.league_name
        LIMIT :limit
    """)

    rows = db.execute(query, {"leagues": leagues, "countries": countries, "positions": positions, "limit": limit}).mappings().all()
    result = []
    for row in rows:
        content = dict(row)
        league_id = content.pop("league_id")
        selected_positions = positions or sorted((content.get("position_counts") or {}).keys())
        content.update({
            "entity_type": "league",
            "selected_positions": selected_positions,
            "country_name": content.get("league_country_name") or "",
        })
        result.append({"id": f"league:{league_id}:{','.join(countries)}:{','.join(selected_positions)}", "content": content})
    return result
