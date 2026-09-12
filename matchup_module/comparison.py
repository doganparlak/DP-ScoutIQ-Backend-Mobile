from __future__ import annotations

import json
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.orm import Session

from player_pool_module.utilities import player_pool_table


def _enrich_visual_identity(db: Session, content: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the current competition identity used by comparison slot logos."""
    result = dict(content or {})
    try:
        sportmonks_id = int(float(result.get("player_id")))
    except (TypeError, ValueError):
        return result

    league_name = str(result.get("league_name") or result.get("league") or "").strip()
    try:
        team_id = int(float(result.get("team_id")))
    except (TypeError, ValueError):
        team_id = None

    row = db.execute(
        text(
            """
            SELECT pc.league_id, eli.image_url AS league_image_path
            FROM player_comp_data pc
            LEFT JOIN enterprise_league_images eli
              ON eli.league_id = pc.league_id
             AND eli.image_status = 'available'
            WHERE pc.player_id = :player_id
            ORDER BY
              CASE WHEN :league_name <> '' AND LOWER(BTRIM(pc.league_name)) = LOWER(BTRIM(:league_name)) THEN 0 ELSE 1 END,
              CASE WHEN :team_id IS NOT NULL AND pc.team_id = :team_id THEN 0 ELSE 1 END,
              pc.match_count DESC NULLS LAST
            LIMIT 1
            """
        ),
        {
            "player_id": sportmonks_id,
            "league_name": league_name,
            "team_id": team_id,
        },
    ).mappings().first()
    if row:
        if result.get("league_id") is None and row.get("league_id") is not None:
            result["league_id"] = row["league_id"]
        if not result.get("league_image_path") and row.get("league_image_path"):
            result["league_image_path"] = row["league_image_path"]
    return result


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
        "content": _enrich_visual_identity(db, row["content"] or {}),
    }


def _fetch_player_by_sportmonks_id(db: Session, sportmonks_id: int, world_cup_mode: bool = False) -> Dict[str, Any]:
    table_name = player_pool_table(world_cup_mode)
    rows = db.execute(text(f"""
        SELECT id, metadata AS content FROM {table_name}
        WHERE CASE WHEN metadata->>'player_id' ~ '^[0-9]+([.]0+)?$'
              THEN (metadata->>'player_id')::numeric END = :sportmonks_id
        LIMIT 2
    """), {"sportmonks_id": sportmonks_id}).mappings().all()
    if len(rows) != 1:
        raise ValueError(f"Expected one current player for SportMonks ID {sportmonks_id}; found {len(rows)}")
    return {
        "id": rows[0]["id"],
        "content": _enrich_visual_identity(db, rows[0]["content"] or {}),
    }


def get_matchup_comparison(db: Session, player1_id: str, player2_id: str, world_cup_mode: bool = False, player1_sportmonks_id: int | None = None, player2_sportmonks_id: int | None = None) -> Dict[str, Any]:
    return {
        "player1": _fetch_player_by_sportmonks_id(db, player1_sportmonks_id, world_cup_mode) if player1_sportmonks_id is not None else _fetch_player_metadata(db, player1_id, world_cup_mode),
        "player2": _fetch_player_by_sportmonks_id(db, player2_sportmonks_id, world_cup_mode) if player2_sportmonks_id is not None else _fetch_player_metadata(db, player2_id, world_cup_mode),
    }


def _source_key(team_id: Any, competition_id: Any) -> str:
    return json.dumps([str(team_id or ""), str(competition_id or "")], separators=(",", ":"))


def _fetch_comp_rows(db: Session, player_id: str, sportmonks_id: int | None = None) -> List[Dict[str, Any]]:
    player = _fetch_player_metadata(db, player_id, False)
    metadata = player["content"] or {}
    player_name = str(metadata.get("player_name") or metadata.get("name") or "").strip()
    nationality = str(
        metadata.get("nationality_name") or metadata.get("nationality") or ""
    ).strip()
    if sportmonks_id is None and not player_name:
        raise ValueError(f"Player identity not found: {player_id}")
    identity_sql = "player_id = :sportmonks_id" if sportmonks_id is not None else "LOWER(TRIM(player_name)) = LOWER(TRIM(:player_name)) AND (:nationality = '' OR LOWER(TRIM(COALESCE(nationality_name, ''))) = LOWER(TRIM(:nationality)))"
    params = {"sportmonks_id": sportmonks_id} if sportmonks_id is not None else {"player_name": player_name, "nationality": nationality}
    rows = db.execute(
        text(f"""
            SELECT
                player_id,
                player_name,
                nationality_name,
                age,
                height,
                weight,
                team_id,
                team_name,
                league_id,
                league_name,
                league_short_code,
                league_country_name,
                match_count,
                position_counts,
                stats
            FROM player_comp_data
            WHERE {identity_sql}
            ORDER BY team_name, league_name
        """),
        params,
    ).mappings().all()
    return [dict(row) for row in rows]


def get_player_comparison_sources(db: Session, player_id: str, sportmonks_id: int | None = None) -> List[Dict[str, Any]]:
    if sportmonks_id is not None:
        player_id = str(_fetch_player_by_sportmonks_id(db, sportmonks_id)["id"])
    rows = _fetch_comp_rows(db, player_id, sportmonks_id)
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = _source_key(row.get("team_id"), row.get("league_id"))
        item = grouped.setdefault(
            key,
            {
                "key": key,
                "country": str(row.get("league_country_name") or ""),
                "leagueShortCode": str(row.get("league_short_code") or ""),
                "team": str(row.get("team_name") or ""),
                "competition": str(row.get("league_name") or ""),
                "matchCount": 0.0,
            },
        )
        try:
            item["matchCount"] += float(row.get("match_count") or 0)
        except (TypeError, ValueError):
            pass
    return list(grouped.values())


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace("%", ""))
        except ValueError:
            return None
    return None


def _selected_comp_metadata(
    db: Session,
    player_id: str,
    selected_sources: List[str],
    base_content: Dict[str, Any],
    sportmonks_id: int | None = None,
) -> Dict[str, Any]:
    selected = set(selected_sources)
    rows = [
        row
        for row in _fetch_comp_rows(db, player_id, sportmonks_id)
        if _source_key(row.get("team_id"), row.get("league_id")) in selected
    ]
    if not rows:
        raise ValueError(f"Selected player competition data not found: {player_id}")

    result: Dict[str, Any] = {
        key: base_content.get(key)
        for key in (
            "potential",
            "form",
            "gender",
            "primary_position_code",
        )
        if base_content.get(key) is not None
    }
    first = rows[0]
    result.update(
        {
            "player_name": first.get("player_name") or base_content.get("player_name"),
            "nationality_name": first.get("nationality_name") or base_content.get("nationality_name"),
            "team_name": ", ".join(dict.fromkeys(str(row.get("team_name") or "") for row in rows if row.get("team_name"))),
            "league_name": ", ".join(dict.fromkeys(str(row.get("league_name") or "") for row in rows if row.get("league_name"))),
        }
    )
    team_ids = list(dict.fromkeys(row.get("team_id") for row in rows if row.get("team_id") is not None))
    league_ids = list(dict.fromkeys(row.get("league_id") for row in rows if row.get("league_id") is not None))
    if len(team_ids) == 1:
        result["team_id"] = team_ids[0]
    if len(league_ids) == 1:
        result["league_id"] = league_ids[0]
        image = db.execute(
            text(
                """
                SELECT image_url
                FROM enterprise_league_images
                WHERE league_id = :league_id AND image_status = 'available'
                LIMIT 1
                """
            ),
            {"league_id": league_ids[0]},
        ).scalar()
        if image:
            result["league_image_path"] = image

    total_matches = sum(_number(row.get("match_count")) or 0 for row in rows)
    result["match_count"] = total_matches
    for field in ("age", "height", "weight"):
        weighted = [
            (_number(row.get(field)), _number(row.get("match_count")) or 1)
            for row in rows
        ]
        weighted = [(value, weight) for value, weight in weighted if value is not None]
        if weighted:
            result[field] = round(
                sum(value * weight for value, weight in weighted) / sum(weight for _, weight in weighted),
                1,
            )

    positions: Dict[str, float] = {}
    metric_totals: Dict[str, float] = {}
    metric_weights: Dict[str, float] = {}
    for row in rows:
        for role, count in (row.get("position_counts") or {}).items():
            numeric = _number(count)
            if numeric is not None:
                positions[str(role)] = positions.get(str(role), 0) + numeric
        weight = _number(row.get("match_count")) or 1
        for metric, value in (row.get("stats") or {}).items():
            numeric = _number(value)
            if numeric is None:
                continue
            metric_totals[str(metric)] = metric_totals.get(str(metric), 0) + numeric * weight
            metric_weights[str(metric)] = metric_weights.get(str(metric), 0) + weight

    result["position_counts"] = {key: round(value, 2) for key, value in positions.items()}
    result["position_names_seen"] = list(positions)
    result["position_count_total"] = round(sum(positions.values()), 2)
    result.update(
        {
            metric: round(total / metric_weights[metric], 4)
            for metric, total in metric_totals.items()
            if metric_weights.get(metric)
        }
    )
    return result
