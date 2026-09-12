from __future__ import annotations

import os
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session


SPORTMONKS_BASE_URL = os.getenv(
    "SPORTMONKS_BASE_URL", "https://api.sportmonks.com/v3/football"
).rstrip("/")
MAX_DATE_RANGE_DAYS = 100
MAX_UPSTREAM_PAGES = 5
MAX_FIXTURE_RESULTS = 50
TEAM_DETAILS_CACHE_TTL_SECONDS = 900
_TEAM_DETAILS_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
_TEAM_DETAILS_CACHE_LOCK = threading.Lock()


class SportMonksError(RuntimeError):
    pass


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def _fold(value: str | None) -> str:
    translated = _clean(value).translate(
        str.maketrans({"ı": "i", "İ": "I", "ğ": "g", "Ğ": "G", "ş": "s", "Ş": "S", "ç": "c", "Ç": "C", "ü": "u", "Ü": "U", "ö": "o", "Ö": "O", "ß": "ss"})
    )
    return "".join(
        char for char in unicodedata.normalize("NFD", translated)
        if unicodedata.category(char) != "Mn"
    ).casefold()


def _matches(actual: str | None, requested: str | None) -> bool:
    needle = _fold(requested)
    return not needle or needle in _fold(actual)


def _team_matches(actual: str | None, requested: str | None) -> bool:
    needle = _fold(requested)
    return not needle or _fold(actual) == needle


def get_match_filter_options(
    db: Session,
    country: str | None = None,
    league: str | None = None,
) -> dict[str, list[str]]:
    country = _clean(country)
    league = _clean(league)
    row = db.execute(
        text(
            """
            SELECT
              ARRAY(
                SELECT DISTINCT league_country_name
                FROM player_comp_data
                WHERE COALESCE(league_country_name, '') <> ''
                  AND (:league = '' OR league_name = :league)
                ORDER BY league_country_name
              ) AS countries,
              ARRAY(
                SELECT DISTINCT league_name
                FROM player_comp_data
                WHERE COALESCE(league_name, '') <> ''
                  AND (:country = '' OR league_country_name = :country)
                ORDER BY league_name
              ) AS leagues,
              ARRAY(
                SELECT DISTINCT team_name
                FROM player_comp_data
                WHERE COALESCE(team_name, '') <> ''
                  AND (:country = '' OR league_country_name = :country)
                  AND (:league = '' OR league_name = :league)
                ORDER BY team_name
              ) AS teams
            """
        ),
        {"country": country, "league": league},
    ).mappings().one()
    return {key: list(row[key] or []) for key in ("countries", "leagues", "teams")}


