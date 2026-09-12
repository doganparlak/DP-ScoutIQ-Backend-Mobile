"""Enterprise player selection with concise mobile explanations."""
import os, re, json
from typing import Any

def build_player_perspectives(
    teams: list[dict[str, Any]],
    lineups: list[dict[str, Any]],
    events: list[dict[str, Any]],
    lang: str,
    strict: bool = False,
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

    def normalize_goal_or_assist_claims(text: str, player: dict[str, Any]) -> str:
        """Keep any stated goal/assist total aligned with the lineup metrics."""
        metrics = player.get("metrics") or {}
        goals = metrics.get("shooting", {}).get("Goals")
        assists = metrics.get("passing", {}).get("Assists")
        try:
            goals = int(float(goals))
        except (TypeError, ValueError):
            goals = None
        try:
            assists = int(float(assists))
        except (TypeError, ValueError):
            assists = None

        corrected = str(text or "")
        claims = (
            (goals, r"\b(\d+)\s+gol(?:[a-zçğıöşü']*)?\b(?!\s+(?:dönüş|beklent|oran|performans))"),
            (assists, r"\b(\d+)\s+asist(?:[a-zçğıöşü']*)?\b(?!\s+(?:verim|oran))"),
            (goals, r"\b(\d+)\s+goals?\b(?!\s+(?:conversion|expectancy|rate|performance))"),
            (assists, r"\b(\d+)\s+assists?\b(?!\s+(?:efficiency|rate))"),
        )
        for expected, pattern in claims:
            if expected is None:
                continue
            corrected = re.sub(
                pattern,
                lambda match: match.group(0).replace(match.group(1), str(expected), 1),
                corrected,
                flags=re.I,
            )
        return corrected

    def development_evidence(player: dict[str, Any]) -> dict[str, Any]:
        """Give the model an explicit weakness-first view of a development selection."""
        categories = player.get("categories") or {}
        errors: dict[str, Any] = {}
        for metric in categories.get("errors_discipline", []) or []:
            try:
                if float(metric.get("value")) > 0:
                    errors[metric.get("name")] = metric.get("value")
            except (TypeError, ValueError):
                continue
        low_efficiency_names = {
            "Shots On Target (%)",
            "Pass Accuracy (%)",
            "Cross Accuracy (%)",
            "Long Ball Accuracy (%)",
            "Dribble Accuracy (%)",
            "Duels Won (%)",
            "Aerials Won (%)",
        }
        low_efficiency: dict[str, Any] = {}
        for rows in categories.values():
            for metric in rows or []:
                name = metric.get("name")
                if name in low_efficiency_names and metric.get("value") is not None:
                    low_efficiency[name] = metric.get("value")
        return {
            "priority_errors_and_discipline": errors,
            "efficiency_metrics_to_assess_for_low_values": low_efficiency,
        }

    selected: dict[str, list[dict[str, Any]]] = {}
    compact: dict[str, Any] = {}
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
        compact_players = []
        selected[team_key] = []
        for player, selection_type in chosen_players:
            selected[team_key].append({
                "player_id": player.get("player_id"),
                "player_name": player.get("player_name"),
                "selection_type": selection_type,
                "text": "",
            })
            compact_player = {
                "player_id": player.get("player_id"),
                "player_name": player.get("player_name"),
                "selection_type": selection_type,
                "position": player.get("position_name"),
                "formation_field": player.get("formation_field"),
                "formation_position": player.get("formation_position"),
                "starter": player.get("starter"),
                "metrics": {
                    group: {metric.get("name"): metric.get("value") for metric in rows or []}
                    for group, rows in (player.get("categories") or {}).items() if rows
                },
                "expected_metrics": {
                    metric.get("name"): metric.get("value")
                    for metric in player.get("expected_metrics") or []
                },
                "authoritative_output": {
                    "Goals": find_metric(player, "Goals") or 0,
                    "Assists": find_metric(player, "Assists") or 0,
                },
                "events": [
                    {key: event.get(key) for key in ("type", "minute", "extra_minute", "info")}
                    for event in events
                    if event.get("player_id") == player.get("player_id")
                    or event.get("related_player_id") == player.get("player_id")
                ],
            }
            if selection_type == "development":
                compact_player["development_evidence"] = development_evidence(player)
            compact_players.append(compact_player)
        compact[team_key] = {"team_name": team.get("name"), "players": compact_players}

    for team_key, rows in selected.items():
        for index, row in enumerate(rows):
            player_data = compact[team_key]["players"][index]
            contribution = player_data["metrics"].get("contribution_impact", {})
            rating = contribution.get("Rating", "—")
            minutes = contribution.get("Minutes Played", "—")
            if row.get("selection_type") == "development":
                row["text"] = (f"{row['player_name']}, {minutes} dakikada {rating} puan aldı. Öne çıkanlar dışında, 30 dakikadan fazla oynayan takım arkadaşları arasında en düşük puana sahip olduğu için gelişim alanı olarak seçildi." if lang == "tr" else f"{row['player_name']} recorded a {rating} rating in {minutes} minutes. Among remaining teammates who played more than 30 minutes, this was the lowest rating, identifying the development selection.")
            else:
                row["text"] = (f"{row['player_name']}, {minutes} dakikada {rating} puan aldı. Takım içindeki gol ve puan sıralamasını temel alan seçimde, maçın öne çıkan 2 oyuncusundan biri olarak belirlendi." if lang == "tr" else f"{row['player_name']} recorded a {rating} rating in {minutes} minutes. The team's goal and rating rankings placed this performance among the 2 standout selections for the match.")
    if not any(selected.values()):
        return selected
    if not os.getenv("OPENAI_API_KEY"):
        if strict:
            raise ValueError("Player analysis is unavailable")
        return selected
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"))
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"], temperature=0.2, timeout=180, max_retries=1).invoke([
            (
                "system",
                "You are ScoutWise's senior player-performance analyst. Return only valid JSON keyed by the supplied team IDs. Each value must preserve the supplied three-player order and contain exactly player_id and text. Write one focused, evidence-led interpretation of 25-35 words per player in the requested language, considering the player's position. Write every numerical value with digits, never words: use forms such as 3 fouls, 4 duels, 70 minutes, 12 of 14 passes, and 42%. In Turkish put the percent sign before the number, for example %42. The supplied metrics and authoritative_output object are the only factual source. Goals and assists must match authoritative_output exactly; never infer a personal total from a team-goal sequence, an event order, or any other field. Begin naturally with the player's name and the central meaning of the performance; never open with formulaic constructions such as 'a centre-back who played 71 minutes', 'playing 90 minutes as a midfielder', or their equivalents. Minutes and position may appear later only when analytically useful. For selection_type=featured, write an exclusively positive assessment: emphasize the player's highest and most influential metric values, scoring or creative output, efficiency, rating, position-specific strengths, and positive match impact. Do not include any negative sentence, limitation, weakness, loss, error, missed chance, low efficiency, adverse contrast, or a transition such as 'however'. For selection_type=development, write an exclusively weakness-focused diagnosis. Start with the central deficiency; prioritize supplied development_evidence.priority_errors_and_discipline, then low efficiency percentages and low position-relevant output. Discuss concrete negatives such as cards, penalties conceded, fouls, errors, possession losses, duels or aerial duels lost, missed chances, inaccurate actions, and weak conversion. Do not praise, soften, balance, or acknowledge any strength or positive evidence. Never cite a successful action or favorable ratio in a development assessment, even if it is supplied in the data; include a metric only when it directly demonstrates a weakness. Never mention leadership, captaincy, experience, security, successful passes, successful clearances, successful interceptions, successful tackles, high volume, good contribution, resilience, or use transitions such as 'however', 'although', 'despite', 'while', 'but', 'yine de', 'ancak', 'buna karşın', or 'rağmen'. Do not turn mere minutes played, captaincy, or involvement volume into a positive statement. In Turkish use natural football terminology: write 'ikili mücadele', never the untranslated word 'duel'. Select only supported weaknesses and explain why they matter for the player's position. Combine rating, minutes, role, events, volume and efficiency where they support the assigned selection type. Never invent actions, tactics, causation, or metrics. Use clear sentences with no headings, markdown, bullets, recommendations, or raw category names.",
            ),
            ("human", f"Language: {language}\nSelected standout and development-area players with match data:\n{json.dumps(compact, ensure_ascii=False, default=str)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        object_start = raw.find("{")
        if object_start < 0:
            raise ValueError("Player perspective response did not contain a JSON object")
        parsed, _ = json.JSONDecoder().raw_decode(raw[object_start:])
        if not isinstance(parsed, dict):
            raise ValueError("Player perspective response root was not a JSON object")
        for team_key, rows in selected.items():
            generated = parsed.get(team_key) or []
            by_id = {str(item.get("player_id")): str(item.get("text") or "").strip() for item in generated}
            for index, row in enumerate(rows):
                generated_text = by_id.get(str(row.get("player_id")))
                player_data = compact[team_key]["players"][index]
                if strict and not generated_text:
                    raise ValueError("Incomplete player analysis")
                if generated_text:
                    row["text"] = normalize_goal_or_assist_claims(generated_text, player_data)
        return selected
    except Exception as exc:
        if strict:
            raise ValueError("Player analysis could not be generated") from None
        print(f"[enterprise_match_report] event=player_perspective_fallback error={exc}")
        return selected
