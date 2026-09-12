from __future__ import annotations

import os
from typing import Any

import requests


SPORTMONKS_BASE_URL = os.getenv(
    "SPORTMONKS_BASE_URL", "https://api.sportmonks.com/v3/football"
).rstrip("/")


class StandingsError(RuntimeError):
    pass


def get_league_standings(league_id: int) -> dict[str, Any]:
    """Return grouped current-season standings and official position rules."""
    token = os.getenv("SPORTMONKS_API_KEY")
    if not token:
        raise StandingsError("ScoutWise data service is not configured")

    league_response = requests.get(
        f"{SPORTMONKS_BASE_URL}/leagues/{int(league_id)}",
        params={"api_token": token, "include": "currentSeason"},
        timeout=45,
    )
    if league_response.status_code != 200:
        raise StandingsError(
            f"ScoutWise league data request failed (HTTP {league_response.status_code})"
        )
    league = league_response.json().get("data") or {}
    season = (
        league.get("currentSeason")
        or league.get("currentseason")
        or league.get("current_season")
        or {}
    )
    season_id = int(season.get("id") or 0)
    if not season_id:
        raise StandingsError("ScoutWise current season data is unavailable for this league")

    response = requests.get(
        f"{SPORTMONKS_BASE_URL}/standings/seasons/{season_id}",
        params={
            "api_token": token,
            "include": "participant;details.type;form;season;stage;group;rule.type",
        },
        timeout=45,
    )
    if response.status_code != 200:
        raise StandingsError(
            f"ScoutWise standings request failed (HTTP {response.status_code})"
        )

    aliases = {
        "games_played": "played", "played": "played", "overall_matches_played": "played",
        "wins": "won", "won": "won", "overall_won": "won",
        "draws": "drawn", "draw": "drawn", "overall_draw": "drawn",
        "losses": "lost", "lost": "lost", "overall_lost": "lost",
        "goals_for": "goalsFor", "goals scored": "goalsFor", "overall_goals_for": "goalsFor",
        "goals_against": "goalsAgainst", "goals conceded": "goalsAgainst", "overall_goals_against": "goalsAgainst",
        "goal_difference": "goalDifference", "goal difference": "goalDifference",
    }
    tables: dict[tuple[int | None, int | None], dict[str, Any]] = {}
    for standing in response.json().get("data") or []:
        participant = standing.get("participant") or {}
        values: dict[str, Any] = {}
        for detail in standing.get("details") or []:
            detail_type = detail.get("type") or {}
            type_name = str(detail_type.get("code") or detail_type.get("name") or "").lower().replace("-", "_")
            field = aliases.get(type_name)
            if field:
                values[field] = detail.get("value")
        goals_for, goals_against = values.get("goalsFor"), values.get("goalsAgainst")
        stage, group = standing.get("stage") or {}, standing.get("group") or {}
        stage_id, group_id = standing.get("stage_id"), standing.get("group_id")
        key = (int(stage_id) if stage_id is not None else None, int(group_id) if group_id is not None else None)
        label = " · ".join(value for value in (str(stage.get("name") or "").strip(), str(group.get("name") or "").strip()) if value) or "Lig Tablosu"
        table = tables.setdefault(key, {"key": f"{key[0] or 'stage'}:{key[1] or 'all'}", "label": label, "stageId": key[0], "groupId": key[1], "rows": []})
        rule_type = (standing.get("rule") or {}).get("type") or {}
        table["rows"].append({
            "position": standing.get("position"),
            "teamId": participant.get("id") or standing.get("participant_id"),
            "teamName": participant.get("name") or "-",
            "teamImageUrl": participant.get("image_path"),
            "played": values.get("played"), "won": values.get("won"), "drawn": values.get("drawn"), "lost": values.get("lost"),
            "goalsFor": goals_for, "goalsAgainst": goals_against,
            "goalDifference": values.get("goalDifference") if values.get("goalDifference") is not None else (int(goals_for) - int(goals_against) if goals_for is not None and goals_against is not None else None),
            "points": standing.get("points"),
            "form": "".join(str(item.get("form") or "") for item in (standing.get("form") or [])) or None,
            "standingRule": {"name": str(rule_type.get("name") or ""), "code": str(rule_type.get("code") or "")} if rule_type else None,
            "seasonName": (standing.get("season") or season).get("name"),
        })
    result_tables = list(tables.values())
    for table in result_tables:
        table["rows"].sort(key=lambda row: (row["position"] is None, row["position"] or 9999))
    result_tables.sort(key=lambda table: (table["label"].casefold(), table["stageId"] or 0, table["groupId"] or 0))
    return {"leagueId": int(league_id), "seasonId": season_id, "seasonName": season.get("name"), "tables": result_tables}