def search_team_pool(
    db: Session,
    team: str | None = None,
    country: str | None = None,
    league: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            WITH candidates AS (
              SELECT team_id, MAX(team_name) AS team_name
              FROM player_comp_data
              WHERE team_id IS NOT NULL
                AND COALESCE(team_name, '') <> ''
                AND (
                  :team_fold = ''
                  OR TRANSLATE(LOWER(team_name), 'çğıöşü', 'cgiosu') LIKE ('%' || :team_fold || '%')
                )
                AND (:country = '' OR league_country_name = :country)
                AND (:league = '' OR league_name = :league)
              GROUP BY team_id
              ORDER BY MAX(team_name)
              LIMIT :limit
            )
            SELECT
              candidate.team_id,
              candidate.team_name,
              domestic.league_country_name,
              domestic.league_name,
              domestic.league_id
            FROM candidates candidate
            LEFT JOIN LATERAL (
              SELECT league_country_name, league_name, league_id
              FROM player_comp_data team_context
              WHERE team_context.team_id = candidate.team_id
                AND COALESCE(league_country_name, '') <> ''
                AND COALESCE(league_name, '') <> ''
              GROUP BY league_country_name, league_name, league_id
              ORDER BY
                CASE WHEN LOWER(league_country_name) = 'europe' THEN 1 ELSE 0 END,
                CASE WHEN LOWER(league_name) LIKE '%cup%' THEN 1 ELSE 0 END,
                COUNT(*) DESC,
                league_name
              LIMIT 1
            ) domestic ON TRUE
            ORDER BY candidate.team_name
            LIMIT :limit
            """
        ),
        {
            "team_fold": _fold(team),
            "country": _clean(country),
            "league": _clean(league),
            "limit": min(max(1, int(limit)), 50),
        },
    ).mappings().all()
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(rows)))) as executor:
        details = list(executor.map(lambda row: _get_team_details(int(row["team_id"])), rows))
    return [
        {
            "id": str(row["team_id"]),
            "name": detail.get("name") or row["team_name"],
            "country": detail.get("country") or row["league_country_name"] or "",
            "league": row["league_name"] or "",
            "leagueId": int(row["league_id"]) if row["league_id"] is not None else None,
            "logoUrl": detail.get("logoUrl"),
            "city": detail.get("city") or "",
            "coachName": detail.get("coachName") or "",
            "playerCount": int(detail.get("playerCount") or 0),
            "stadiumName": detail.get("stadiumName") or "",
            "stadiumImageUrl": detail.get("stadiumImageUrl"),
        }
        for row, detail in zip(rows, details)
    ]


def _get_team_details(team_id: int) -> dict[str, Any]:
    now = time.monotonic()
    with _TEAM_DETAILS_CACHE_LOCK:
        cached = _TEAM_DETAILS_CACHE.get(team_id)
        if cached and now - cached[0] < TEAM_DETAILS_CACHE_TTL_SECONDS:
            return cached[1]
    token = os.getenv("SPORTMONKS_API_KEY")
    if not token:
        raise SportMonksError("ScoutWise data service is not configured")
    response = requests.get(
        f"{SPORTMONKS_BASE_URL}/teams/{team_id}",
        params={"api_token": token, "include": "country;venue;players;coaches.coach"},
        timeout=30,
    )
    if response.status_code != 200:
        raise SportMonksError(f"ScoutWise team data request failed (HTTP {response.status_code})")
    team = response.json().get("data") or {}
    venue = team.get("venue") or {}
    active_coach = next((item for item in team.get("coaches") or [] if item.get("active")), None) or {}
    coach = active_coach.get("coach") or {}
    player_ids = {item.get("player_id") for item in team.get("players") or [] if item.get("player_id")}
    detail = {
        "name": team.get("name") or "",
        "country": (team.get("country") or {}).get("name") or "",
        "logoUrl": team.get("image_path"),
        "city": venue.get("city_name") or "",
        "coachName": coach.get("display_name") or coach.get("name") or "",
        "playerCount": len(player_ids),
        "stadiumName": venue.get("name") or "",
        "stadiumImageUrl": venue.get("image_path"),
    }
    with _TEAM_DETAILS_CACHE_LOCK:
        _TEAM_DETAILS_CACHE[team_id] = (now, detail)
    return detail


def get_team_played_matches(team_id: int, domestic_league_id: int) -> list[dict[str, Any]]:
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
    previous_domestic = next((season for season in domestic if season.get("name") != current_domestic.get("name")), None)
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
    return sorted(results, key=lambda fixture: fixture["startingAt"], reverse=True)


def resolve_league_id(db: Session, league: str | None, country: str | None) -> int | None:
    league = _clean(league)
    if not league:
        return None
    value = db.execute(
        text(
            """
            SELECT league_id
            FROM player_comp_data
            WHERE league_name = :league
              AND (:country = '' OR league_country_name = :country)
            GROUP BY league_id
            ORDER BY COUNT(*) DESC
            LIMIT 1
            """
        ),
        {"league": league, "country": _clean(country)},
    ).scalar()
    return int(value) if value is not None else None


def resolve_team_id(db: Session, team: str | None) -> int | None:
    team = _clean(team)
    if not team:
        return None
    rows = db.execute(
        text(
            """
            SELECT team_id, team_name, COUNT(*) AS usage_count
            FROM player_comp_data
            WHERE team_id IS NOT NULL
              AND COALESCE(team_name, '') <> ''
              AND LOWER(team_name) = LOWER(:team)
            GROUP BY team_id, team_name
            ORDER BY COUNT(*) DESC, team_id
            """
        ),
        {"team": team},
    ).mappings().all()
    if rows:
        return int(rows[0]["team_id"])
    candidates = db.execute(
        text(
            """
            SELECT team_id, team_name, COUNT(*) AS usage_count
            FROM player_comp_data
            WHERE team_id IS NOT NULL
              AND COALESCE(team_name, '') <> ''
            GROUP BY team_id, team_name
            ORDER BY COUNT(*) DESC, team_id
            """
        )
    ).mappings().all()
    folded = _fold(team)
    match = next((row for row in candidates if _fold(row["team_name"]) == folded), None)
    return int(match["team_id"]) if match else None


def _current_score(scores: list[dict[str, Any]], location: str) -> int | None:
    current = next(
        (
            item for item in scores
            if item.get("description") == "CURRENT"
            and (item.get("score") or {}).get("participant") == location
        ),
        None,
    )
    value = (current or {}).get("score", {}).get("goals")
    return int(value) if isinstance(value, (int, float)) else None


def _fixture_out(fixture: dict[str, Any]) -> dict[str, Any] | None:
    participants = fixture.get("participants") or []
    home = next((team for team in participants if (team.get("meta") or {}).get("location") == "home"), None)
    away = next((team for team in participants if (team.get("meta") or {}).get("location") == "away"), None)
    if not home or not away:
        return None
    league = fixture.get("league") or {}
    country = league.get("country") or {}
    state = fixture.get("state") or {}
    scores = fixture.get("scores") or []
    venue = fixture.get("venue") or {}
    season = fixture.get("season") or {}
    stage = fixture.get("stage") or {}
    round_data = fixture.get("round") or {}
    referees = fixture.get("referees") or []
    primary_referee = next(
        (
            item for item in referees
            if str((item.get("type") or {}).get("name") or "").casefold()
            in {"referee", "main referee"}
        ),
        referees[0] if referees else {},
    )
    referee_name = (
        primary_referee.get("name")
        or (primary_referee.get("referee") or {}).get("name")
        or ""
    )
    return {
        "fixtureId": fixture.get("id"),
        "name": fixture.get("name") or f'{home.get("name", "")} vs {away.get("name", "")}',
        "startingAt": fixture.get("starting_at"),
        "resultInfo": fixture.get("result_info"),
        "country": {"id": country.get("id"), "name": country.get("name") or "", "imageUrl": country.get("image_path")},
        "league": {"id": league.get("id"), "name": league.get("name") or "", "imageUrl": league.get("image_path")},
        "season": {"id": season.get("id"), "name": season.get("name") or ""},
        "stage": {"id": stage.get("id"), "name": stage.get("name") or ""},
        "round": {"id": round_data.get("id"), "name": round_data.get("name") or ""},
        "leg": fixture.get("leg") or "",
        "refereeName": referee_name,
        "venue": {
            "id": venue.get("id"),
            "name": venue.get("name") or "",
            "cityName": venue.get("city_name") or "",
            "capacity": venue.get("capacity"),
            "surface": venue.get("surface") or "",
            "imageUrl": venue.get("image_path"),
        },
        "homeTeam": {"id": home.get("id"), "name": home.get("name") or "", "imageUrl": home.get("image_path"), "score": _current_score(scores, "home")},
        "awayTeam": {"id": away.get("id"), "name": away.get("name") or "", "imageUrl": away.get("image_path"), "score": _current_score(scores, "away")},
        "state": {"code": state.get("short_name") or state.get("state") or "", "name": state.get("name") or ""},
    }


def get_fixture(
    fixture_id: int,
    include_team_details: bool = False,
    include_pre_match_data: bool = False,
) -> dict[str, Any] | None:
    """Return the current compact fixture representation used by Enterprise."""
    token = os.getenv("SPORTMONKS_API_KEY")
    if not token:
        raise SportMonksError("ScoutWise data service is not configured")
    includes = "participants;league.country;season;stage;round;venue;state;referees;scores"
    if include_pre_match_data:
        includes += ";lineups.player;lineups.position;formations;metadata;expectedLineups.player;expectedLineups.team"
    response = requests.get(
        f"{SPORTMONKS_BASE_URL}/fixtures/{int(fixture_id)}",
        params={
            "api_token": token,
            "include": includes,
        },
        timeout=45,
    )
    # Expected lineups are a premium include. Keep the rest of the pre-match
    # card available when an account does not have that entitlement.
    if include_pre_match_data and response.status_code == 403:
        fallback_includes = includes.replace(
            ";expectedLineups.player;expectedLineups.team", ""
        )
        response = requests.get(
            f"{SPORTMONKS_BASE_URL}/fixtures/{int(fixture_id)}",
            params={"api_token": token, "include": fallback_includes},
            timeout=45,
        )
    if response.status_code != 200:
        detail = "ScoutWise fixture data request failed"
        try:
            detail = response.json().get("message") or detail
        except ValueError:
            pass
        raise SportMonksError(f"{detail} (HTTP {response.status_code})")
    compact = _fixture_out(response.json().get("data") or {})
    if not compact:
        return None
    raw_fixture = response.json().get("data") or {}
    if include_pre_match_data:
        metadata = raw_fixture.get("metadata") or []
        if isinstance(metadata, dict):
            metadata = [metadata]
        confirmed = False
        for item in metadata:
            if not isinstance(item, dict):
                continue
            type_data = item.get("type") or {}
            key = str(
                item.get("key")
                or item.get("name")
                or type_data.get("code")
                or type_data.get("name")
                or ""
            ).strip().casefold().replace(" ", "_")
            if key != "lineup_confirmed":
                continue
            value = item.get("value", item.get("data", item.get("boolean")))
            confirmed = value is True or str(value).strip().casefold() in {"true", "1", "yes"}
            break
        compact["lineupConfirmed"] = confirmed
        compact["officialLineups"] = raw_fixture.get("lineups") or []
        compact["expectedLineups"] = (
            raw_fixture.get("expectedLineups")
            or raw_fixture.get("expected_lineups")
            or raw_fixture.get("expectedlineups")
            or []
        )
        compact["formations"] = raw_fixture.get("formations") or []
    venue = compact.get("venue") or {}
    if include_team_details:
        try:
            sides = ("homeTeam", "awayTeam")
            with ThreadPoolExecutor(max_workers=2) as executor:
                details = list(
                    executor.map(
                        lambda side: _get_team_details(
                            int((compact.get(side) or {}).get("id") or 0)
                        ),
                        sides,
                    )
                )
            for side, detail in zip(sides, details):
                compact[side] = {
                    **(compact.get(side) or {}),
                    "coachName": detail.get("coachName") or "",
                    "playerCount": int(detail.get("playerCount") or 0),
                }
            home_details = details[0]
            compact["venue"] = {
                **venue,
                "name": venue.get("name") or home_details.get("stadiumName") or "",
                "cityName": venue.get("cityName") or home_details.get("city") or "",
                "imageUrl": venue.get("imageUrl") or home_details.get("stadiumImageUrl"),
            }
        except (SportMonksError, TypeError, ValueError):
            pass
    elif not venue.get("name") or not venue.get("cityName"):
        try:
            home_details = _get_team_details(int((compact.get("homeTeam") or {}).get("id") or 0))
            compact["venue"] = {
                **venue,
                "name": venue.get("name") or home_details.get("stadiumName") or "",
                "cityName": venue.get("cityName") or home_details.get("city") or "",
                "imageUrl": venue.get("imageUrl") or home_details.get("stadiumImageUrl"),
            }
        except (SportMonksError, TypeError, ValueError):
            pass
    return compact


def search_fixtures(filters: dict[str, Any]) -> dict[str, Any]:
    start_value = _clean(filters.get("startDate"))
    end_value = _clean(filters.get("endDate"))
    if not start_value and not end_value:
        end = date.today()
        start = end - timedelta(days=MAX_DATE_RANGE_DAYS)
    elif start_value and not end_value:
        start = date.fromisoformat(start_value)
        end = start + timedelta(days=MAX_DATE_RANGE_DAYS)
    elif end_value and not start_value:
        end = date.fromisoformat(end_value)
        start = end - timedelta(days=MAX_DATE_RANGE_DAYS)
    else:
        start = date.fromisoformat(start_value)
        end = date.fromisoformat(end_value)
    if end < start:
        raise ValueError("endDate cannot be earlier than startDate")
    if (end - start).days > MAX_DATE_RANGE_DAYS:
        raise ValueError(f"Date range cannot exceed {MAX_DATE_RANGE_DAYS} days")

    token = os.getenv("SPORTMONKS_API_KEY")
    if not token:
        raise SportMonksError("ScoutWise data service is not configured")

    league_id = filters.get("leagueId")
    page = max(1, int(filters.get("page") or 1))
    limit = min(max(1, int(filters.get("limit") or MAX_FIXTURE_RESULTS)), MAX_FIXTURE_RESULTS)
    params: dict[str, Any] = {
        "api_token": token,
        "include": "participants;league.country;season;stage;round;venue;state;referees;scores",
        "order": "asc",
        "per_page": MAX_FIXTURE_RESULTS,
        "page": page,
    }
    upstream_filters: list[str] = []
    if league_id:
        upstream_filters.append(f"fixtureLeagues:{int(league_id)}")
    home_team_id = filters.get("homeTeamId")
    away_team_id = filters.get("awayTeamId")
    participant_id = home_team_id or away_team_id
    participant = _clean(filters.get("homeTeam")) or _clean(filters.get("awayTeam"))
    if participant and not participant_id:
        upstream_filters.append(f"participantSearch:{participant}")
    if upstream_filters:
        params["filters"] = ";".join(upstream_filters)

    print(
        "[match_analysis_search] "
        f"event=request start={start} end={end} country={_clean(filters.get('country'))!r} "
        f"league={_clean(filters.get('league'))!r} league_id={league_id!r} "
        f"home={_clean(filters.get('homeTeam'))!r} away={_clean(filters.get('awayTeam'))!r} "
        f"upstream_filters={params.get('filters', '')!r}",
        flush=True,
    )

    collected: list[dict[str, Any]] = []
    has_more = False
    pages_read = 0
    while pages_read < MAX_UPSTREAM_PAGES and len(collected) < limit:
        endpoint = f"{SPORTMONKS_BASE_URL}/fixtures/between/{start.isoformat()}/{end.isoformat()}"
        if participant_id:
            endpoint = f"{endpoint}/{int(participant_id)}"
        response = requests.get(
            endpoint,
            params=params,
            timeout=45,
        )
        if response.status_code != 200:
            detail = "ScoutWise fixture data request failed"
            try:
                detail = response.json().get("message") or detail
            except ValueError:
                pass
            raise SportMonksError(f"{detail} (HTTP {response.status_code})")
        payload = response.json()
        upstream_rows = payload.get("data") or []
        for raw in upstream_rows:
            item = _fixture_out(raw)
            if not item:
                continue
            if not _matches(item["country"]["name"], filters.get("country")):
                continue
            if not _matches(item["league"]["name"], filters.get("league")):
                continue
            if home_team_id is not None and int(item["homeTeam"]["id"] or 0) != int(home_team_id):
                continue
            if home_team_id is None and not _team_matches(item["homeTeam"]["name"], filters.get("homeTeam")):
                continue
            if away_team_id is not None and int(item["awayTeam"]["id"] or 0) != int(away_team_id):
                continue
            if away_team_id is None and not _team_matches(item["awayTeam"]["name"], filters.get("awayTeam")):
                continue
            collected.append(item)
            if len(collected) >= limit:
                break
        pagination = payload.get("pagination") or payload.get("meta", {}).get("pagination", {})
        has_more = bool(pagination.get("has_more"))
        pages_read += 1
        if not has_more or len(collected) >= limit:
            break
        params["page"] = int(params["page"]) + 1

    print(
        "[match_analysis_search] "
        f"event=complete upstream_rows={len(upstream_rows)} matched_rows={len(collected)} "
        f"pages_read={pages_read} has_more={has_more}",
        flush=True,
    )
    return {"fixtures": collected, "pagination": {"page": page, "hasMore": has_more, "pagesRead": pages_read}}
