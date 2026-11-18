"""Deterministic post-match standout and development player selection."""
from typing import Any


def build_player_perspectives(
    teams: list[dict[str, Any]],
    lineups: list[dict[str, Any]],
    events: list[dict[str, Any]],
    include_locked: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    def find_metric(player: dict[str, Any], name: str) -> float | None:
        for rows in (player.get("categories") or {}).values():
            for metric in rows or []:
                if metric.get("name") == name:
                    try:
                        return float(metric.get("value"))
                    except (TypeError, ValueError):
                        return None
        return None

    selected: dict[str, list[dict[str, Any]]] = {}
    for team in teams:
        candidates = []
        for player in lineups:
            if player.get("team_id") != team.get("id"):
                continue
            minutes = find_metric(player, "Minutes Played") or 0
            rating = find_metric(player, "Rating")
            if minutes > 0 and rating is not None:
                player_events = [event for event in events if event.get("player_id") == player.get("player_id")]
                goals = sum(1 for event in player_events if str(event.get("type") or "").lower() == "goal")
                red_card = any("red" in str(event.get("type") or "").lower() for event in player_events)
                candidates.append({
                    "rating": rating,
                    "minutes": minutes,
                    "goals": goals,
                    "red_card": red_card,
                    "player": player,
                })
        rating_order = sorted(
            candidates,
            key=lambda item: (-item["rating"], -item["minutes"], str(item["player"].get("player_name") or "")),
        )
        scoring_order = sorted(
            [item for item in candidates if item["goals"] > 0],
            key=lambda item: (-item["goals"], -item["rating"], -item["minutes"]),
        )
        featured: list[dict[str, Any]] = []
        top_scorer = scoring_order[0] if scoring_order else None
        if top_scorer and not top_scorer["red_card"]:
            featured.append(top_scorer)
        for item in rating_order:
            if all(item["player"].get("player_id") != chosen["player"].get("player_id") for chosen in featured):
                featured.append(item)
            if len(featured) == 2:
                break
        featured_ids = {item["player"].get("player_id") for item in featured}
        worst_pool = [
            item for item in candidates
            if item["minutes"] > 30 and item["player"].get("player_id") not in featured_ids
        ]
        worst = min(
            worst_pool,
            key=lambda item: (item["rating"], -item["minutes"], str(item["player"].get("player_name") or "")),
            default=None,
        )
        chosen_players = [(item["player"], "featured") for item in featured]
        if worst:
            chosen_players.append((worst["player"], "development"))
        team_key = str(team.get("id"))
        selected[team_key] = [
            {"locked": True, "selection_type": selection_type}
            if not include_locked and selection_type == "featured" and index == 1
            else {
                "player_id": player.get("player_id"),
                "player_name": player.get("player_name"),
                "position_name": player.get("position_name"),
                "rating": find_metric(player, "Rating"),
                "selection_type": selection_type,
            }
            for index, (player, selection_type) in enumerate(chosen_players)
        ]
    return selected
