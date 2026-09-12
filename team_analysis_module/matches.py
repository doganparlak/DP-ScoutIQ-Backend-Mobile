from typing import Any
from datetime import date
import os
import requests
from match_pool_module.fixtures import SPORTMONKS_BASE_URL, SportMonksError, _current_score

def get_current_team_matches(team_id: int, domestic_league_id: int) -> list[dict[str, Any]]:
    token = os.getenv("SPORTMONKS_API_KEY")
    if not token:
        raise SportMonksError("ScoutWise data service is not configured")
    team_response = requests.get(
        f"{SPORTMONKS_BASE_URL}/teams/{team_id}",
        params={"api_token": token, "include": "seasons"},
        timeout=30,
    )
    if team_response.status_code != 200:
        raise SportMonksError(f"ScoutWise team seasons request failed (HTTP {team_response.status_code})")
    seasons = (team_response.json().get("data") or {}).get("seasons") or []
    domestic = sorted(
        (season for season in seasons if int(season.get("league_id") or 0) == int(domestic_league_id)),
        key=lambda season: season.get("starting_at") or "",
        reverse=True,
    )
    if not domestic:
        return []
    current_domestic = next((season for season in domestic if season.get("is_current")), domestic[0])
    previous_domestic = None
    season_names = {current_domestic.get("name")}
    if previous_domestic:
        season_names.add(previous_domestic.get("name"))
    relevant_seasons = [season for season in seasons if season.get("name") in season_names]
    current_name = str(current_domestic.get("name") or "")
    current_starts = [season.get("starting_at") for season in relevant_seasons if season.get("name") == current_name and season.get("starting_at")]
    previous_name = str(previous_domestic.get("name") or "") if previous_domestic else ""
    previous_starts = [season.get("starting_at") for season in relevant_seasons if season.get("name") == previous_name and season.get("starting_at")]
    current_start = min(current_starts or [current_domestic.get("starting_at")])
    start_date = min(previous_starts) if previous_starts else current_start
    end_date = date.today().isoformat()
    params: dict[str, Any] = {
        "api_token": token,
        "include": "participants;league.country;state;scores;season",
        "per_page": 50,
        "page": 1,
        "order": "desc",
    }
    raw_fixtures: list[dict[str, Any]] = []
    while True:
        response = requests.get(
            f"{SPORTMONKS_BASE_URL}/fixtures/between/{start_date}/{end_date}/{team_id}",
            params=params,
            timeout=45,
        )
        if response.status_code != 200:
            raise SportMonksError(f"ScoutWise team fixtures request failed (HTTP {response.status_code})")
        payload = response.json()
        raw_fixtures.extend(payload.get("data") or [])
        pagination = payload.get("pagination") or {}
        if not pagination.get("has_more"):
            break
        params["page"] = int(params["page"]) + 1
    completed_codes = {"FT", "AET", "PEN", "WO", "AP"}
    results: list[dict[str, Any]] = []
    for fixture in raw_fixtures:
        state = fixture.get("state") or {}
        if (state.get("short_name") or state.get("state")) not in completed_codes:
            continue
        participants = fixture.get("participants") or []
        home = next((item for item in participants if (item.get("meta") or {}).get("location") == "home"), {})
        away = next((item for item in participants if (item.get("meta") or {}).get("location") == "away"), {})
        scores = fixture.get("scores") or []
        league = fixture.get("league") or {}
        fixture_season = fixture.get("season") or {}
        results.append({
            "fixtureId": int(fixture["id"]),
            "name": fixture.get("name") or f'{home.get("name", "")} vs {away.get("name", "")}',
            "startingAt": fixture.get("starting_at") or "",
            "country": (league.get("country") or {}).get("name") or "",
            "league": league.get("name") or "",
            "homeTeam": home.get("name") or "",
            "awayTeam": away.get("name") or "",
            "homeTeamId": int(home["id"]) if home.get("id") is not None else None,
            "awayTeamId": int(away["id"]) if away.get("id") is not None else None,
            "homeScore": _current_score(scores, "home"),
            "awayScore": _current_score(scores, "away"),
            "thisSeason": fixture_season.get("name") == current_name or (fixture.get("starting_at") or "")[:10] >= current_start,
        })
    return sorted([row for row in results if row["thisSeason"]], key=lambda fixture: fixture["startingAt"], reverse=True)


