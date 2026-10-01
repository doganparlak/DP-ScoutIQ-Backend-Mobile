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
    from .current_leagues import get_team_filter_options
    return get_team_filter_options(db, country, league)


def search_team_pool(
    db: Session,
    team: str | None = None,
    country: str | None = None,
    league: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    from .current_leagues import load_current_teams, fetch_current_league
    candidates = [row for row in load_current_teams(db) if _matches(row['team_name'], team)]
    rows = []
    for candidate in candidates:
        row = dict(candidate)
        if row['league_id'] is None and _clean(team):
            # Only named searches verify unresolved teams upstream. Broad league
            # filters use confirmed membership, not hundreds of speculative calls.
            try:
                current = fetch_current_league(int(row['team_id']))
            except RuntimeError as exc:
                raise SportMonksError(str(exc)) from exc
            if current:
                row.update(current)
        if _clean(country) and row['league_country_name'] != _clean(country):
            continue
        if _clean(league) and row['league_name'] != _clean(league):
            continue
        rows.append(row)
        if len(rows) >= min(max(1, int(limit)), 50):
            break
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

