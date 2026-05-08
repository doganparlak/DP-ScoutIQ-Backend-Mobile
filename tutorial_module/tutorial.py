from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.orm import Session

from player_pool_module.player_pool import search_players


def _fallback_gyokeres() -> Dict[str, Any]:
    return {
        "name": "Viktor Gyökeres",
        "meta": {
            "gender": "Male",
            "age": 27,
            "nationality": "Sweden",
            "league": "Premier League",
            "team": "Arsenal",
            "height": 189,
            "weight": 90,
            "roles": ["Attacker"],
        },
        "stats": [
            {"metric": "Goals", "value": 24},
            {"metric": "Assists", "value": 8},
            {"metric": "Shots per 90", "value": 4.2},
            {"metric": "Progressive Carries", "value": 5.1},
            {"metric": "Aerial Duels Won %", "value": 48},
        ],
    }


def _content_to_player(content: Dict[str, Any]) -> Dict[str, Any]:
    name = content.get("player_name") or content.get("name") or "Viktor Gyökeres"
    roles = content.get("roles") or content.get("positions") or [content.get("position_name") or "Attacker"]
    stats = []
    skip = {
        "player_name",
        "player_name_norm",
        "name",
        "gender",
        "age",
        "nationality",
        "nationality_name",
        "league",
        "league_name",
        "league_name_norm",
        "team",
        "team_name",
        "team_name_norm",
        "height",
        "weight",
        "roles",
        "positions",
        "position_name",
    }

    for metric, value in content.items():
        if metric in skip:
            continue
        if isinstance(value, (int, float)):
            stats.append({"metric": metric, "value": value})
        elif isinstance(value, str):
            try:
                stats.append({"metric": metric, "value": float(value)})
            except ValueError:
                pass

    return {
        "name": name,
        "meta": {
            "gender": content.get("gender") or "Male",
            "age": content.get("age") or 27,
            "nationality": content.get("nationality") or content.get("nationality_name") or "Sweden",
            "league": content.get("league") or content.get("league_name") or "Premier League",
            "team": content.get("team") or content.get("team_name") or "Arsenal",
            "height": content.get("height") or 189,
            "weight": content.get("weight") or 90,
            "roles": [role for role in roles if role] or ["Attacker"],
        },
        "stats": stats,
    }


def tutorial_chat_response(db: Session) -> Dict[str, Any]:
    rows = search_players(db, {"name": "Viktor Gyökeres", "limit": 1})
    player = _content_to_player(rows[0]["content"]) if rows else _fallback_gyokeres()

    if player["name"].lower().replace("ö", "o") != "viktor gyokeres":
        player = _fallback_gyokeres()

    answer = (
        "A strong center-forward fit is Viktor Gyökeres. He gives you a powerful runner, "
        "box presence, pressing intensity, and enough mobility to lead the line in a high-pressing 4-3-3."
    )

    return {
        "response": answer,
        "data": {"players": [player]},
        "response_parts": [{"type": "text"}],
    }
