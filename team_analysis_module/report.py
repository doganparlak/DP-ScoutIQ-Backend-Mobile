from __future__ import annotations

import os
import json
import re
from collections import defaultdict
from typing import Any

import requests


SPORTMONKS_BASE_URL = os.getenv(
    "SPORTMONKS_BASE_URL", "https://api.sportmonks.com/v3/football"
).rstrip("/")
MATCH_REPORT_VERSION = 44

MATCH_REPORT_INCLUDE = ";".join(
    [
        "participants",
        "league.country",
        "season",
        "stage",
        "round",
        "venue",
        "state",
        "scores",
        "formations",
        "coaches",
        "referees",
        "periods.type",
        "periods.statistics.type",
        "statistics.type",
        "lineups.player",
        "lineups.position",
        "lineups.details.type",
        "events.type",
        "metadata",
        "weatherReport",
        "xGFixture.type",
        "lineups.xGLineup.type",
        "pressure",
        "trends",
        "ballCoordinates",
    ]
)

# These are the exact conceptual groups used by the player scouting report.
METRIC_GROUPS = (
    "contribution_impact",
    "goalkeeping",
    "shooting",
    "passing",
    "set_pieces",
    "defending",
    "errors_discipline",
)

TEAM_METRIC_CATEGORY: dict[str, tuple[str, str]] = {
    "Attacks": ("contribution_impact", "Attacks"),
    "Dangerous Attacks": ("contribution_impact", "Dangerous Attacks"),
    "Ball Possession %": ("contribution_impact", "Ball Possession %"),
    "Shots Total": ("shooting", "Shots Total"),
    "Shots On Target": ("shooting", "Shots On Target"),
    "Goals": ("shooting", "Goals"),
    "Expected Goals (xG)": ("shooting", "Expected Goals"),
    "Expected Goals on Target (xGoT)": ("shooting", "Expected Goals On Target"),
    "Shooting Performance (SP)": ("shooting", "Shooting Performance"),
    "Shots Insidebox": ("shooting", "Shots Insidebox"),
    "Shots Outsidebox": ("shooting", "Shots Outsidebox"),
    "Goal Attempts": ("shooting", "Goal Attempts"),
    "Passes": ("passing", "Passes"),
    "Successful Passes": ("passing", "Accurate Passes"),
    "Successful Passes Percentage": ("passing", "Accurate Passes (%)"),
    "Long Passes": ("passing", "Long Balls"),
    "Successful Long Passes": ("passing", "Long Balls Won"),
    "Successful Long Passes Percentage": ("passing", "Long Balls Won (%)"),
    "Total Crosses": ("passing", "Total Crosses"),
    "Accurate Crosses": ("passing", "Accurate Crosses"),
    "Key Passes": ("passing", "Key Passes"),
    "Corners": ("set_pieces", "Corners"),
    "Goal Kicks": ("set_pieces", "Goal Kicks"),
    "Free Kicks": ("set_pieces", "Free Kicks"),
    "Throwins": ("set_pieces", "Throwins"),
    "Penalties Scored": ("set_pieces", "Penalties Scored"),
    "Penalties Missed": ("set_pieces", "Penalties Missed"),
    "Penalties Won": ("set_pieces", "Penalties Won"),
    "Penalties Committed": ("set_pieces", "Penalties Committed"),
    "Penalties Saved": ("set_pieces", "Penalties Saved"),
    "Big Chances Created": ("contribution_impact", "Big Chances Created"),
    "Dribble Attempts": ("contribution_impact", "Dribble Attempts"),
    "Successful Dribbles": ("contribution_impact", "Successful Dribbles"),
    "Successful Dribbles Percentage": ("contribution_impact", "Dribble Accuracy (%)"),
    "Saves": ("defending", "Saves"),
    "Tackles": ("defending", "Tackles"),
    "Shots Blocked": ("defending", "Blocked Shots"),
    "Interceptions": ("defending", "Interceptions"),
    "Duels Won": ("defending", "Duels Won"),
    "Successful Headers": ("defending", "Successful Headers"),
    "Ball Safe": ("passing", "Ball Safe"),
    "Shots Off Target": ("errors_discipline", "Shots Off Target"),
    "Big Chances Missed": ("errors_discipline", "Big Chances Missed"),
    "Fouls": ("errors_discipline", "Fouls"),
    "Offsides": ("errors_discipline", "Offsides"),
    "Yellowcards": ("errors_discipline", "Yellow Cards"),
    "Redcards": ("errors_discipline", "Red Cards"),
}

PLAYER_METRIC_CATEGORY: dict[str, tuple[str, str]] = {
    **TEAM_METRIC_CATEGORY,
    "Minutes Played": ("contribution_impact", "Minutes Played"),
    "Touches": ("contribution_impact", "Touches"),
    "Rating": ("contribution_impact", "Rating"),
    "Captain": ("contribution_impact", "Captain"),
    "Fouls Drawn": ("contribution_impact", "Fouls Drawn"),
    "Assists": ("passing", "Assists"),
    "Accurate Passes": ("passing", "Accurate Passes"),
    "Accurate Passes Percentage": ("passing", "Accurate Passes (%)"),
    "Long Balls Won": ("passing", "Long Balls Won"),
    "Long Balls Won Percentage": ("passing", "Long Balls Won (%)"),
    "Passes In Final Third": ("passing", "Passes In Final Third"),
    "Backward Passes": ("passing", "Backward Passes"),
    "Long Balls": ("passing", "Long Balls"),
    "Successful Crosses Percentage": ("passing", "Successful Crosses Percentage"),
    "Chances Created": ("contribution_impact", "Chances Created"),
    "Blocked Shots": ("defending", "Blocked Shots"),
    "Saves Insidebox": ("goalkeeping", "Saves Insidebox"),
    "Punches": ("goalkeeping", "Punches"),
    "Good High Claim": ("goalkeeping", "Good High Claim"),
    "Tackles Won": ("defending", "Tackles Won"),
    "Tacles Won Percentage": ("defending", "Tackles Won (%)"),
    "Clearances": ("defending", "Clearances"),
    "Ball Recovery": ("defending", "Ball Recovery"),
    "Aerials": ("defending", "Aerials"),
    "Aerials Won": ("defending", "Aerials Won"),
    "Aerials Won Percentage": ("defending", "Aerials Won (%)"),
    "Total Duels": ("defending", "Total Duels"),
    "Duels Won Percentage": ("defending", "Duels Won (%)"),
    "Goals Conceded": ("errors_discipline", "Goals Conceded"),
    "Goalkeeper Goals Conceded": ("errors_discipline", "Goals Conceded"),
    "Aerials Lost": ("errors_discipline", "Aerials Lost"),
    "Duels Lost": ("errors_discipline", "Duels Lost"),
    "Dispossessed": ("errors_discipline", "Dispossessed"),
    "Dribbled Past": ("errors_discipline", "Dribbled Past"),
    "Possession Lost": ("errors_discipline", "Possession Lost"),
    "Error Lead To Goal": ("errors_discipline", "Error Lead To Goal"),
    "Error Lead To Shot": ("errors_discipline", "Error Lead To Shot"),
    "Yellowcards": ("errors_discipline", "Yellow Cards"),
    "Redcards": ("errors_discipline", "Red Cards"),
}

# Percentages that have a known numerator and denominator must be derived from
# their aggregated components, never averaged as independently rounded match
# percentages.
DERIVED_PERCENTAGE_METRICS: dict[str, tuple[str, str]] = {
    "Shots On Target (%)": ("Shots On Target", "Shots Total"),
    "Goal Conversion (%)": ("Goals", "Shots Total"),
    "On Target Goal Conversion (%)": ("Goals", "Shots On Target"),
    "Assist Efficiency (%)": ("Assists", "Key Passes"),
    "Dribble Accuracy (%)": ("Successful Dribbles", "Dribble Attempts"),
    "Tackles Won (%)": ("Tackles Won", "Tackles"),
    "Aerials Won (%)": ("Aerials Won", "Aerials"),
    "Duels Won (%)": ("Duels Won", "Total Duels"),
    "Long Balls Won (%)": ("Long Balls Won", "Long Balls"),
    "Accurate Passes (%)": ("Accurate Passes", "Passes"),
    "Accurate Crosses (%)": ("Accurate Crosses", "Total Crosses"),
}


class MatchReportError(RuntimeError):
    pass


def _value(item: dict[str, Any]) -> Any:
    data = item.get("data") or {}
    return data.get("value") if isinstance(data, dict) else data


def _metric(name: str, value: Any, type_id: Any = None) -> dict[str, Any]:
    return {"name": name, "value": value, "type_id": type_id}


def _empty_categories() -> dict[str, list[dict[str, Any]]]:
    return {name: [] for name in METRIC_GROUPS}


def _categorize(
    rows: list[dict[str, Any]],
    mapping: dict[str, tuple[str, str]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    categories = _empty_categories()
    extras: list[dict[str, Any]] = []
    for row in rows:
        type_obj = row.get("type") or {}
        original_name = str(type_obj.get("name") or row.get("name") or row.get("type_id") or "")
        item = _metric(original_name, _value(row), row.get("type_id"))
        category = mapping.get(original_name)
        if category:
            group, canonical_name = category
            item["name"] = canonical_name
            item["source_name"] = original_name
            categories[group].append(item)
        else:
            extras.append(item)
    return categories, extras


def _numeric_metric(rows: list[dict[str, Any]], name: str) -> float | None:
    for row in rows:
        if row.get("name") != name:
            continue
        try:
            return float(row.get("value"))
        except (TypeError, ValueError):
            return None
    return None


def _upsert_percentage(
    rows: list[dict[str, Any]],
    name: str,
    numerator: float | None,
    denominator: float | None,
) -> None:
    if numerator is None or denominator is None or denominator <= 0:
        return
    rows[:] = [row for row in rows if row.get("name") != name]
    rows.append(_metric(name, round(numerator / denominator * 100, 1)))


def _derive_player_metrics(
    categories: dict[str, list[dict[str, Any]]],
    expected: list[dict[str, Any]],
) -> None:
    shooting = categories["shooting"]
    passing = categories["passing"]
    impact = categories["contribution_impact"]
    defending = categories["defending"]
    shots_total = _numeric_metric(shooting, "Shots Total")
    shots_on_target = _numeric_metric(shooting, "Shots On Target")
    goals = _numeric_metric(shooting, "Goals")
    expected_to_shooting = {
        "Expected Goals (xG)": "Expected Goals",
        "Expected Goals on Target (xGoT)": "Expected Goals On Target",
        "Shooting Performance (SP)": "Shooting Performance",
    }
    for expected_name, shooting_name in expected_to_shooting.items():
        value = _numeric_metric(expected, expected_name)
        if value is not None and _numeric_metric(shooting, shooting_name) is None:
            shooting.append(_metric(shooting_name, value))
    _upsert_percentage(shooting, "Shots On Target (%)", shots_on_target, shots_total)
    _upsert_percentage(shooting, "Goal Conversion (%)", goals, shots_total)
    _upsert_percentage(shooting, "On Target Goal Conversion (%)", goals, shots_on_target)
    _upsert_percentage(
        passing,
        "Assist Efficiency (%)",
        _numeric_metric(passing, "Assists"),
        _numeric_metric(passing, "Key Passes"),
    )
    _upsert_percentage(
        impact,
        "Dribble Accuracy (%)",
        _numeric_metric(impact, "Successful Dribbles"),
        _numeric_metric(impact, "Dribble Attempts"),
    )
    _upsert_percentage(
        defending,
        "Tackles Won (%)",
        _numeric_metric(defending, "Tackles Won"),
        _numeric_metric(defending, "Tackles"),
    )
    _upsert_percentage(
        defending,
        "Aerials Won (%)",
        _numeric_metric(defending, "Aerials Won"),
        _numeric_metric(defending, "Aerials"),
    )
    _upsert_percentage(
        defending,
        "Duels Won (%)",
        _numeric_metric(defending, "Duels Won"),
        _numeric_metric(defending, "Total Duels"),
    )
    _upsert_percentage(
        passing,
        "Long Balls Won (%)",
        _numeric_metric(passing, "Long Balls Won"),
        _numeric_metric(passing, "Long Balls"),
    )
    _upsert_percentage(
        passing,
        "Accurate Passes (%)",
        _numeric_metric(passing, "Accurate Passes"),
        _numeric_metric(passing, "Passes"),
    )
    _upsert_percentage(
        passing,
        "Accurate Crosses (%)",
        _numeric_metric(passing, "Accurate Crosses"),
        _numeric_metric(passing, "Total Crosses"),
    )
    _upsert_percentage(
        shooting,
        "Shot Quality (%)",
        _numeric_metric(expected, "Expected Goals (xG)"),
        shots_total,
    )
    _upsert_percentage(
        shooting,
        "On Target Shot Quality (%)",
        _numeric_metric(expected, "Expected Goals on Target (xGoT)"),
        shots_on_target,
    )


def _derive_team_percentages(categories: dict[str, list[dict[str, Any]]]) -> None:
    shooting = categories["shooting"]
    passing = categories["passing"]
    _upsert_percentage(
        shooting,
        "Shots On Target (%)",
        _numeric_metric(shooting, "Shots On Target"),
        _numeric_metric(shooting, "Shots Total"),
    )
    _upsert_percentage(
        passing,
        "Accurate Crosses (%)",
        _numeric_metric(passing, "Accurate Crosses"),
        _numeric_metric(passing, "Total Crosses"),
    )


def _team_stat_index(team: dict[str, Any]) -> dict[str, float]:
    index: dict[str, float] = {}
    for rows in team.get("categories", {}).values():
        for item in rows:
            try:
                index[str(item.get("name"))] = float(item.get("value"))
            except (TypeError, ValueError):
                continue
    for item in team.get("extra_metrics", []):
        try:
            index[str(item.get("name"))] = float(item.get("value"))
        except (TypeError, ValueError):
            continue
    return index


def _build_summary(teams: list[dict[str, Any]], events: list[dict[str, Any]], lang: str) -> list[str]:
    if len(teams) != 2:
        return []
    home = next((team for team in teams if team.get("location") == "home"), teams[0])
    away = next((team for team in teams if team.get("location") == "away"), teams[1])
    hi, ai = _team_stat_index(home), _team_stat_index(away)
    home_name, away_name = home.get("name"), away.get("name")
    if lang == "tr":
        points = [
            f"{home_name} toplam şutlarda {hi.get('Shots Total', 0):g}-{ai.get('Shots Total', 0):g} üstünlük kurdu.",
            f"Topa sahip olma dağılımı {home_name} %{hi.get('Ball Possession %', 0):g}, {away_name} %{ai.get('Ball Possession %', 0):g} olarak gerçekleşti.",
            f"Pas başarısı {home_name} için %{hi.get('Accurate Passes (%)', 0):g}, {away_name} için %{ai.get('Accurate Passes (%)', 0):g} oldu.",
        ]
        red_events = [event for event in events if str(event.get("type", "")).lower() == "redcard"]
        if red_events:
            points.append(f"Maçta {len(red_events)} kırmızı kart olayı kaydedildi ve oyun dengesi değişti.")
        return points
    points = [
        f"{home_name} led total shots {hi.get('Shots Total', 0):g}-{ai.get('Shots Total', 0):g}.",
        f"Possession finished at {hi.get('Ball Possession %', 0):g}% for {home_name} and {ai.get('Ball Possession %', 0):g}% for {away_name}.",
        f"Pass accuracy was {hi.get('Accurate Passes (%)', 0):g}% for {home_name} and {ai.get('Accurate Passes (%)', 0):g}% for {away_name}.",
    ]
    return points


def _build_scoutwise_perspective(report: dict[str, Any], lang: str) -> list[str]:
    teams = report.get("teams") or []
    events = report.get("events") or []
    pressure = report.get("pressure") or []
    if not pressure:
        return []
    compact = {
        "teams": [
            {
                "name": team.get("name"),
                "location": team.get("location"),
                "metrics": _team_stat_index(team),
            }
            for team in teams
        ],
        "events": [
            {
                "minute": event.get("minute"),
                "team": event.get("team_name"),
                "type": event.get("type"),
                "player": event.get("player_name"),
                "result": event.get("result"),
            }
            for event in events
        ],
        "pressure": {
            str(team.get("name")): [
                {"minute": row.get("minute"), "value": row.get("value")}
                for row in pressure
                if row.get("team_id") == team.get("id")
            ]
            for team in teams
        },
    }
    fallback = [str(item).strip() for item in (report.get("summary") or []) if str(item).strip()][:3]
    if not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv(
            "OPENAI_MATCH_REPORT_MODEL",
            os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"),
        )
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=model,
            api_key=os.environ["OPENAI_API_KEY"],
            temperature=0.25,
        ).invoke(
            [
                (
                    "system",
                    "You are ScoutWise Enterprise's football match analyst. Return exactly two lines, each no longer than 55 words. Line 1 must chronologically interpret the first half (0-45+); line 2 must interpret the second half (46-full time), including the result. Never expose raw pressure-index numbers. Translate them into match-relative qualitative levels such as low, moderate, high, very high, rising, falling, or dominant, tied to concrete minute ranges. Relate goals, red cards, and substitutions to nearby pressure changes using observational wording such as 'coincided with', never proven causation. Do not add headings, bullets, markdown, caveats, or recommendations.",
                ),
                (
                    "human",
                    f"Language: {language}\nMatch data:\n{json.dumps(compact, ensure_ascii=False, default=str)}",
                ),
            ]
        )
        raw = str(response.content or "").strip()
        lines = [re.sub(r"^\s*[-•]\s*", "", line).strip() for line in raw.splitlines() if line.strip()]
        if len(lines) < 2:
            lines = [part.strip() for part in re.split(r"(?<=[.!?])\s+", raw) if part.strip()]
        return lines[:2] or fallback[:2]
    except Exception as exc:
        print(f"[enterprise_match_report] event=perspective_fallback error={exc}")
        return fallback


def _build_regional_play_perspective(report: dict[str, Any], lang: str) -> str:
    rows = report.get("ball_coordinates") or []
    counts = [0] * 9
    for row in rows:
        try:
            x = min(0.999999, max(0.0, float(row.get("x") or 0)))
            y = min(0.999999, max(0.0, float(row.get("y") or 0)))
            counts[int(y * 3) * 3 + int(x * 3)] += 1
        except (TypeError, ValueError):
            continue
    total = sum(counts) or 1
    zones = [round(count / total * 100, 1) for count in counts]
    valid_rows = []
    for row in rows:
        try:
            valid_rows.append((float(row.get("x") or 0), float(row.get("y") or 0)))
        except (TypeError, ValueError):
            continue
    valid_total = len(valid_rows) or 1
    upper_wide_band = round(sum(zones[index] for index in (0, 1, 2)), 1)
    central_band = round(sum(zones[index] for index in (3, 4, 5)), 1)
    lower_wide_band = round(sum(zones[index] for index in (6, 7, 8)), 1)
    concentration_margin = 5.0
    upper_wide_is_clear = upper_wide_band - central_band >= concentration_margin
    lower_wide_is_clear = lower_wide_band - central_band >= concentration_margin
    central_is_clear = (
        central_band - upper_wide_band >= concentration_margin
        and central_band - lower_wide_band >= concentration_margin
    )
    if central_is_clear:
        channel_concentration = "central_area_more_intense"
    elif upper_wide_is_clear and lower_wide_is_clear:
        channel_concentration = "both_wide_areas_more_intense"
    elif upper_wide_is_clear or lower_wide_is_clear:
        channel_concentration = "one_flank_more_intense"
    else:
        channel_concentration = "balanced_no_clear_advantage"
    semantic = {
        "upper_wide_band_z1_z2_z3_pct": upper_wide_band,
        "central_band_z4_z5_z6_pct": central_band,
        "lower_wide_band_z7_z8_z9_pct": lower_wide_band,
        "channel_concentration": channel_concentration,
        "minimum_clear_difference_percentage_points": concentration_margin,
        "middle_field_band_z2_z5_z8_pct": round(sum(zones[index] for index in (1, 4, 7)), 1),
        "outer_end_bands_z1_z4_z7_and_z3_z6_z9_pct": round(sum(zones[index] for index in (0, 3, 6, 2, 5, 8)), 1),
        "penalty_area_vicinities_combined_pct": round(sum(1 for x, y in valid_rows if (x <= 16.5 / 105 or x >= 88.5 / 105) and 13.84 / 68 <= y <= 54.16 / 68) / valid_total * 100, 1),
    }
    fallback = (
        "Kaydedilen top konumları, sahanın belirli bölgelerinde daha yoğun bir dağılım gösterdi."
        if lang == "tr"
        else "Recorded ball locations showed a greater concentration in specific pitch zones."
    )
    if not rows or not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv(
            "OPENAI_MATCH_REPORT_MODEL",
            os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"),
        )
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=model,
            api_key=os.environ["OPENAI_API_KEY"],
            temperature=0.2,
        ).invoke(
            [
                (
                    "system",
                    "You are ScoutWise Enterprise's football match analyst. Write exactly one concise sentence, at most 45 words, interpreting the recorded ball-location pattern. Treat Z1+Z2+Z3, Z4+Z5+Z6, and Z7+Z8+Z9 as three equal-area horizontal bands. Obey the supplied channel_concentration classification exactly. central_area_more_intense means the centre exceeds both wide areas by at least five percentage points. both_wide_areas_more_intense means both wide areas exceed the centre by at least five points. one_flank_more_intense means only one wide area exceeds the centre by at least five points, so describe concentration in one flank rather than saying both wings dominated. balanced_no_clear_advantage means no clear centre-versus-wide superiority should be stated. Never compare the combined six wide zones directly with the three central zones. Use penalty_area_vicinities_combined_pct when describing concentration near the penalty areas; never translate the outer field bands generically as 'ends' or 'tips'. Compare the supported patterns and describe the match's regional play structure and use of space. State only what the distribution positively indicates; never add a limitation, uncertainty, capability disclaimer, or a phrase such as 'does not prove', 'cannot determine', 'alone is insufficient', or an equivalent. In Turkish, never use 'kanal' for this comparison; use natural football terms such as 'merkez', 'geniş alanlar', 'kanat bölgeleri', 'bir kanat bölgesi', 'alan kullanımı', and 'ceza alanlarına yakın bölgeler'. Never use 'uçlar', 'coğrafya', or their equivalents. Prefer qualitative football language over numbers and zone IDs. Never mention K1, K2, goal names/labels, left or right goal, team possession, attack direction, defensive thirds, attacking thirds, or third zones. Do not add headings, bullets, markdown, or recommendations.",
                ),
                (
                    "human",
                    f"Language: {language}\nZones in visual row-major order Z1-Z9 (top-left to bottom-right), percentages: {zones}\nSemantic spatial aggregates: {json.dumps(semantic, ensure_ascii=False)}",
                ),
            ]
        )
        value = re.sub(r"\s+", " ", str(response.content or "")).strip()
        return value or fallback
    except Exception as exc:
        print(f"[enterprise_match_report] event=regional_perspective_fallback error={exc}")
        return fallback


def _build_team_analysis_perspectives(
    teams: list[dict[str, Any]],
    period_teams: dict[str, list[dict[str, Any]]],
    lang: str,
) -> dict[str, str]:
    scopes = {"overall": teams, **period_teams}
    compact: dict[str, Any] = {}
    for scope, scope_teams in scopes.items():
        compact[scope] = {}
        for team in scope_teams:
            team_values = {
                group: {item.get("name"): item.get("value") for item in rows}
                for group, rows in (team.get("categories") or {}).items()
                if rows and group != "goalkeeping"
            }
            if scope == "overall" and team.get("expected_metrics"):
                team_values["expected"] = {
                    item.get("name"): item.get("value")
                    for item in team.get("expected_metrics") or []
                }
            compact[scope][team.get("name")] = team_values
    fallback_label = "Genel ve yarı bazındaki değerler, iki takımın bu alandaki performans farklarını gösterir."
    fallback = {
        group: fallback_label
        for group in (
            "contribution_impact", "shooting", "passing", "set_pieces",
            "defending", "errors_discipline", "expected",
        )
    }
    if not os.getenv("OPENAI_API_KEY"):
        return fallback


    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv(
            "OPENAI_MATCH_REPORT_MODEL",
            os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"),
        )
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=model,
            api_key=os.environ["OPENAI_API_KEY"],
            temperature=0.2,
        ).invoke(
            [
                (
                    "system",
                    "You are ScoutWise Enterprise's senior football match analyst. Return only one valid JSON object keyed by category, never by scope. For every available category, write one stable, concise, evidence-led analysis of 50-75 words in the requested language. Use only the most diagnostic statistics explicitly and explain their meaning rather than producing a list. Compare overall, first-half, and second-half values together, retaining only the clearest change after halftime, volume-versus-efficiency gap, or contradiction between related metrics. Prefer 2-3 direct sentences with no repetition. The same analysis is displayed in all period switch states. Never invent causation, tactical intent, attack direction, or unavailable facts; phrase relationships as evidence or indications, not certainty. Do not use markdown or headings.",
                ),
                (
                    "human",
                    f"Language: {language}\nScope/category team metrics:\n{json.dumps(compact, ensure_ascii=False, default=str)}",
                ),
            ]
        )
        raw = str(response.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
        parsed = json.loads(raw)
        available_groups = {
            group
            for scope_data in compact.values()
            for team_data in scope_data.values()
            for group in team_data
        }
        result: dict[str, str] = {}
        for group in available_groups:
            value = str(parsed.get(group) or "").strip()
            result[group] = value or fallback[group]
        return result
    except Exception as exc:
        print(f"[enterprise_match_report] event=team_perspective_fallback error={exc}")
        return fallback
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv(
            "OPENAI_MATCH_REPORT_MODEL",
            os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"),
        )
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=model,
            api_key=os.environ["OPENAI_API_KEY"],
            temperature=0.2,
        ).invoke(
            [
                (
                    "system",
                    "You are ScoutWise Enterprise's football match analyst. Write exactly one concise sentence, at most 45 words, interpreting the recorded ball-location pattern using these rules: concentration in Z2/Z5/Z8 means play stayed around the middle-field band; concentration in Z1/Z4/Z7 or Z3/Z6/Z9 means play reached the more dangerous areas nearer either end more often; concentration in Z1/Z2/Z3 and Z7/Z8/Z9 means the wide channels were used more; concentration in Z4/Z5/Z6 means the central channel was used more. Compare these patterns and describe the match's regional play structure and use of space. Use natural football-analysis terminology such as 'alan kullanımı', 'oyunun merkezi', 'geniş alanlar', 'kanatlar', or their equivalents in the requested language. Never use 'coğrafya' or 'geography'. Prefer qualitative football language over numbers and zone IDs. Never mention K1, K2, goal names/labels, left or right goal, team possession, attacking direction, defensive thirds, or attacking thirds. Do not add headings, bullets, markdown, or recommendations.",
                ),
                (
                    "human",
                    f"Language: {language}\nZones in visual row-major order Z1-Z9 (top-left to bottom-right), percentages: {zones}\nSemantic spatial aggregates: {json.dumps(semantic, ensure_ascii=False)}",
                ),
            ]
        )
        value = re.sub(r"\s+", " ", str(response.content or "")).strip()
        return value or fallback
    except Exception as exc:
        print(f"[enterprise_match_report] event=regional_perspective_fallback error={exc}")
        return fallback


def _build_player_analysis_perspectives(
    teams: list[dict[str, Any]],
    lineups: list[dict[str, Any]],
    events: list[dict[str, Any]],
    lang: str,
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
                row["text"] = (
                    f"{row['player_name']}, {rating} rating ile {minutes} dakikalık performansında takımının daha sınırlı kalan isimlerinden biri oldu. Hata ve disiplin göstergeleriyle düşük kalan verimlilik değerleri, mevki sorumlulukları içindeki temel gelişim alanlarını oluşturdu."
                    if lang == "tr"
                    else f"{row['player_name']} recorded a {rating} rating across {minutes} minutes and produced one of the team's more limited performances. Lower output and error indicators within the positional role define the main development area."
                )
            else:
                row["text"] = (
                    f"{row['player_name']}, {rating} rating ile {minutes} dakika boyunca takımının öne çıkan performanslarından birini verdi. Mevki rolündeki yüksek katkı değerleri ve maç içindeki etkili aksiyonları bu seçimi destekledi."
                    if lang == "tr"
                    else f"{row['player_name']} delivered one of the team's standout displays with a {rating} rating across {minutes} minutes. Strong contribution values and influential actions within the positional role support the selection."
                )
    if not any(selected.values()) or not os.getenv("OPENAI_API_KEY"):
        return selected
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"))
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"], temperature=0.2).invoke([
            (
                "system",
                "You are ScoutWise Enterprise's senior player-performance analyst. Return only valid JSON keyed by the supplied team IDs. Each value must preserve the supplied three-player order and contain exactly player_id and text. Write one focused, evidence-led interpretation of 45-65 words per player in the requested language, considering the player's position. Write every numerical value with digits, never words: use forms such as 3 fouls, 4 duels, 70 minutes, 12 of 14 passes, and 42%. In Turkish put the percent sign before the number, for example %42. The supplied metrics and authoritative_output object are the only factual source. Goals and assists must match authoritative_output exactly; never infer a personal total from a team-goal sequence, an event order, or any other field. Begin naturally with the player's name and the central meaning of the performance; never open with formulaic constructions such as 'a centre-back who played 71 minutes', 'playing 90 minutes as a midfielder', or their equivalents. Minutes and position may appear later only when analytically useful. For selection_type=featured, write an exclusively positive assessment: emphasize the player's highest and most influential metric values, scoring or creative output, efficiency, rating, position-specific strengths, and positive match impact. Do not include any negative sentence, limitation, weakness, loss, error, missed chance, low efficiency, adverse contrast, or a transition such as 'however'. For selection_type=development, write an exclusively weakness-focused diagnosis. Start with the central deficiency; prioritize supplied development_evidence.priority_errors_and_discipline, then low efficiency percentages and low position-relevant output. Discuss concrete negatives such as cards, penalties conceded, fouls, errors, possession losses, duels or aerial duels lost, missed chances, inaccurate actions, and weak conversion. Do not praise, soften, balance, or acknowledge any strength or positive evidence. Never cite a successful action or favorable ratio in a development assessment, even if it is supplied in the data; include a metric only when it directly demonstrates a weakness. Never mention leadership, captaincy, experience, security, successful passes, successful clearances, successful interceptions, successful tackles, high volume, good contribution, resilience, or use transitions such as 'however', 'although', 'despite', 'while', 'but', 'yine de', 'ancak', 'buna karşın', or 'rağmen'. Do not turn mere minutes played, captaincy, or involvement volume into a positive statement. In Turkish use natural football terminology: write 'ikili mücadele', never the untranslated word 'duel'. Select only supported weaknesses and explain why they matter for the player's position. Combine rating, minutes, role, events, volume and efficiency where they support the assigned selection type. Never invent actions, tactics, causation, or metrics. Use clear sentences with no headings, markdown, bullets, recommendations, or raw category names.",
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
                if generated_text:
                    row["text"] = normalize_goal_or_assist_claims(generated_text, player_data)
        return selected
    except Exception as exc:
        print(f"[enterprise_match_report] event=player_perspective_fallback error={exc}")
        return selected


def _build_team_deep_analyses(report: dict[str, Any], lang: str) -> dict[str, list[dict[str, str]]]:
    teams = report.get("teams") or []
    period_teams = report.get("period_teams") or {}
    lineups = report.get("lineups") or []
    events = report.get("events") or []
    pressure = report.get("pressure") or []

    def metric_map(team: dict[str, Any]) -> dict[str, Any]:
        values = {
            group: {item.get("name"): item.get("value") for item in rows or []}
            for group, rows in (team.get("categories") or {}).items()
            if rows
        }
        if team.get("expected_metrics"):
            values["expected"] = {
                item.get("name"): item.get("value")
                for item in team.get("expected_metrics") or []
            }
        return values

    compact: dict[str, Any] = {"match": report.get("fixture"), "teams": {}}
    fallback_headers_tr = ["Oyun Kimliği", "Üretim ve Verimlilik", "Kadro Kullanımı", "Maçın Kırılma Anları", "Maç Yönetimi"]
    fallback_headers_en = ["Game Identity", "Production and Efficiency", "Squad Usage", "Turning Points", "Game Management"]
    fallback: dict[str, list[dict[str, str]]] = {}
    for team in teams:
        team_id = team.get("id")
        team_key = str(team_id)
        team_pressure = [row for row in pressure if row.get("team_id") == team_id]
        pressure_by_half: dict[str, Any] = {}
        for half, start, end in (("first_half", 0, 45), ("second_half", 46, 130)):
            rows = [row for row in team_pressure if start <= int(row.get("minute") or 0) <= end]
            values = [float(row.get("value") or 0) for row in rows]
            pressure_by_half[half] = {
                "average": round(sum(values) / len(values), 2) if values else 0,
                "maximum": round(max(values), 2) if values else 0,
                "peak_minutes": [
                    row.get("minute") for row in sorted(rows, key=lambda item: float(item.get("value") or 0), reverse=True)[:3]
                ],
            }
        team_lineups = []
        for player in lineups:
            if player.get("team_id") != team_id:
                continue
            contribution = {
                item.get("name"): item.get("value")
                for item in (player.get("categories") or {}).get("contribution_impact", [])
            }
            try:
                minutes = float(contribution.get("Minutes Played") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0:
                continue
            team_lineups.append({
                "player_id": player.get("player_id"),
                "name": player.get("player_name"),
                "position": player.get("position_name"),
                "starter": player.get("starter"),
                "minutes": minutes,
                "rating": contribution.get("Rating"),
                "metrics": metric_map(player),
            })
        scoped_periods = {}
        for scope, scoped_teams in period_teams.items():
            scoped_team = next((item for item in scoped_teams if item.get("id") == team_id), None)
            scoped_periods[scope] = metric_map(scoped_team or {})
        compact["teams"][team_key] = {
            "team_name": team.get("name"),
            "opponent": next((item.get("name") for item in teams if item.get("id") != team_id), None),
            "formation": team.get("formation"),
            "overall_metrics": metric_map(team),
            "period_metrics": scoped_periods,
            "players_used": team_lineups,
            "events": [event for event in events if event.get("team_id") == team_id],
            **({"pressure_summary": pressure_by_half} if pressure else {}),
        }
        headers = fallback_headers_tr if lang == "tr" else fallback_headers_en
        fallback[team_key] = [
            {"header": header, "text": ("Bu alan için maç verileri yeniden değerlendiriliyor." if lang == "tr" else "Match data is being reassessed for this area."), "tone": "neutral"}
            for header in headers
        ]
    if not teams or not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"))
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"], temperature=0.25).invoke([
            (
                "system",
                "You are ScoutWise Enterprise's senior team-performance analyst, not a match-timeline narrator. Return only valid JSON keyed by the supplied team IDs. Each team value must be an array of exactly five objects with exactly header, text, and tone. tone must be exactly positive, negative, or neutral. Assign positive when the central conclusion is a strength, successful effect, superiority, or effective response; negative when it is a weakness, inefficiency, vulnerability, deterioration, or failed conversion; neutral when it is genuinely balanced or mixed. Classify the central conclusion, not isolated sentences. Select five distinct, dynamic headers from the evidence; never reuse a fixed template or generic headings such as 'General Analysis'. Each 2-5 word header must name a football characteristic or structural insight, not an event, score, minute, formation number, or chronological phase. Formations may appear factually only inside the text. Keep every text within 40-60 words, but make it analytically dense rather than superficial. Every bullet must follow this reasoning chain: connect at least two relevant pieces of evidence, identify the recurring team characteristic or mechanism they indicate, then explain its practical implication for control, progression, chance quality, defensive security, or adaptability. Prefer relationships such as volume versus efficiency, possession versus penetration, shot volume versus shot quality, pass security versus chance creation, duel output versus defensive exposure, and first-half versus second-half stability. Compare the team with its opponent where that sharpens the diagnosis. Across the five bullets, cover: structure and player relationships; ball progression and control; chance creation and finishing profile; defensive behaviour and discipline; adaptability or the structural effect of substitutions. Do not retell goals, cards, substitutions, or score changes as a story. Events, timing, score flow, and pressure may be used only as supporting evidence for a broader team characteristic, never as the subject of a bullet. Enforce exact numerical logic: an unchanged value is stable, never an increase or decrease; only call a value higher or lower after checking both numbers; identify clearly whether a comparison is between teams, halves, totals, or subsets; never compare unrelated metrics. Never write a self-correction, contradiction, fragment, or construction equivalent to 'three to two, not...' inside the final text. If evidence is ambiguous, omit that comparison instead of repairing it in prose. Do not claim that a late substitute merely added freshness, that a change had limited impact, or that a team reacted well solely because of its timing or the final score; demonstrate any substitution effect through supplied position, metric, event, or pressure evidence. For Turkish, use natural professional football terminology: say 'oyuncu değişiklikleri' or contextually 'kadro hamleleri', never 'personel değişimleri'; use 'ikinci yarı', 'şut denemesi', 'ceza sahası', 'topla ilerleme', 'üretkenlik', and 'bitiricilik' naturally, and avoid literal translations or corporate vocabulary. Formation fields establish only a player's line and slot, not a specific tactical role. Never infer unsupported roles, causation, attack direction, pressing scheme, build-up pattern, or tactical intent. Do not invent data. Do not use markdown, bullet characters, recommendations, or raw pressure numbers."
                + (" No Pressure Index evidence is available: never mention pressure, momentum, dominance derived from pressure, pressure changes, or a Pressure Index." if not pressure else " Translate supplied pressure values into relative qualitative language."),
            ),
            ("human", f"Language: {language}\nFull match evidence by team:\n{json.dumps(compact, ensure_ascii=False, default=str)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        result: dict[str, list[dict[str, str]]] = {}
        for team_key in compact["teams"]:
            rows = parsed.get(team_key) or []
            cleaned = [
                {
                    "header": str(row.get("header") or "").strip(),
                    "text": str(row.get("text") or "").strip(),
                    "tone": str(row.get("tone") or "neutral").strip().lower()
                    if str(row.get("tone") or "").strip().lower() in {"positive", "negative", "neutral"}
                    else "neutral",
                }
                for row in rows[:5]
                if str(row.get("header") or "").strip() and str(row.get("text") or "").strip()
            ]
            if len(cleaned) == 5:
                tone_order = {"positive": 0, "neutral": 1, "negative": 2}
                result[team_key] = sorted(
                    cleaned,
                    key=lambda item: tone_order.get(item.get("tone", "neutral"), 1),
                )
            else:
                result[team_key] = fallback[team_key]
        return result
    except Exception as exc:
        print(f"[enterprise_match_report] event=team_deep_analysis_fallback error={exc}")
        return fallback


def _build_report_overview_summary(report: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    teams = report.get("teams") or []
    has_pressure = bool(report.get("pressure"))
    has_territory = bool(report.get("ball_coordinates"))
    compact = {
        "fixture": report.get("fixture"),
        "league": report.get("league"),
        "venue": report.get("venue"),
        "state": report.get("state"),
        "teams": [
            {
                "id": team.get("id"),
                "name": team.get("name"),
                "location": team.get("location"),
                "formation": team.get("formation"),
            }
            for team in teams
        ],
        "score_flow": report.get("events"),
        **(
            {"momentum_interpretation": report.get("scoutwise_perspective_points")}
            if has_pressure
            else {}
        ),
        **(
            {"regional_play_interpretation": report.get("regional_play_perspective")}
            if has_territory
            else {}
        ),
        "team_comparison_interpretations": report.get("team_analysis_perspectives"),
        "team_deep_analyses": report.get("team_deep_analyses"),
        "player_analysis_interpretations": report.get("player_analysis_perspectives"),
        "lineups": [
            {
                "player_name": player.get("player_name"),
                "team_name": player.get("team_name"),
                "position": player.get("position_name"),
                "starter": player.get("starter"),
                "formation_field": player.get("formation_field"),
                "rating": next(
                    (
                        metric.get("value")
                        for metric in (player.get("categories") or {}).get("contribution_impact", [])
                        if metric.get("name") == "Rating"
                    ),
                    None,
                ),
                "minutes": next(
                    (
                        metric.get("value")
                        for metric in (player.get("categories") or {}).get("contribution_impact", [])
                        if metric.get("name") == "Minutes Played"
                    ),
                    None,
                ),
            }
            for player in report.get("lineups") or []
        ],
    }
    categories_tr = ["Maç Kartı", "Kadro ve Diziliş", "Maç Akışı", "Bölgesel Oyun Dağılımı", "Takım Karşılaştırması", "Takım Analizi", "Oyuncu Analizi"]
    categories_en = ["Match Card", "Lineup & Formation", "Timeline", "Regional Play Distribution", "Team Comparison", "Team Analysis", "Player Analysis"]
    if has_pressure:
        categories_tr.insert(3, "Momentum")
        categories_en.insert(3, "Momentum")
    if not has_territory:
        categories_tr.remove("Bölgesel Oyun Dağılımı")
        categories_en.remove("Regional Play Distribution")
    categories = categories_tr if lang == "tr" else categories_en
    fallback = [
        {"category": category, "summary": ("Bu bölümün rapor özeti hazırlanıyor." if lang == "tr" else "The report summary for this section is being prepared."), "sub_bullets": []}
        for category in categories
    ]
    if not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"))
        language = "Turkish" if lang == "tr" else "English"
        section_count = len(categories)
        section_order = ", ".join(categories_en)
        momentum_instruction = (
            "Lineup & Formation and Momentum have exactly two; "
            if has_pressure
            else "Lineup & Formation has exactly two; "
        )
        pressure_guard = (
            " Never mention pressure or momentum because no Pressure Index data is available."
            if not has_pressure
            else ""
        )
        territory_guard = (
            " Never mention regional play distribution, field zones, ball-location concentration, channels, or territory because no ball-coordinate evidence is available."
            if not has_territory
            else ""
        )
        response = ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"], temperature=0.2).invoke([
            (
                "system",
                f"You are ScoutWise Enterprise's lead football-report editor. Return only a valid JSON array of exactly {section_count} objects, in this exact report-section order: {section_order}. Translate category names naturally into the requested language. Every object must contain exactly category, summary, and sub_bullets. Keep exactly these sections and be highly concise. summary must be a cohesive, evidence-led synthesis of 20-30 words containing only the section's decisive interpretation. Every sub-bullet object must contain exactly label and text, with text limited to 10-18 words. Apply these counts strictly: Match Card, Timeline, and Regional Play Distribution have zero sub-bullets; {momentum_instruction}Team Comparison, Team Analysis, and Player Analysis have exactly two. For Team Comparison and Team Analysis, create one sub-bullet per team and use that team's exact name as the label. For Player Analysis, select exactly one interpreted player from each team and use the player's full name as the label; never select two players from the same team. Integrate and compress the supplied existing ScoutWise interpretations; do not contradict them or introduce a new tactical claim. State only supported interpretations and what the evidence positively indicates. Never add defensive capability caveats such as 'does not prove', 'cannot determine', 'cannot establish', 'alone is insufficient', 'the data does not show', or their equivalents. In Regional Play Distribution especially, describe the supported spatial pattern, channel use, central versus wide concentration, and proximity to dangerous end areas without explaining what cannot be inferred. Preserve distinctions between observation, correlation, and causation through careful affirmative wording, not disclaimer sentences. Never invent data, attack direction, roles, or events.{pressure_guard}{territory_guard} Avoid repeating the same fact across sections. Use polished, direct football-analysis language without markdown, bullet characters, recommendations, or generic filler.",
            ),
            ("human", f"Language: {language}\nReport evidence and existing interpretations:\n{json.dumps(compact, ensure_ascii=False, default=str)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or len(parsed) != section_count:
            return fallback
        cleaned = []
        for index, row in enumerate(parsed):
            summary = str(row.get("summary") or "").strip()
            sub_bullets = [
                {"label": str(item.get("label") or "").strip(), "text": str(item.get("text") or "").strip()}
                for item in (row.get("sub_bullets") or [])[:3]
                if str(item.get("label") or "").strip() and str(item.get("text") or "").strip()
            ]
            cleaned.append({
                "category": categories[index],
                "summary": summary or fallback[index]["summary"],
                "sub_bullets": sub_bullets,
            })
        return cleaned
    except Exception as exc:
        print(f"[enterprise_match_report] event=overview_summary_fallback error={exc}")
        return fallback


def generate_match_report(fixture_id: int, lang: str = "en", build_narratives: bool = True) -> dict[str, Any]:
    token = os.getenv("SPORTMONKS_API_KEY")
    if not token:
        raise MatchReportError("SPORTMONKS_API_KEY is not configured")
    response = requests.get(
        f"{SPORTMONKS_BASE_URL}/fixtures/{fixture_id}",
        params={"api_token": token, "include": MATCH_REPORT_INCLUDE},
        timeout=75,
    )
    if response.status_code != 200:
        try:
            message = response.json().get("message")
        except ValueError:
            message = None
        raise MatchReportError(f"{message or 'ScoutWise match data request failed'} (HTTP {response.status_code})")
    fixture = response.json().get("data") or {}
    if not fixture:
        raise MatchReportError("ScoutWise returned empty match data")

    participants = fixture.get("participants") or []
    team_by_id = {team.get("id"): team for team in participants}
    statistic_rows: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in fixture.get("statistics") or []:
        statistic_rows[row.get("participant_id")].append(row)
    expected_rows: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in fixture.get("xgfixture") or []:
        expected_rows[row.get("participant_id")].append(row)

    teams: list[dict[str, Any]] = []
    for team in participants:
        categories, extras = _categorize(statistic_rows.get(team.get("id"), []), TEAM_METRIC_CATEGORY)
        shooting_values = {
            str(metric.get("name")): metric.get("value")
            for metric in categories.get("shooting", [])
        }
        try:
            total_shots = float(shooting_values.get("Shots Total") or 0)
            shots_on_target = float(shooting_values.get("Shots On Target") or 0)
            if total_shots > 0:
                categories["shooting"].append(
                    _metric(
                        "Shots On Target (%)",
                        round(shots_on_target / total_shots * 100, 1),
                    )
                )
        except (TypeError, ValueError):
            pass
        passing_values = {
            str(metric.get("name")): metric.get("value")
            for metric in categories.get("passing", [])
        }
        try:
            total_crosses = float(passing_values.get("Total Crosses") or 0)
            accurate_crosses = float(passing_values.get("Accurate Crosses") or 0)
            if total_crosses > 0:
                categories["passing"].append(
                    _metric(
                        "Accurate Crosses (%)",
                        round(accurate_crosses / total_crosses * 100, 1),
                    )
                )
        except (TypeError, ValueError):
            pass
        expected = [
            _metric(str((row.get("type") or {}).get("name") or row.get("type_id")), _value(row), row.get("type_id"))
            for row in expected_rows.get(team.get("id"), [])
            if str((row.get("type") or {}).get("name") or "") != "Expected Goals Against (xGA)"
        ]
        teams.append(
            {
                "id": team.get("id"),
                "name": team.get("name"),
                "short_code": team.get("short_code"),
                "image_url": team.get("image_path"),
                "location": (team.get("meta") or {}).get("location"),
                "winner": (team.get("meta") or {}).get("winner"),
                "position": (team.get("meta") or {}).get("position"),
                "categories": categories,
                "extra_metrics": extras,
                "expected_metrics": expected,
            }
        )

    period_teams: dict[str, list[dict[str, Any]]] = {}
    for period in fixture.get("periods") or []:
        period_key = (
            "first_half"
            if period.get("type_id") == 1
            else "second_half"
            if period.get("type_id") == 2
            else None
        )
        if not period_key:
            continue
        rows_by_team: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for row in period.get("statistics") or []:
            rows_by_team[row.get("participant_id")].append(row)
        scoped_teams: list[dict[str, Any]] = []
        for full_team in teams:
            categories, extras = _categorize(
                rows_by_team.get(full_team.get("id"), []),
                TEAM_METRIC_CATEGORY,
            )
            _derive_team_percentages(categories)
            scoped_teams.append(
                {
                    **{key: full_team.get(key) for key in (
                        "id", "name", "short_code", "image_url", "location",
                        "winner", "position",
                    )},
                    "categories": categories,
                    "extra_metrics": extras,
                    "expected_metrics": [],
                }
            )
        period_teams[period_key] = scoped_teams

    formations = fixture.get("formations") or []
    formation_by_team = {row.get("participant_id"): row.get("formation") for row in formations}
    lineups: list[dict[str, Any]] = []
    player_expected_rows = 0
    for row in fixture.get("lineups") or []:
        categories, extras = _categorize(row.get("details") or [], PLAYER_METRIC_CATEGORY)
        expected = [
            _metric(str((item.get("type") or {}).get("name") or item.get("type_id")), _value(item), item.get("type_id"))
            for item in row.get("xglineup") or []
        ]
        _derive_player_metrics(categories, expected)
        if expected:
            player_expected_rows += 1
        lineups.append(
            {
                "id": row.get("id"),
                "player_id": row.get("player_id"),
                "player_name": row.get("player_name"),
                "player_image_url": (row.get("player") or {}).get("image_path"),
                "team_id": row.get("team_id"),
                "team_name": (team_by_id.get(row.get("team_id")) or {}).get("name"),
                "position_id": row.get("position_id"),
                "position_name": (row.get("position") or {}).get("name"),
                "type_id": row.get("type_id"),
                "starter": row.get("type_id") == 11,
                "jersey_number": row.get("jersey_number"),
                "formation_field": row.get("formation_field"),
                "formation_position": row.get("formation_position"),
                "categories": categories,
                "extra_metrics": extras,
                "expected_metrics": expected,
            }
        )

    events = []
    player_team = {row.get("player_id"): row.get("team_id") for row in fixture.get("lineups") or []}
    for row in fixture.get("events") or []:
        team_id = row.get("team_id") or player_team.get(row.get("player_id"))
        events.append(
            {
                "id": row.get("id"),
                "team_id": team_id,
                "team_name": (team_by_id.get(team_id) or {}).get("name"),
                "player_id": row.get("player_id"),
                "player_name": row.get("player_name"),
                "related_player_id": row.get("related_player_id"),
                "related_player_name": row.get("related_player_name"),
                "minute": row.get("minute"),
                "extra_minute": row.get("extra_minute"),
                "result": row.get("result"),
                "info": row.get("info"),
                "addition": row.get("addition"),
                "type_id": row.get("type_id"),
                "type": (row.get("type") or {}).get("name"),
            }
        )

    pressure = [
        {
            "team_id": row.get("participant_id"),
            "team_name": (team_by_id.get(row.get("participant_id")) or {}).get("name"),
            "minute": row.get("minute"),
            "value": row.get("pressure"),
        }
        for row in fixture.get("pressure") or []
    ]
    trends = [
        {
            "team_id": row.get("participant_id"),
            "team_name": (team_by_id.get(row.get("participant_id")) or {}).get("name"),
            "type_id": row.get("type_id"),
            "period_id": row.get("period_id"),
            "minute": row.get("minute"),
            "value": row.get("value"),
        }
        for row in fixture.get("trends") or []
    ]
    ball_coordinates = [
        {key: row.get(key) for key in ("period_id", "timer", "x", "y")}
        for row in fixture.get("ballcoordinates") or []
    ]

    report = {
        "language": lang,
        "version": MATCH_REPORT_VERSION,
        "fixture": {
            key: fixture.get(key)
            for key in (
                "id", "name", "starting_at", "starting_at_timestamp", "result_info",
                "length", "leg", "league_id", "season_id", "stage_id", "round_id",
                "venue_id", "state_id",
            )
        },
        "league": fixture.get("league"),
        "season": fixture.get("season"),
        "stage": fixture.get("stage"),
        "round": fixture.get("round"),
        "venue": fixture.get("venue"),
        "state": fixture.get("state"),
        "weather": fixture.get("weatherreport"),
        "coaches": fixture.get("coaches") or [],
        "referees": fixture.get("referees") or [],
        "scores": fixture.get("scores") or [],
        "periods": fixture.get("periods") or [],
        "formations": [
            {
                **row,
                "team_name": (team_by_id.get(row.get("participant_id")) or {}).get("name"),
            }
            for row in formations
        ],
        "teams": teams,
        "period_teams": period_teams,
        "lineups": lineups,
        "events": events,
        "pressure": pressure,
        "trends": trends,
        "ball_coordinates": ball_coordinates,
        "summary": _build_summary(teams, events, lang),
        "coverage": {
            "team_stat_rows": len(fixture.get("statistics") or []),
            "unique_team_metrics": len({
                metric.get("name")
                for team in teams
                for metric in (
                    [item for rows in team.get("categories", {}).values() for item in rows]
                    + team.get("expected_metrics", [])
                    + team.get("extra_metrics", [])
                )
                if metric.get("name")
            }),
            "player_stat_rows": sum(
                len(row.get("details") or []) + len(row.get("xglineup") or [])
                for row in fixture.get("lineups") or []
            ),
            "lineup_rows": len(lineups),
            "unique_player_metrics": len({
                metric.get("name")
                for player in lineups
                for metric in (
                    [item for rows in player.get("categories", {}).values() for item in rows]
                    + player.get("expected_metrics", [])
                    + player.get("extra_metrics", [])
                )
                if metric.get("name")
            }),
            "players_with_minutes": sum(
                1
                for player in lineups
                if any(
                    metric.get("name") == "Minutes Played"
                    and float(metric.get("value") or 0) > 0
                    for metric in player.get("categories", {}).get("contribution_impact", [])
                )
            ),
            "players_with_expected_metrics": player_expected_rows,
            "event_rows": len(events),
            "pressure_rows": len(pressure),
            "trend_rows": len(trends),
            "ball_coordinate_rows": len(ball_coordinates),
        },
        "metric_groups": list(METRIC_GROUPS),
        "temporary_extra_team_metrics": sorted(
            {item["name"] for team in teams for item in team.get("extra_metrics", [])}
        ),
    }
    for team in report["teams"]:
        team["formation"] = formation_by_team.get(team.get("id"))
    if build_narratives:
        report["scoutwise_perspective_points"] = _build_scoutwise_perspective(report, lang)
        report["scoutwise_perspective"] = " ".join(report["scoutwise_perspective_points"])
        report["regional_play_perspective"] = _build_regional_play_perspective(report, lang)
        report["team_analysis_perspectives"] = _build_team_analysis_perspectives(teams, period_teams, lang)
        report["player_analysis_perspectives"] = _build_player_analysis_perspectives(teams, lineups, events, lang)
        report["team_deep_analyses"] = _build_team_deep_analyses(report, lang)
        report["overview_summary"] = _build_report_overview_summary(report, lang)
    report["analysis_model"] = os.getenv(
        "OPENAI_MATCH_REPORT_MODEL",
        os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"),
    )
    return report


def build_team_report_metrics(
    reports: list[dict[str, Any]], team_id: int, lang: str = "en", build_narratives: bool = True
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    samples: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    match_rows: list[dict[str, Any]] = []
    for report in reports:
        team = next(
            (row for row in report.get("teams") or [] if int(row.get("id") or 0) == int(team_id)),
            None,
        )
        if not team:
            continue
        match_values: dict[str, dict[str, float]] = {}
        sources = dict(team.get("categories") or {})
        if team.get("expected_metrics"):
            sources["expected"] = team.get("expected_metrics") or []
        for group, metrics in sources.items():
            for metric in metrics or []:
                try:
                    value = float(metric.get("value"))
                except (TypeError, ValueError):
                    continue
                name = str(metric.get("name") or "").strip()
                if not name:
                    continue
                samples[group][name].append(value)
                match_values.setdefault(group, {})[name] = round(value, 3)
        match_rows.append({
            "fixture": (report.get("fixture") or {}).get("name") or (report.get("fixture") or {}).get("id"),
            "metrics": match_values,
        })
    aggregate: dict[str, list[dict[str, Any]]] = {}
    for group, metrics in samples.items():
        aggregate[group] = []
        for name, values in metrics.items():
            is_average = "%" in name or "percentage" in name.casefold() or "performance" in name.casefold()
            total = sum(values)
            numerator_denominator = DERIVED_PERCENTAGE_METRICS.get(name)
            numerator = sum(metrics.get(numerator_denominator[0], [])) if numerator_denominator else 0.0
            denominator = sum(metrics.get(numerator_denominator[1], [])) if numerator_denominator else 0.0
            derived_rate = numerator / denominator * 100 if numerator_denominator and denominator > 0 else None
            aggregate[group].append({
                "name": name,
                "value": round(derived_rate, 2) if derived_rate is not None else round(total / len(values), 2) if is_average else round(total, 2),
                "perMatch": round(derived_rate, 2) if derived_rate is not None else round(total / len(values), 2),
                "aggregation": "derived_rate" if derived_rate is not None else "average" if is_average else "total",
                "matchesCovered": len(values),
            })
        aggregate[group].sort(key=lambda row: row["name"])
    available = list(aggregate)
    if not build_narratives:
        return aggregate, {}
    fallback_text = (
        "Seçili maçların toplu değerleri, takımın bu kategorideki üretimini ve maç başına seviyesini birlikte gösteriyor."
        if lang == "tr" else
        "The selected-match totals and per-match levels show the team's output in this category."
    )
    fallback = {group: fallback_text for group in available}
    if not available or not os.getenv("OPENAI_API_KEY"):
        return aggregate, fallback
    try:
        from langchain_openai import ChatOpenAI
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior football team analyst. Return only one valid JSON object whose top-level keys MUST be copied character-for-character from REQUIRED_JSON_KEYS. Never translate, rename, title-case or omit those machine keys. Translate metric concepts only inside each string value. For every required key, write one evidence-led interpretation of 50-75 words in the requested language. Analyse only the named team across the selected matches; never compare it with opponents. The output must prioritise football conclusions, not recite the data: use at most two short numerical facts as evidence, then immediately explain what their relationship indicates about efficiency, consistency, risk, control, or recurring performance structure. Never state both the total and per-match version of the same metric; choose whichever is more informative. Do not enumerate metrics, repeat card values, report coverage counts, or walk through match-by-match ranges unless that variation directly supports the main conclusion. When the language is Turkish, every football term inside the values must be natural Turkish; the required JSON keys are the sole exception because they are machine identifiers and are not displayed. Use 2-3 cohesive sentences, with the evidence brief and the interpretation dominant. Never invent causation, tactical intent, external benchmarks, opponent comparisons, or unavailable facts. Do not use markdown or headings."),
            ("human", f"Language: {language}\nREQUIRED_JSON_KEYS: {json.dumps(available, ensure_ascii=False)}\nAggregated team categories: {json.dumps(aggregate, ensure_ascii=False)}\nPer-match team values: {json.dumps(match_rows, ensure_ascii=False)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("team perspective response root was not a JSON object")
        missing = [group for group in available if not str(parsed.get(group) or "").strip()]
        if missing:
            print(f"[enterprise_team_report] event=perspective_partial_fallback missing_keys={missing}")
        return aggregate, {group: str(parsed.get(group) or fallback[group]).strip() for group in available}
    except Exception as exc:
        print(f"[enterprise_team_report] event=perspective_fallback error={exc}")
        return aggregate, fallback


def build_team_report_player_perspectives(
    reports: list[dict[str, Any]], team_id: int, lang: str = "en", build_narratives: bool = True
) -> dict[str, dict[str, str]]:
    players: dict[str, dict[str, Any]] = {}
    for report in reports:
        for row in report.get("lineups") or []:
            if int(row.get("team_id") or 0) != int(team_id):
                continue
            contribution = (row.get("categories") or {}).get("contribution_impact") or []
            values = {str(item.get("name")): item.get("value") for item in contribution}
            try:
                minutes = float(values.get("Minutes Played") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0:
                continue
            key = str(row.get("player_id") or row.get("player_name"))
            player = players.setdefault(key, {
                "player_id": row.get("player_id"), "player_name": row.get("player_name"),
                "position": row.get("position_name"), "minutes": 0.0,
                "rating_weighted": 0.0, "rating_minutes": 0.0,
                "metrics": defaultdict(lambda: defaultdict(list)),
            })
            player["minutes"] += minutes
            try:
                rating = float(values.get("Rating"))
                player["rating_weighted"] += rating * minutes
                player["rating_minutes"] += minutes
            except (TypeError, ValueError):
                pass
            sources = dict(row.get("categories") or {})
            if row.get("expected_metrics"):
                sources["expected"] = row.get("expected_metrics") or []
            for group, metrics in sources.items():
                for metric in metrics or []:
                    try:
                        value = float(metric.get("value"))
                    except (TypeError, ValueError):
                        continue
                    player["metrics"][group][str(metric.get("name"))].append((value, minutes))
    eligible = []
    for player in players.values():
        if player["minutes"] < 90 or not player["rating_minutes"]:
            continue
        player["rating"] = player["rating_weighted"] / player["rating_minutes"]
        compact_metrics = {}
        for group, metrics in player["metrics"].items():
            compact_metrics[group] = {}
            for name, samples in metrics.items():
                average = "%" in name or any(word in name.casefold() for word in ("rating", "percentage", "performance", "captain"))
                available_minutes = sum(minutes for _, minutes in samples)
                compact_metrics[group][name] = round(
                    sum(value * minutes for value, minutes in samples) / available_minutes
                    if average and available_minutes else
                    sum(value for value, _ in samples) * 90 / available_minutes
                    if available_minutes else 0,
                    2,
                )
            # Keep the player-perspective evidence consistent with the cells:
            # derive each known percentage from its aggregated count pair.
            for rate_name, (numerator_name, denominator_name) in DERIVED_PERCENTAGE_METRICS.items():
                numerator = compact_metrics[group].get(numerator_name)
                denominator = compact_metrics[group].get(denominator_name)
                if numerator is not None and denominator is not None and denominator > 0:
                    compact_metrics[group][rate_name] = round(numerator / denominator * 100, 2)
        player["metrics"] = compact_metrics
        eligible.append(player)
    ordered = sorted(eligible, key=lambda player: (-player["rating"], -player["minutes"], str(player["player_name"])))
    featured = ordered[:3]
    featured_ids = {str(player["player_id"] or player["player_name"]) for player in featured}
    development = [player for player in reversed(ordered) if str(player["player_id"] or player["player_name"]) not in featured_ids][:3]
    chosen = [(player, "featured") for player in featured] + [(player, "development") for player in development]
    if not build_narratives:
        return {str(p["player_id"] or p["player_name"]): {"selectionType": selection, "rating": p["rating"]} for p, selection in chosen}
    result = {
        str(player["player_id"] or player["player_name"]): {
            "selectionType": selection,
            "rating": player["rating"],
            "text": (
                f"{player['player_name']}, seçili maçlarda {player['rating']:.2f} ortalama puanla öne çıkan performanslardan birini verdi. Mevki rolündeki üretimi bu seçimi destekledi."
                if selection == "featured" and lang == "tr" else
                f"{player['player_name']}, seçili maçlardaki {player['rating']:.2f} ortalama puanıyla gelişime açık performanslar arasında kaldı. Mevki sorumluluklarındaki düşük kalan göstergeler temel gelişim alanını oluşturdu."
                if lang == "tr" else
                f"{player['player_name']} produced one of the standout selected-match performances with a {player['rating']:.2f} average score. The player's positional output supports the selection."
                if selection == "featured" else
                f"{player['player_name']} remained among the development-area performers with a {player['rating']:.2f} selected-match average. Lower position-relevant output defines the main development area."
            ),
        }
        for player, selection in chosen
    }
    if not chosen or not os.getenv("OPENAI_API_KEY"):
        return result
    compact = [{
        "player_id": player["player_id"], "player_name": player["player_name"],
        "selection_type": selection, "position": player["position"],
        "minutes": round(player["minutes"]), "average_rating": round(player["rating"], 2),
        "aggregated_selected_match_metrics": player["metrics"],
    } for player, selection in chosen]
    try:
        from langchain_openai import ChatOpenAI
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior player-performance analyst. Return only a valid JSON object keyed by player_id. Each value must be a 20-30 word interpretation in the requested language. Use the same methodology throughout. For featured players, write an exclusively positive, evidence-led assessment emphasizing the strongest position-relevant aggregated metrics and their football meaning; never mention a weakness. For development players, write an exclusively weakness-focused diagnosis using low efficiency, errors, discipline, lost actions, missed chances, or low position-relevant output; never praise or soften. Use only supplied evidence, lead with the conclusion, keep numerical facts brief, and prioritise interpretation over listing. Translate all metric concepts naturally; in Turkish never use English metric names and write percentages as %42. Never invent tactics, causation, benchmarks, or recommendations. No markdown or headings."),
            ("human", f"Language: {language}\nSelected players and aggregated selected-match data:\n{json.dumps(compact, ensure_ascii=False, default=str)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        for player, selection in chosen:
            key = str(player["player_id"] or player["player_name"])
            value = parsed.get(key) or parsed.get(str(player["player_id"]))
            if isinstance(value, dict):
                value = value.get("text")
            if str(value or "").strip():
                result[key] = {"selectionType": selection, "rating": player["rating"], "text": str(value).strip()}
    except Exception as exc:
        print(f"[enterprise_team_report] event=player_perspective_fallback error={exc}")
    return result


def build_team_report_momentum_perspectives(
    reports: list[dict[str, Any]], team_id: int, lang: str = "en"
) -> dict[str, str]:
    halves: dict[str, list[dict[str, Any]]] = {"firstHalf": [], "secondHalf": []}
    for report in reports:
        pressure = report.get("pressure") or []
        team_rows = {int(row.get("minute") or 0): float(row.get("value") or 0) for row in pressure if int(row.get("team_id") or 0) == int(team_id)}
        opponent_rows: dict[int, float] = defaultdict(float)
        for row in pressure:
            if int(row.get("team_id") or 0) != int(team_id):
                opponent_rows[int(row.get("minute") or 0)] += float(row.get("value") or 0)
        minutes = sorted(set(team_rows) | set(opponent_rows))
        events = report.get("events") or []
        for key, start, end in (("firstHalf", 0, 45), ("secondHalf", 46, 120)):
            net = [(minute, team_rows.get(minute, 0) - opponent_rows.get(minute, 0)) for minute in minutes if start <= minute <= end]
            if not net:
                continue
            event_rows = []
            for event in events:
                minute = int(event.get("minute") or 0)
                event_type = str(event.get("type") or "").casefold()
                if start <= minute <= end and ("goal" in event_type or "substitution" in event_type):
                    event_rows.append({"minute": minute, "type": event.get("type"), "forTeam": int(event.get("team_id") or 0) == int(team_id)})
            halves[key].append({
                "fixture": (report.get("fixture") or {}).get("name") or (report.get("fixture") or {}).get("id"),
                "averageNetPressure": round(sum(value for _, value in net) / len(net), 2),
                "dominantMinuteShare": round(sum(1 for _, value in net if value > 0) / len(net) * 100, 1),
                "strongestMinutes": sorted(net, key=lambda row: row[1], reverse=True)[:5],
                "weakestMinutes": sorted(net, key=lambda row: row[1])[:5],
                "events": event_rows,
            })
    fallback = {
        "firstHalf": "İlk yarı baskı akışı seçili maçlar boyunca takımın üstünlük kurduğu ve kontrolü rakibe bıraktığı dakika aralıklarını birlikte gösteriyor." if lang == "tr" else "The first-half pressure flow shows the intervals in which the team established and surrendered control across the selected matches.",
        "secondHalf": "İkinci yarı baskı akışı, devre sonrası baskı sürekliliğini ve gol ya da oyuncu değişiklikleri çevresindeki tekrar eden yön değişimlerini öne çıkarıyor." if lang == "tr" else "The second-half pressure flow highlights post-break continuity and recurring changes around goals or substitutions.",
    }
    if not any(halves.values()) or not os.getenv("OPENAI_API_KEY"):
        return fallback


    try:
        from langchain_openai import ChatOpenAI
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
        ).invoke([
            ("system", "You are ScoutWise Enterprise's senior football momentum analyst. Return only valid JSON with exactly firstHalf and secondHalf. Write 45-65 words for each in the requested language. Analyse the named team's recurring pressure pattern across all selected matches, not a chronological retelling of every match. Identify the clearest repeated dominant and lost-control minute bands, changes after goals or substitutions when supported, and whether pressure was sustained or fragmented. Treat relationships as coincidence, never proven causation. Do not mention raw pressure-index values, red cards, unavailable tactics, recommendations, or opponent names. Keep facts brief and make the football inference dominant. In Turkish use natural terminology and no English metric names. No markdown or headings."),
            ("human", f"Language: {language}\nAggregated first/second-half momentum evidence:\n{json.dumps(halves, ensure_ascii=False, default=str)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        return {key: str(parsed.get(key) or fallback[key]).strip() for key in fallback}
    except Exception as exc:
        print(f"[enterprise_team_report] event=momentum_perspective_fallback error={exc}")
        return fallback


def build_team_report_regional_perspective(
    reports: list[dict[str, Any]], team_id: int, lang: str = "en"
) -> str:
    del team_id
    aggregated = {"ball_coordinates": []}
    for report in reports:
        rows = report.get("ball_coordinates") or []
        if rows:
            aggregated["ball_coordinates"].extend(rows)
    fallback = (
        "Birleşik konum dağılımı, takımın yer aldığı oyunlarda topun tekrar eden biçimde yoğunlaştığı bölgeleri ve yerleşik alan kullanım karakterini gösteriyor."
        if lang == "tr"
        else "The combined location distribution reveals the recurring areas of concentration and established spatial character in matches involving the team."
    )
    if not aggregated["ball_coordinates"]:
        return fallback
    perspective = _build_regional_play_perspective(aggregated, lang)
    generic = (
        "Kaydedilen top konumları, sahanın belirli bölgelerinde daha yoğun bir dağılım gösterdi."
        if lang == "tr"
        else "Recorded ball locations showed a greater concentration in specific pitch zones."
    )
    if not perspective or perspective == generic:
        return fallback
    prefix = (
        "Takımın seçili maçlar boyunca tekrar eden alan kullanım karakterinde "
        if lang == "tr"
        else "Within the team's recurring spatial character across the selected matches, "
    )
    return prefix + perspective[:1].lower() + perspective[1:]


def _mobile_profile_analysis(value):
    """Accept a paragraph or bullet array without iterating a string as characters."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        if not isinstance(item, str):
            continue
        item = re.sub(r"^\s*(?:[-•–—]+|\d+[.)])\s*", "", item).strip()
        if len(item.split()) < 3:
            continue
        if len(item.split()) > 35:
            sentences = re.split(r"(?<=[.!?])\s+", item)
            item = sentences[0]
        cleaned.append(item)
    return cleaned[:1]


def _profile_prompt(prompt: str, build_narratives: bool) -> str:
    """Keep AI selection for every plan; only the explanation fields are paid."""
    if build_narratives:
        return prompt
    prompt = prompt.replace('title, metrics and analysis', 'title and metrics')
    prompt = prompt.replace('playerId, metrics and analysis', 'playerId and metrics')
    prompt = prompt.replace('title, exactly FOUR single-metric cells, and an analysis array of exactly ONE professional bullet of 20-30 words',
                            'title and exactly FOUR single-metric cells')
    # Remove prose-output requirements, preserving shared selection/evidence rules.
    sentences = re.split(r'(?<=[.!?])\s+', prompt)
    prompt = ' '.join(sentence for sentence in sentences
                      if not re.search(r'\banalysis\b|\bbullet(?:s)?\b', sentence, re.I))
    return prompt + (' Return the same evidence-led selections, dynamic titles, and diagnostic metrics '
                     'you would choose for a full report. Do not generate explanations or commentary. '
                     'All supplied volume metrics are already normalized per 90; show numeric values only, never /90. '
                     'Rate metrics retain percentages. Consider all supplied relevant evidence, including metrics not displayed. '
                     'Omit every analysis field. Theme objects contain exactly title and metrics; '
                     'player objects, when requested, contain exactly playerId and metrics.')


def _profile_output(value: dict[str, Any], build_narratives: bool) -> dict[str, Any]:
    if build_narratives:
        return value
    from api_module.report_access import without_analysis
    return without_analysis(value)


def build_team_report_attack_profile(
    reports: list[dict[str, Any]],
    team_id: int,
    team_metrics: dict[str, list[dict[str, Any]]],
    lang: str = "en",
    build_narratives: bool = True,
) -> dict[str, Any]:
    attacking_groups = ("contribution_impact", "shooting", "passing", "expected")
    team_name = next((str(team.get("name") or "") for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == int(team_id)), "Takım" if lang == "tr" else "The team")
    team_evidence = {
        group: [{
            "name": row.get("name"),
            "valuePer90": row.get("perMatch"),
            "isRate": "%" in str(row.get("name") or "") or "percentage" in str(row.get("name") or "").casefold(),
        } for row in rows]
        for group, rows in team_metrics.items()
        if group in attacking_groups and rows
    }
    players: dict[str, dict[str, Any]] = {}
    for report in reports:
        for row in report.get("lineups") or []:
            if int(row.get("team_id") or 0) != int(team_id):
                continue
            impact = {
                str(item.get("name")): item.get("value")
                for item in (row.get("categories") or {}).get("contribution_impact", []) or []
            }
            try:
                minutes = float(impact.get("Minutes Played") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0:
                continue
            key = str(row.get("player_id") or row.get("player_name"))
            player = players.setdefault(key, {
                "playerId": row.get("player_id"), "name": row.get("player_name"),
                "imageUrl": row.get("player_image_url"), "position": row.get("position_name"),
                "minutes": 0.0, "ratingWeighted": 0.0, "ratingMinutes": 0.0,
                "metrics": defaultdict(lambda: defaultdict(list)),
            })
            player["minutes"] += minutes
            if not player.get("imageUrl") and row.get("player_image_url"):
                player["imageUrl"] = row.get("player_image_url")
            try:
                rating = float(impact.get("Rating"))
                player["ratingWeighted"] += rating * minutes
                player["ratingMinutes"] += minutes
            except (TypeError, ValueError):
                pass
            sources = dict(row.get("categories") or {})
            sources["expected"] = row.get("expected_metrics") or []
            for group in attacking_groups:
                for metric in sources.get(group) or []:
                    try:
                        value = float(metric.get("value"))
                    except (TypeError, ValueError):
                        continue
                    player["metrics"][group][str(metric.get("name") or "")].append((value, minutes))
    candidates: list[dict[str, Any]] = []
    for player in players.values():
        if player["minutes"] < 90:
            continue
        position_name = str(player.get("position") or "").casefold()
        if "goalkeeper" in position_name or "kaleci" in position_name:
            continue
        compact: dict[str, dict[str, float]] = {}
        attack_score = 0.0
        for group, metrics in player["metrics"].items():
            compact[group] = {}
            for name, samples in metrics.items():
                normalized = name.casefold()
                average = "%" in name or any(word in normalized for word in ("rating", "percentage", "performance"))
                raw_value = (
                    sum(value * minutes for value, minutes in samples) / sum(minutes for _, minutes in samples)
                    if average else sum(value for value, _ in samples)
                )
                value = raw_value if average else raw_value / player["minutes"] * 90
                compact[group][name] = {"value": round(value, 2), "basis": "rate" if average else "per90"}
                if any(word in normalized for word in ("goal", "assist", "shot", "key pass", "chance", "expected goal", "expected assist", "xg", "xa")):
                    weight = 5 if "goal" in normalized and "expected" not in normalized else 3 if "assist" in normalized else 1
                    attack_score += max(0, raw_value) * weight
        rating = player["ratingWeighted"] / player["ratingMinutes"] if player["ratingMinutes"] else 0
        role_bonus = 2.0 if any(role in position_name for role in ("attacker", "forward", "winger", "hücum")) else 1.0 if any(role in position_name for role in ("midfielder", "orta saha")) else 0.0
        player.update({"minutes": round(player["minutes"]), "averageRating": round(rating, 2), "metrics": compact, "attackScore": round(attack_score + rating + role_bonus, 2)})
        candidates.append(player)
    candidates.sort(key=lambda row: (-row["attackScore"], -row["averageRating"], -row["minutes"]))
    shortlist = candidates[:8]
    selected = shortlist[:2]
    def metric_cell(name: str, value: Any, basis: str = "per90") -> dict[str, str]:
        try:
            numeric = float(value)
            shown = str(int(numeric)) if numeric.is_integer() else f"{numeric:.2f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            shown = str(value or "—")
        return {"label": name, "value": f"%{shown}" if basis == "rate" else shown}

    flat_team = [
        (group, str(row.get("name") or ""), row.get("valuePer90"), "rate" if row.get("isRate") else "per90")
        for group, rows in team_evidence.items() for row in rows
        if row.get("name") and row.get("valuePer90") is not None
    ]
    theme_rules = [
        ("Üretim ve Bitiricilik" if lang == "tr" else "Creation & Finishing", ("goal", "shot", "xg", "chance", "conversion")),
        ("Fırsat Oluşturma ve Pas Bağlantısı" if lang == "tr" else "Chance Creation & Passing Links", ("assist", "key pass", "xa", "pass", "cross", "through")),
        ("Hücum Sürekliliği ve Tehdit Çeşitliliği" if lang == "tr" else "Attacking Continuity & Threat Variety", ("possession", "touch", "attack", "dribble", "cross", "danger")),
    ]
    used_team_metrics: set[tuple[str, str]] = set()
    fallback_theme_metrics: list[list[dict[str, str]]] = []
    for _, keywords in theme_rules:
        ranked = sorted(flat_team, key=lambda item: (not any(word in item[1].casefold() for word in keywords), item[0], item[1]))
        chosen_rows = []
        for group, name, value, basis in ranked:
            key = (group, name)
            if key in used_team_metrics:
                continue
            chosen_rows.append(metric_cell(name, value, basis))
            used_team_metrics.add(key)
            if len(chosen_rows) == 4:
                break
        if len(chosen_rows) < 4:
            existing_labels = {row["label"] for row in chosen_rows}
            for _, name, value, basis in ranked:
                if name in existing_labels:
                    continue
                chosen_rows.append(metric_cell(name, value, basis))
                existing_labels.add(name)
                if len(chosen_rows) == 4:
                    break
        fallback_theme_metrics.append(chosen_rows)
    fallback_themes = [
        {"title": theme_rules[0][0], "metrics": fallback_theme_metrics[0], "analysis": ([f"{team_name}, şut hacmi ile isabet ve xG üretimini aynı anda yukarı taşıyabildiği bölümlerde hücumlarını rastlantısal denemelerden çıkarıp sürekli ceza sahası tehdidine dönüştürüyor. Gol çıktısının bu kaliteyle aynı yönde hareket etmesi bitiriciliğin üretim yapısını desteklediğini; ayrışması ise son aksiyonda kayıp yaşandığını gösteriyor.", f"{team_name} için temel hücum kimliği yalnızca sık şut çekmek değil, şut öncesindeki aksiyonu savunmayı dengesiz yakalayacak kaliteye taşımaktır. İsabetli şut payı ile fırsat kalitesi birlikte güçlendiğinde takım oyunu rakip kaleye yerleştiriyor; hacim tek başına yükseldiğinde ise üretim daha kolay savunulabilir hale geliyor."] if lang == "tr" else [f"{team_name} turns attacking volume into sustained penalty-area threat when shot frequency, on-target output and xG rise together. Goal output moving with that quality supports a repeatable finishing structure; separation points to loss at the final action rather than an inability to reach scoring positions.", f"For {team_name}, the attacking identity is not simply frequent shooting but improving the action before the shot until the defence is unbalanced. When on-target share and chance quality strengthen together, the team pins play near goal; volume without that support becomes easier to defend."])},
        {"title": theme_rules[1][0], "metrics": fallback_theme_metrics[1], "analysis": ([f"{team_name}, pas başarısını kilit pas ve xA üretimine bağlayabildiğinde top dolaşımı yalnızca güvenli sahiplik olarak kalmıyor; savunma hatları arasında doğrudan fırsat hazırlayan bir mekanizmaya dönüşüyor. Asist çıktısı yaratılan kaliteyi takip ediyorsa son pas zinciri işliyor, geride kalıyorsa hazırlanan avantaj tamamlanamıyor.", f"{team_name} hücumunun dayanıklılığı yaratıcılığın tek bir oyuncu veya pas koridorunda toplanmamasına bağlıdır. Kilit pas, ara pas ve ceza sahası bağlantıları farklı rollere yayıldığında takım kapalı savunmaya karşı yön değiştirebilir; üretimin dar bir bağlantıda yoğunlaşması ise o bağlantı kapandığında akışı kırılganlaştırır."] if lang == "tr" else [f"When {team_name} connects pass success to key-pass and xA production, circulation stops being safe possession and becomes a mechanism for creating chances between defensive lines. Assist output following that quality indicates a functioning final-pass chain; falling behind it points to lost advantage at completion.", f"The resilience of {team_name}'s attack depends on creativity not being isolated in one player or passing corridor. Distribution of key passes, through balls and penalty-area links allows direction changes against compact blocks; concentration in one connection makes the flow fragile when that route closes."])},
        {"title": theme_rules[2][0], "metrics": fallback_theme_metrics[2], "analysis": ([f"{team_name}, şut, dripling, orta ve ceza sahası bağlantılarını aynı üretim zincirinde kullanabildiğinde tek bir sonuçlandırma yoluna sıkışmıyor. Bu çeşitlilik savunmanın yalnızca merkez veya kanat koridorunu kapatarak hücumu durdurmasını zorlaştırıyor ve takımın ilk tercih kapandığında tehdidi farklı bir bölgeden yeniden kurmasını sağlıyor.", f"{team_name} adına üretimin birkaç oyuncu ya da aksiyon tipinde yoğunlaşması hücum akışını kırılganlaştırırken, katkının farklı rollere yayılması topun son bölgeye taşınmasından bitiriciliğe kadar daha devamlı bir yapı oluşturuyor. Bu dağılım baskı altında bile hücum yönünü değiştirebilme ve ikinci çözümü devreye sokabilme kapasitesini belirliyor."] if lang == "tr" else [f"When {team_name} connects shooting, dribbling, crossing and penalty-area links within the same production chain, the attack is not confined to one finishing route. This variety makes it harder to stop the team by protecting only central or wide corridors and enables threat to be rebuilt elsewhere.", f"For {team_name}, concentration in a few players or action types makes attacking flow fragile, while distribution across roles creates continuity from progression to finishing. That spread defines the capacity to redirect attacks under pressure and activate a secondary solution when the primary route closes."])},
    ]
    fallback_players = []
    for player in selected:
        flat_player = [(name, details.get("value"), details.get("basis") or "per90") for metrics in player["metrics"].values() for name, details in metrics.items()]
        position = str(player.get("position") or "").casefold()
        role_keywords = (
            ("goal", "xg", "shot", "chance", "touch", "dribbl", "assist", "key pass")
            if any(role in position for role in ("attacker", "forward", "hücum")) else
            ("assist", "key pass", "xa", "pass", "dribbl", "cross", "touch", "shot", "xg")
            if any(role in position for role in ("midfielder", "winger", "orta saha")) else
            ("pass", "touch", "cross", "dribbl", "assist", "key pass", "shot", "xg")
        )
        def role_priority(row: tuple[str, Any, str]) -> tuple[int, str]:
            lower = row[0].casefold()
            return (next((index for index, word in enumerate(role_keywords) if word in lower), len(role_keywords)), row[0])
        ranked_player = sorted(flat_player, key=role_priority)
        player_cells = []
        used_names: set[str] = set()
        for name, value, basis in ranked_player:
            if name in used_names or not name:
                continue
            player_cells.append(metric_cell(name, value, basis))
            used_names.add(name)
            if len(player_cells) == 4:
                break
        fallback_players.append({
            "playerId": player["playerId"], "name": player["name"], "imageUrl": player.get("imageUrl"),
            "position": player.get("position"), "minutes": player["minutes"], "averageRating": player["averageRating"],
            "metrics": player_cells[:4],
            "analysis": ([f"{player['name']}, bitiricilik ile fırsat hazırlama arasındaki bağlantısı sayesinde yalnızca son aksiyonun değil, hücumun gelişim sürecinin de parçası oluyor. Dört temel göstergenin birlikte ürettiği profil, oyuncunun tehdit seviyesini tek bir gole veya şuta bağlı kalmadan sürdürebildiğini gösteriyor.", "Oyuncunun pas ve şut katkısı aynı hücum dizilerinde değer ürettiğinde savunma hem hazırlayıcı hem tamamlayıcı tehdidi takip etmek zorunda kalır. Bu çift yönlü rol, takımın son bölgedeki karar seçeneklerini genişletirken hücum akışının tek bir bağlantıya bağımlı kalmasını azaltır."] if lang == "tr" else [f"{player['name']} contributes to both the development and completion of attacks through the link between finishing and chance creation. The combined profile across four core indicators shows threat that is not dependent on a single goal or shot event.", "When passing and shooting contributions create value within the same attacking sequences, defenders must track both a creator and a finisher. This dual role expands final-third choices and reduces the attack's dependence on one connection."]),
        })
    fallback = {"themes": fallback_themes[:2], "players": fallback_players}
    if not team_evidence or not selected or not os.getenv("OPENAI_API_KEY"):
        return _profile_output(fallback, build_narratives)
    try:
        from langchain_openai import ChatOpenAI
        language = "Turkish" if lang == "tr" else "English"
        compact_players = [{key: value for key, value in player.items() if key not in ("ratingWeighted", "ratingMinutes")} for player in shortlist]
        response = ChatOpenAI(
            model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
            api_key=os.environ["OPENAI_API_KEY"], temperature=0.25, timeout=120, max_retries=1,
        ).invoke([
            ("system", _profile_prompt("You are ScoutWise Enterprise's senior professional attacking analyst. Return only valid JSON with exactly themes and players. All supplied volume metrics are already normalized per 90 minutes, but NEVER write '/90' or repeatedly mention normalization in any metric cell or analysis; show only the numeric value. Rate metrics retain percentages. Ignore set-piece data entirely. themes must contain exactly two dynamically named, non-overlapping attacking identities inferred from the complete team dataset. Across them synthesize every available relevant metric from shooting, passing, advanced metrics and contribution-impact. Each theme must have title, metrics and analysis. Theme metrics must contain exactly four objects with label and value, and EVERY theme metric cell must contain exactly one metric label and one numeric value—never combine multiple metrics in a cell. Theme analysis must contain exactly ONE professional bullet string of 20-30 words. Start with a direct team-specific conclusion about how the team creates, progresses or finishes; never define a metric and never write generic statements. Use the entire supplied dataset internally, but do not recite it: each analysis bullet may contain at most ONE short numerical fact and at least 80% of its wording must be football inference. Never write sequences such as '37 passes, 30 accurate passes, 81%, 2 final-third passes'. Explain what the relationships reveal about recurring attacking routes, dependencies, efficiency, risk, chance quality, conversion and threat variety. players must contain exactly two players selected strictly through attacking contribution. For every player copy playerId exactly from the supplied shortlist and return playerId, metrics and analysis. Each player must have exactly four role-specific metric cells, each containing exactly one metric label and one numeric value. Choose different cells when player roles differ. Player analysis must consider all supplied relevant metrics—including those not displayed—but contain exactly ONE bullet of 20-30 words, with at most ONE short numerical fact per bullet. Lead with what the player does in this team's attack, the decisions or spaces the role affects, and the dependency or advantage created; do not narrate a statistical line. Do not mention match count, opponents, unavailable tactics, external benchmarks, coverage counts, raw category names or recommendations. In Turkish translate Touches exactly as 'Topla Buluşma' and use 'başarı oranı', never 'doğruluk'; write advanced metrics with their full Turkish name followed by the correct abbreviation in parentheses, for example 'Akan Oyun Gol Beklentisi (xGOP)'. Preserve distinct xG, xA, xGoT, npxG, xGOP and other supplied advanced metrics. Never output markdown.", build_narratives)),
            ("human", f"Language: {language}\nTeam: {team_name}\nAggregated team attacking metrics: {json.dumps(team_evidence, ensure_ascii=False)}\nEligible attacking-player shortlist: {json.dumps(compact_players, ensure_ascii=False, default=str)}"),
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
        parsed = json.loads(raw)
        for entry in [*(parsed.get("themes") or []), *(parsed.get("players") or [])]:
            if isinstance(entry, dict):
                entry["analysis"] = _mobile_profile_analysis(entry.get("analysis"))
        themes = parsed.get("themes") if isinstance(parsed.get("themes"), list) else []
        generated_players = parsed.get("players") if isinstance(parsed.get("players"), list) else []
        by_id = {str(player["playerId"]): player for player in shortlist}
        by_name = {str(player.get("name") or "").strip().casefold(): player for player in shortlist}
        output_players = []
        for generated in generated_players[:2]:
            source = by_id.get(str(generated.get("playerId")))
            if not source:
                source = by_name.get(str(generated.get("name") or "").strip().casefold())
            if not source:
                continue
            output_players.append({
                "playerId": source["playerId"], "name": source["name"], "imageUrl": source.get("imageUrl"),
                "position": source.get("position"), "minutes": source["minutes"], "averageRating": source["averageRating"],
                "metrics": [{"label": str(value.get("label") or ""), "value": str(value.get("value") or "")} for value in (generated.get("metrics") or [])[:4] if isinstance(value, dict)],
                "analysis": [str(value) for value in (generated.get("analysis") or [])[:3]],
            })
        def valid_single_metric_cells(items: list[dict[str, Any]]) -> bool:
            for item in items:
                cells = item.get("metrics") or []
                if len(cells) < 4:
                    return False
                for cell in cells[:4]:
                    value = str(cell.get("value") or "")
                    if "·" in value or len(re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)?", value)) > 1:
                        return False
            return True

        def inference_led(items: list[dict[str, Any]]) -> bool:
            return all(
                all(len(re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)?", str(bullet))) <= 1 for bullet in (item.get("analysis") or []))
                for item in items
            )

        valid_themes = len(themes) == 2 and valid_single_metric_cells(themes) and inference_led(themes) and (not build_narratives or all(len(item.get("analysis") or []) >= 1 for item in themes))
        valid_players = len(output_players) == 2 and valid_single_metric_cells(output_players) and inference_led(output_players) and (not build_narratives or all(len(item.get("analysis") or []) >= 1 for item in output_players))
        if valid_themes and valid_players:
            return _profile_output({"themes": [{"title": str(item.get("title") or "").strip(), "metrics": [{"label": str(value.get("label") or ""), "value": str(value.get("value") or "")} for value in (item.get("metrics") or [])[:4] if isinstance(value, dict)], "analysis": [str(value) for value in (item.get("analysis") or [])[:3]]} for item in themes], "players": output_players}, build_narratives)

        # Preserve usable model-written analysis instead of discarding the whole
        # profile when one metric cell or one player identifier is malformed.
        repaired_themes = []
        for index, fallback_theme in enumerate(fallback_themes[:2]):
            generated = themes[index] if index < len(themes) and isinstance(themes[index], dict) else {}
            generated_analysis = [str(value).strip() for value in (generated.get("analysis") or [])[:3] if str(value).strip()]
            generated_metrics = [
                {"label": str(value.get("label") or ""), "value": str(value.get("value") or "")}
                for value in (generated.get("metrics") or [])[:4] if isinstance(value, dict)
            ]
            repaired_themes.append({
                "title": str(generated.get("title") or fallback_theme["title"]).strip(),
                "metrics": generated_metrics if valid_single_metric_cells([{"metrics": generated_metrics}]) else fallback_theme["metrics"],
                "analysis": generated_analysis if len(generated_analysis) >= 1 and inference_led([{"analysis": generated_analysis}]) else fallback_theme["analysis"],
            })

        fallback_player_by_id = {str(player["playerId"]): player for player in fallback_players}
        repaired_players = []
        for generated in output_players:
            fallback_player = fallback_player_by_id.get(str(generated["playerId"]))
            if not fallback_player:
                continue
            generated_analysis = [str(value).strip() for value in (generated.get("analysis") or [])[:3] if str(value).strip()]
            generated_metrics = generated.get("metrics") or []
            repaired_players.append({
                **fallback_player,
                "metrics": generated_metrics if valid_single_metric_cells([{"metrics": generated_metrics}]) else fallback_player["metrics"],
                "analysis": generated_analysis if len(generated_analysis) >= 1 and inference_led([{"analysis": generated_analysis}]) else fallback_player["analysis"],
            })
        repaired_ids = {str(player["playerId"]) for player in repaired_players}
        repaired_players.extend(player for player in fallback_players if str(player["playerId"]) not in repaired_ids)
        print(
            "[enterprise_team_report] event=attack_profile_validation_fallback "
            f"themes={len(themes)} output_players={len(output_players)} "
            f"valid_themes={valid_themes} valid_players={valid_players}"
        )
        return _profile_output({"themes": repaired_themes, "players": repaired_players[:2]}, build_narratives)
    except Exception as exc:
        print(f"[enterprise_team_report] event=attack_profile_fallback error={exc}")
    return _profile_output(fallback, build_narratives)


def build_team_report_defense_profile(
    reports: list[dict[str, Any]],
    team_id: int,
    team_metrics: dict[str, list[dict[str, Any]]],
    lang: str = "en",
    build_narratives: bool = True,
) -> dict[str, Any]:
    """Build a selected-match defensive identity without goalkeeper-save evidence."""
    groups = ("defending", "errors_discipline")
    excluded = (
        "save", "kurtar", "offside", "ofsayt", "shot off target",
        "shots off target", "big chances missed", "missed big chances",
        "inaccurate shot", "goal conversion", "chance created",
    )
    team_name = next((str(team.get("name") or "") for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == int(team_id)), "Takım" if lang == "tr" else "The team")

    def allowed(name: Any) -> bool:
        return bool(name) and not any(word in str(name).casefold() for word in excluded)

    team_evidence = {
        group: [{"name": row.get("name"), "valuePer90": row.get("perMatch"), "isRate": "%" in str(row.get("name") or "") or "percentage" in str(row.get("name") or "").casefold()} for row in rows if allowed(row.get("name"))]
        for group, rows in team_metrics.items() if group in groups
    }
    team_evidence = {group: rows for group, rows in team_evidence.items() if rows}
    players: dict[str, dict[str, Any]] = {}
    for report in reports:
        for row in report.get("lineups") or []:
            if int(row.get("team_id") or 0) != int(team_id):
                continue
            position_name = str(row.get("position_name") or "").casefold()
            if "goalkeeper" in position_name or "kaleci" in position_name:
                continue
            impact = {str(item.get("name")): item.get("value") for item in (row.get("categories") or {}).get("contribution_impact", []) or []}
            try:
                minutes = float(impact.get("Minutes Played") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0:
                continue
            key = str(row.get("player_id") or row.get("player_name"))
            player = players.setdefault(key, {"playerId": row.get("player_id"), "name": row.get("player_name"), "imageUrl": row.get("player_image_url"), "position": row.get("position_name"), "minutes": 0.0, "metrics": defaultdict(lambda: defaultdict(list))})
            player["minutes"] += minutes
            for group in groups:
                for metric in (row.get("categories") or {}).get(group, []) or []:
                    name = str(metric.get("name") or "")
                    if not allowed(name):
                        continue
                    try:
                        player["metrics"][group][name].append((float(metric.get("value")), minutes))
                    except (TypeError, ValueError):
                        continue
    candidates = []
    for player in players.values():
        if player["minutes"] < 90:
            continue
        position_name = str(player.get("position") or "").casefold()
        if "goalkeeper" in position_name or "kaleci" in position_name:
            continue
        compact, score = {}, 0.0
        for group, metrics in player["metrics"].items():
            compact[group] = {}
            for name, samples in metrics.items():
                lower = name.casefold()
                is_rate = "%" in name or "percentage" in lower
                raw = sum(value * minutes for value, minutes in samples) / sum(minutes for _, minutes in samples) if is_rate else sum(value for value, _ in samples)
                value = raw if is_rate else raw / player["minutes"] * 90
                compact[group][name] = {"value": round(value, 2), "basis": "rate" if is_rate else "per90"}
                positive = any(word in lower for word in ("tackle", "interception", "clearance", "recovery", "duel", "aerial", "block", "header"))
                negative = any(word in lower for word in ("lost", "error", "foul", "card"))
                score += raw * (1 if positive else -.5 if negative else 0)
        if any(compact.values()):
            player.update({"minutes": round(player["minutes"]), "metrics": compact, "defenseScore": round(score, 2)})
            candidates.append(player)
    candidates.sort(key=lambda row: (-row["defenseScore"], -row["minutes"]))
    shortlist, selected = candidates[:8], candidates[:2]

    def cell(name: str, value: Any, basis: str) -> dict[str, str]:
        numeric = float(value)
        shown = str(int(numeric)) if numeric.is_integer() else f"{numeric:.2f}".rstrip("0").rstrip(".")
        return {"label": name, "value": f"%{shown}" if basis == "rate" else shown}

    flat_team = [(str(row["name"]), row["valuePer90"], "rate" if row["isRate"] else "per90") for rows in team_evidence.values() for row in rows]
    rules = [
        ("Top Kazanımı ve Müdahale" if lang == "tr" else "Ball Winning & Intervention", ("tackle", "interception", "recovery")),
        ("Düello ve Hava Hakimiyeti" if lang == "tr" else "Duels & Aerial Control", ("duel", "aerial", "header")),
        ("Savunma Güvenliği" if lang == "tr" else "Defensive Security", ("clearance", "block", "error", "lost", "foul")),
    ]
    fallback_themes = []
    used: set[str] = set()
    for title, keywords in rules:
        ranked = sorted(flat_team, key=lambda row: (not any(word in row[0].casefold() for word in keywords), row[0]))
        chosen = [row for row in ranked if row[0] not in used][:4]
        if len(chosen) < 4:
            chosen_names = {row[0] for row in chosen}
            chosen.extend(row for row in ranked if row[0] not in chosen_names)
            chosen = chosen[:4]
        used.update(row[0] for row in chosen)
        metrics = [cell(*row) for row in chosen]
        analysis = ([f"{team_name}, bu savunma boyutunda topa müdahale sıklığı ile aksiyon başarısını birlikte taşıdığı ölçüde rakibin hücum devamlılığını kesebiliyor. Verilerin ilişkisi, savunmanın yalnızca olay hacmine değil bu aksiyonların tehlikeyi sona erdirme niteliğine de dayandığını gösteriyor.", f"Bu yapı farklı oyunculara yayıldığında savunma yükü tek bir hatta birikmiyor; düşük kalan tamamlayıcı göstergeler ise ilk müdahale sonrasında ikinci top ve alan güvenliğinin daha kırılgan hale gelebildiğine işaret ediyor."] if lang == "tr" else [f"{team_name} disrupts attacking continuity when intervention volume is supported by successful outcomes. The relationship shows a defensive structure that depends not only on action frequency, but on whether those actions actually end danger.", "When that output is distributed across roles, the defensive burden does not collect in one line; weaker supporting indicators point to greater exposure around second balls and the space after the first intervention."])
        fallback_themes.append({"title": title, "metrics": metrics, "analysis": analysis})
    def fallback_player_for(player: dict[str, Any]) -> dict[str, Any]:
        flat = [(name, detail["value"], detail["basis"]) for metrics in player["metrics"].values() for name, detail in metrics.items()]
        position = str(player.get("position") or "").casefold()
        keywords = (
            ("clearance", "aerial", "header", "block", "duel", "tackle", "interception", "recovery")
            if any(word in position for word in ("defender", "savunma")) else
            ("recovery", "interception", "duel", "tackle", "lost", "foul", "aerial", "block")
            if any(word in position for word in ("midfielder", "orta saha")) else
            ("recovery", "duel", "tackle", "interception", "aerial", "foul", "lost", "block")
        )
        def priority(row: tuple[str, Any, str]) -> tuple[int, str]:
            lower = row[0].casefold()
            return (next((index for index, word in enumerate(keywords) if word in lower), len(keywords)), row[0])
        ranked = sorted(flat, key=priority)
        def family(name: str) -> str:
            lower = name.casefold()
            for key, words in (("clearance", ("clearance",)), ("aerial", ("aerial", "header")), ("duel", ("duel",)), ("tackle", ("tackle",)), ("interception", ("interception",)), ("recovery", ("recovery",)), ("block", ("block",)), ("error", ("error",)), ("discipline", ("foul", "card")), ("loss", ("lost",))):
                if any(word in lower for word in words):
                    return key
            return lower
        chosen, used_families = [], set()
        for row in ranked:
            metric_family = family(row[0])
            if metric_family in used_families:
                continue
            chosen.append(row)
            used_families.add(metric_family)
            if len(chosen) == 4:
                break
        if len(chosen) < 4:
            chosen_names = {row[0] for row in chosen}
            chosen.extend(row for row in ranked if row[0] not in chosen_names)
        return {"playerId": player["playerId"], "name": player["name"], "imageUrl": player.get("imageUrl"), "position": player.get("position"), "minutes": player["minutes"], "metrics": [cell(*row) for row in chosen[:4]], "analysis": ([f"{player['name']}, müdahale, alan savunması ve düello katkısını kendi pozisyonunun sorumlulukları içinde birleştirerek takımın tehlikeyi karşılamasında anahtar rol oynuyor. Birbirinden farklı savunma aksiyonlarında üretim göstermesi, katkısının tek bir olay tipine bağlı kalmadığını ve savunma dizisinin birden fazla aşamasına yayıldığını gösteriyor.", "Oyuncunun hacim ile aksiyon başarısını birlikte taşıması, ilk müdahale sonrasında takımın savunma şeklini koruyabilmesini destekliyor. Zayıf kalan tamamlayıcı gösterge ise profilin hangi savunma anında daha fazla desteğe ihtiyaç duyduğunu belirginleştiriyor; böylece yorum yalnızca toplam aksiyon sayısına dayanmıyor."] if lang == "tr" else [f"{player['name']} combines intervention, space defence and duel output within the responsibilities of the position. Production across distinct defensive actions shows an influence that is not dependent on one event type and reaches multiple stages of the defensive sequence.", "Carrying volume together with action success supports the team's ability to retain its defensive shape after the first intervention. The weaker complementary indicator identifies where the profile requires more support rather than reducing the assessment to action totals."])}

    fallback_players = [fallback_player_for(player) for player in selected]
    fallback = {"themes": fallback_themes[:2], "players": fallback_players}
    if not team_evidence or len(selected) < 2 or not os.getenv("OPENAI_API_KEY"):
        return _profile_output(fallback, build_narratives)
    try:
        from langchain_openai import ChatOpenAI
        response = ChatOpenAI(model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")), api_key=os.environ["OPENAI_API_KEY"], temperature=.25, timeout=120, max_retries=1).invoke([
            ("system", _profile_prompt("You are ScoutWise Enterprise's senior defensive football analyst. Return only valid JSON with exactly themes and players. Every analysis field MUST be a JSON array of complete strings, never one string. Never select a goalkeeper and never use saves or goalkeeper save data. All volume values supplied are per 90; never write /90. Create exactly two dynamic, non-overlapping defensive dimensions. Every theme must have title, exactly FOUR single-metric cells, and an analysis array of exactly ONE professional bullet of 20-30 words. Interpret the team's defensive identity with the same depth used in a professional attacking profile: connect the complete evidence across ball winning, interventions, duels, aerial control, recoveries, blocks, clearances, errors and discipline; lead with what the team repeatedly does, then explain security, exposure, efficiency or dependency. Discuss only the supplied defensive evidence positively and directly. Never explain that an excluded, unavailable or attacking metric is not a defensive measure; omit it silently and analyse what is present. Return exactly two key defensive outfield players copied from the shortlist, including exact playerId. Every player must have exactly FOUR single-metric cells selected independently for that player's own position and distinctive evidence. Avoid filling a card with variants of one family such as aerial total, aerial won and aerial lost; cover four different defensive facets whenever data permits. The two players must not automatically receive the same four metrics. Every player analysis must be a JSON array of exactly ONE player-specific bullet of 20-30 words. Consider all supplied metrics internally, including metrics not displayed. Each bullet must lead with a football conclusion, contain at most one short numeric fact, and interpret relationships rather than define or list metrics. Do not invent pressing schemes, tactical intent, benchmarks or causation. Turkish output must use natural professional terminology and translate all metric names. No markdown.", build_narratives)),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nTeam: {team_name}\nTeam defensive metrics: {json.dumps(team_evidence, ensure_ascii=False)}\nDefensive player shortlist: {json.dumps(shortlist, ensure_ascii=False, default=str)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        for entry in [*(parsed.get("themes") or []), *(parsed.get("players") or [])]:
            if isinstance(entry, dict):
                entry["analysis"] = _mobile_profile_analysis(entry.get("analysis"))
        generated_themes = parsed.get("themes") or []
        generated_players = parsed.get("players") or []
        by_id = {str(player["playerId"]): player for player in shortlist}
        by_name = {str(player["name"]).strip().casefold(): player for player in shortlist}
        def analysis_list(value: Any) -> list[str]:
            if not isinstance(value, list):
                return []
            return [str(text).strip() for text in value[:3] if isinstance(text, str) and text.strip()]

        output_players = []
        for item in generated_players[:2]:
            source = by_id.get(str(item.get("playerId"))) or by_name.get(str(item.get("name") or "").strip().casefold())
            if not source:
                continue
            metrics = [{"label": str(metric.get("label") or ""), "value": str(metric.get("value") or "")} for metric in (item.get("metrics") or [])[:4] if isinstance(metric, dict) and allowed(metric.get("label"))]
            output_players.append({"playerId": source["playerId"], "name": source["name"], "imageUrl": source.get("imageUrl"), "position": source.get("position"), "minutes": source["minutes"], "metrics": metrics, "analysis": analysis_list(item.get("analysis"))})
        themes = [{"title": str(item.get("title") or ""), "metrics": [{"label": str(metric.get("label") or ""), "value": str(metric.get("value") or "")} for metric in (item.get("metrics") or [])[:4] if isinstance(metric, dict) and allowed(metric.get("label"))], "analysis": analysis_list(item.get("analysis"))} for item in generated_themes[:2] if isinstance(item, dict)]
        valid_themes = len(themes) == 2 and all(len(item["metrics"]) == 4 and (not build_narratives or len(item["analysis"]) >= 1) for item in themes)
        valid_players = len(output_players) == 2 and all(len(item["metrics"]) == 4 and (not build_narratives or len(item["analysis"]) >= 1) for item in output_players)
        if valid_themes and valid_players:
            return _profile_output({"themes": themes, "players": output_players}, build_narratives)
        repaired_themes = []
        for index, fallback_theme in enumerate(fallback_themes[:2]):
            generated = themes[index] if index < len(themes) else {}
            metrics = generated.get("metrics") or []
            analysis = analysis_list(generated.get("analysis"))
            repaired_themes.append({"title": str(generated.get("title") or fallback_theme["title"]), "metrics": metrics if len(metrics) == 4 else fallback_theme["metrics"], "analysis": analysis if len(analysis) >= 1 else fallback_theme["analysis"]})
        repaired_players = []
        for generated in output_players:
            source = by_id.get(str(generated["playerId"]))
            if not source:
                continue
            fallback_player = fallback_player_for(source)
            metrics = generated.get("metrics") or []
            analysis = analysis_list(generated.get("analysis"))
            repaired_players.append({**fallback_player, "metrics": metrics if len(metrics) == 4 else fallback_player["metrics"], "analysis": analysis if len(analysis) >= 1 else fallback_player["analysis"]})
        repaired_ids = {str(player["playerId"]) for player in repaired_players}
        repaired_players.extend(player for player in fallback_players if str(player["playerId"]) not in repaired_ids)
        print(f"[enterprise_team_report] event=defense_profile_validation_repair themes={len(themes)} players={len(output_players)} valid_themes={valid_themes} valid_players={valid_players}")
        return _profile_output({"themes": repaired_themes, "players": repaired_players[:2]}, build_narratives)
    except Exception as exc:
        print(f"[enterprise_team_report] event=defense_profile_fallback error={exc}")
    return _profile_output(fallback, build_narratives)



def build_team_report_score_flow_profile(
    reports: list[dict[str, Any]], team_id: int, lang: str = "en", build_narratives: bool = True
) -> dict[str, Any]:
    """Aggregate time and outcome-changing events by level/ahead/behind game state."""
    state_order = ("ahead", "level", "behind")
    totals = {
        key: {"minutes": 0.0, "segments": 0, "matchesEntered": 0, "goalsFor": 0, "goalsAgainst": 0, "exitsPositive": 0, "exitsNegative": 0, "toAhead": 0, "toLevel": 0, "toBehind": 0, "finalWins": 0, "finalDraws": 0, "finalLosses": 0}
        for key in state_order
    }
    team_name = next((str(team.get("name") or "") for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == int(team_id)), "Takım" if lang == "tr" else "The team")

    def state(team_score: int, opponent_score: int) -> str:
        return "ahead" if team_score > opponent_score else "behind" if team_score < opponent_score else "level"

    for report in reports:
        teams = report.get("teams") or []
        analysed = next((team for team in teams if int(team.get("id") or 0) == int(team_id)), None)
        if not analysed:
            continue
        home = str(analysed.get("location") or "").casefold() == "home"
        fixture = report.get("fixture") or {}
        try:
            duration = max(90.0, float(fixture.get("length") or 90))
        except (TypeError, ValueError):
            duration = 90.0
        goals = []
        for event in report.get("events") or []:
            event_type = str(event.get("type") or "").casefold()
            result = str(event.get("result") or "")
            has_score = bool(re.fullmatch(r"\s*\d+\s*[-:]\s*\d+\s*", result))
            if ("goal" not in event_type and not has_score) or any(word in event_type for word in ("disallow", "cancel", "miss")):
                continue
            try:
                minute = float(event.get("minute") or 0) + float(event.get("extra_minute") or 0)
            except (TypeError, ValueError):
                continue
            if minute < 0:
                continue
            goals.append({"minute": minute, "teamId": int(event.get("team_id") or 0), "result": result})
            duration = max(duration, minute)
        goals.sort(key=lambda item: item["minute"])
        team_score = opponent_score = 0
        cursor = 0.0
        entered = {"level"}
        current = "level"
        totals[current]["segments"] += 1
        for goal in goals:
            minute = min(duration, max(cursor, goal["minute"]))
            totals[current]["minutes"] += minute - cursor
            parsed_score = re.fullmatch(r"\s*(\d+)\s*[-:]\s*(\d+)\s*", goal["result"])
            if parsed_score:
                home_score, away_score = int(parsed_score.group(1)), int(parsed_score.group(2))
                next_team_score, next_opponent_score = (home_score, away_score) if home else (away_score, home_score)
                scoring_for = next_team_score > team_score
                team_score, opponent_score = next_team_score, next_opponent_score
            else:
                scoring_for = goal["teamId"] == int(team_id)
            totals[current]["goalsFor" if scoring_for else "goalsAgainst"] += 1
            if not parsed_score and scoring_for:
                team_score += 1
            elif not parsed_score:
                opponent_score += 1
            next_state = state(team_score, opponent_score)
            if next_state != current:
                positive = (current == "level" and next_state == "ahead") or (current == "behind" and next_state == "level")
                totals[current]["exitsPositive" if positive else "exitsNegative"] += 1
                totals[current][f"to{next_state.title()}"] += 1
                totals[next_state]["segments"] += 1
                entered.add(next_state)
                current = next_state
            cursor = minute
        totals[current]["minutes"] += max(0.0, duration - cursor)
        for key in entered:
            totals[key]["matchesEntered"] += 1
            outcome_key = "finalWins" if team_score > opponent_score else "finalLosses" if team_score < opponent_score else "finalDraws"
            totals[key][outcome_key] += 1

    total_minutes = sum(row["minutes"] for row in totals.values()) or 1.0
    labels = {
        "level": "Beraberlikte" if lang == "tr" else "Level",
        "ahead": "Öndeyken" if lang == "tr" else "Ahead",
        "behind": "Gerideyken" if lang == "tr" else "Behind",
    }
    states = []
    for key in state_order:
        row = totals[key]
        states.append({
            "key": key, "label": labels[key], "minutes": round(row["minutes"], 1),
            "share": round(row["minutes"] / total_minutes * 100, 1),
            "averageSpell": round(row["minutes"] / row["segments"], 1) if row["segments"] else 0,
            "segments": row["segments"], "matchesEntered": row["matchesEntered"],
            "goalsFor": row["goalsFor"], "goalsAgainst": row["goalsAgainst"],
            "exitsPositive": row["exitsPositive"], "exitsNegative": row["exitsNegative"],
            "toAhead": row["toAhead"], "toLevel": row["toLevel"], "toBehind": row["toBehind"],
            "finalWins": row["finalWins"], "finalDraws": row["finalDraws"], "finalLosses": row["finalLosses"],
        })
    if not build_narratives:
        return {"states": states}
    fallback = {
        "level": (f"{team_name}, skor eşitken geçirdiği bölümlerde dengeyi bozacak ilk aksiyonu ararken maçın temel başlangıç yapısını koruyor. Bu senaryodaki gol üretimi ile yenilen gollerin yönü, takımın eşit oyundan üstünlüğe mi yoksa reaksiyon vermesi gereken bir duruma mı daha sık geçtiğini ortaya koyuyor." if lang == "tr" else f"While level, {team_name} preserves the match's base structure while searching for the first action that changes the balance. The direction of goals in this state shows whether level games more often became leads or demanded a response."),
        "ahead": (f"{team_name}, öne geçtiği dönemlerde üstünlüğü koruma süresi ile bu sırada verdiği gol reaksiyonunun ilişkisi üzerinden değerlendiriliyor. Uzun ve kesintisiz önde kalma periyotları skor kontrolüne, kısa periyotlar ve eşitliğe dönüşler ise avantajın savunulmasındaki kırılganlığa işaret ediyor." if lang == "tr" else f"When ahead, {team_name} is assessed through the relationship between lead duration and goals conceded in that state. Long uninterrupted spells point to score control, while short spells and returns to level reveal fragility in protecting the advantage."),
        "behind": (f"{team_name}, geride kaldığı bölümlerde eşitliği yeniden kurabilme sıklığı ve bu durumun ne kadar sürdüğüyle reaksiyon kapasitesini gösteriyor. Geride kalma süresinin uzaması hücum baskısının skora dönüşmekte zorlandığını, olumlu çıkışların tekrarlanması ise maç içinde yeniden denge kurabildiğini gösteriyor." if lang == "tr" else f"When behind, {team_name} shows its response capacity through the frequency of restoring parity and the duration of the deficit. Extended trailing spells suggest pressure struggled to alter the score; repeated positive exits indicate an ability to rebuild balance."),
    }
    if not reports or not os.getenv("OPENAI_API_KEY"):
        return {"states": states, "perspectives": fallback}
    try:
        from langchain_openai import ChatOpenAI
        narrative_states = [
            {
                key: value for key, value in row.items()
                if key in ("key", "label") or (isinstance(value, (int, float)) and value > 0)
            }
            for row in states
        ]
        response = ChatOpenAI(model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")), api_key=os.environ["OPENAI_API_KEY"], temperature=.2).invoke([
            ("system", "You are ScoutWise Enterprise's senior game-state analyst. Return only valid JSON with exactly level, ahead and behind. Write one concise 30-35 word professional interpretation for each state in the requested language. Fields with a zero value have been deliberately omitted from the evidence. NEVER mention, infer, enumerate or paraphrase any zero or absent event: do not write '0 times', 'none', 'did not occur', 'no comeback', 'without a win', 'bulunmadı', 'gerçekleşmedi', 'ulaşmadı' or equivalents. Discuss only transitions and outcomes that actually occurred. Compare the meaningful time share, spell length, goals and non-zero transitions. For behind, discuss comeback wins only when finalWins is present. For level, interpret only the supplied moves ahead, falls behind or level finishes. For ahead, interpret only supplied final outcomes. Lead with a team-specific conclusion about score control, initiative or response capacity. Use at most TWO numerical facts in the entire paragraph and make the football inference dominant; never enumerate the evidence or give a statistical recap. Never define states, mention total match count, invent tactics or causation, give recommendations, or explain missing/irrelevant data. No markdown."),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nTeam: {team_name}\nNon-zero score-state evidence only: {json.dumps(narrative_states, ensure_ascii=False)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        perspectives = {}
        forbidden_absence = re.compile(r"(?:\b0(?:[.,]0+)?\b|bulunmad[ıi]|gerçekleşmed[ıi]|ulaşmad[ıi]|none\b|did not occur|no comeback|without a win)", re.I)
        for key in state_order:
            value = str(parsed.get(key) or "").strip()
            numeric_facts = re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)?", value)
            perspectives[key] = value if value and len(numeric_facts) <= 2 and not forbidden_absence.search(value) else fallback[key]
        return {"states": states, "perspectives": perspectives}
    except Exception as exc:
        print(f"[enterprise_team_report] event=score_flow_profile_fallback error={exc}")
        return {"states": states, "perspectives": fallback}


def build_team_report_strengths(
    reports: list[dict[str, Any]],
    team_id: int,
    team_metrics: dict[str, list[dict[str, Any]]],
    lang: str = "en",
    build_narratives: bool = True,
) -> dict[str, Any]:
    """Identify three evidence-supported team strengths from all metric categories."""
    team_name = next((str(team.get("name") or "") for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == int(team_id)), "Takım" if lang == "tr" else "The team")
    evidence = {
        group: [{"name": row.get("name"), "valuePer90": row.get("perMatch"), "isRate": row.get("aggregation") == "average"} for row in rows if row.get("name") and row.get("perMatch") is not None]
        for group, rows in team_metrics.items() if rows
    }
    flat = [(group, str(row["name"]), row["valuePer90"], "rate" if row["isRate"] else "per90") for group, rows in evidence.items() for row in rows]

    def metric_cell(name: str, value: Any, basis: str) -> dict[str, str]:
        numeric = float(value)
        shown = str(int(numeric)) if numeric.is_integer() else f"{numeric:.2f}".rstrip("0").rstrip(".")
        return {"label": name, "value": f"%{shown}" if basis == "rate" else shown}

    rules = [
        ("Üretim Gücü" if lang == "tr" else "Production Strength", ("goal", "shot", "expected", "chance", "assist")),
        ("Oyun Kontrolü" if lang == "tr" else "Game Control", ("pass", "possession", "touch", "cross", "dribbl")),
        ("Savunma Dayanıklılığı" if lang == "tr" else "Defensive Resilience", ("tackle", "interception", "recovery", "duel", "aerial", "clearance", "block")),
    ]
    used: set[tuple[str, str]] = set()
    fallback_themes = []
    for title, keywords in rules:
        ranked = sorted(flat, key=lambda row: (not any(word in row[1].casefold() for word in keywords), row[0], row[1]))
        chosen = []
        for row in ranked:
            key = (row[0], row[1])
            if key in used:
                continue
            chosen.append(row)
            used.add(key)
            if len(chosen) == 4:
                break
        fallback_themes.append({
            "title": title,
            "metrics": [metric_cell(name, value, basis) for _, name, value, basis in chosen],
            "analysis": ([
                f"{team_name}, bu performans boyutunda hacim ile verimliliği aynı üretim zincirinde birleştirebildiği için seçili maçlar boyunca tekrar edebilen bir avantaj oluşturuyor. Görünür dört gösterge yönü özetlerken, tamamlayıcı takım verileri bu gücün tek bir aksiyona bağlı kalmadan farklı oyun anlarına yayıldığını gösteriyor.",
                "Bu güçlü yönün asıl değeri yalnızca yüksek aksiyon sayısı değil, ilgili göstergelerin birbirini desteklemesidir. Üretim, başarı oranı ve sonuç çıktısı aynı doğrultuda hareket ettiğinde takım bu alandaki üstün niteliğini daha istikrarlı biçimde oyuna yansıtabiliyor.",
            ] if lang == "tr" else [
                f"{team_name} creates a repeatable advantage in this performance dimension by connecting volume with efficiency across the same production chain. The four visible indicators summarize the direction, while supporting team evidence shows a strength distributed across different game moments rather than one action type.",
                "The value of this strength lies not only in action volume, but in the way the related indicators reinforce one another. When production, success rate and outcome move together, the team expresses this positive characteristic more consistently.",
            ]),
        })
    fallback = {"themes": fallback_themes[:2]}
    if not flat or not os.getenv("OPENAI_API_KEY"):
        return _profile_output(fallback, build_narratives)
    try:
        from langchain_openai import ChatOpenAI
        response = ChatOpenAI(model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")), api_key=os.environ["OPENAI_API_KEY"], temperature=.25, timeout=120, max_retries=1).invoke([
            ("system", _profile_prompt("You are ScoutWise Enterprise's senior team-performance analyst. Return only valid JSON with exactly themes. themes must be an array of exactly TWO dynamically named, non-overlapping team strengths inferred from ALL supplied metric categories. Do not use fixed generic headings. Every theme must contain exactly title, metrics and analysis. metrics must contain exactly FOUR objects with exactly label and value; each cell contains one metric and one numeric value only. Select the four most diagnostic visible metrics for that strength, but use every related supplied metric internally when interpreting it. analysis MUST be a JSON array of exactly ONE complete professional string of 20-30 words. Lead with a direct team-specific conclusion about what the team repeatedly does well, connect volume, efficiency and outcome where supported, and explain the practical football advantage. At least 80% of each bullet must be inference; use at most one short numerical fact per bullet and never enumerate a statistical line. Strengths must be positively framed but evidence-led. Do not claim superiority to external teams or leagues because no benchmark is supplied. Never invent tactics, roles, causation, recommendations or unavailable context. All volume metrics are already normalized per 90; never write /90. Rate metrics retain percentages. In Turkish use natural professional football terminology and translate displayed metric labels. No markdown.", build_narratives)),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nTeam: {team_name}\nAll aggregated team metrics: {json.dumps(evidence, ensure_ascii=False)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        generated = parsed.get("themes") if isinstance(parsed, dict) and isinstance(parsed.get("themes"), list) else []
        themes = []
        for index, fallback_theme in enumerate(fallback_themes[:2]):
            item = generated[index] if index < len(generated) and isinstance(generated[index], dict) else {}
            metrics = [{"label": str(row.get("label") or ""), "value": str(row.get("value") or "")} for row in (item.get("metrics") or [])[:4] if isinstance(row, dict)]
            analysis_value = item.get("analysis")
            analysis = _mobile_profile_analysis(analysis_value)
            themes.append({"title": str(item.get("title") or fallback_theme["title"]).strip(), "metrics": metrics if len(metrics) == 4 else fallback_theme["metrics"], "analysis": analysis if len(analysis) >= 1 else fallback_theme["analysis"]})
        if len(generated) != 2:
            print(f"[enterprise_team_report] event=strengths_validation_repair themes={len(generated)}")
        return _profile_output({"themes": themes}, build_narratives)
    except Exception as exc:
        print(f"[enterprise_team_report] event=strengths_fallback error={exc}")
        return _profile_output(fallback, build_narratives)


def build_team_report_weaknesses(
    reports: list[dict[str, Any]],
    team_id: int,
    team_metrics: dict[str, list[dict[str, Any]]],
    strengths: dict[str, Any] | None = None,
    lang: str = "en",
    build_narratives: bool = True,
) -> dict[str, Any]:
    """Identify three evidence-supported team vulnerabilities from all metric categories."""
    team_name = next((str(team.get("name") or "") for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == int(team_id)), "Takım" if lang == "tr" else "The team")
    evidence = {
        group: [{"name": row.get("name"), "valuePer90": row.get("perMatch"), "isRate": row.get("aggregation") == "average", "evidenceDirection": (
            "adverse" if any(word in str(row.get("name") or "").casefold() for word in ("missed", "lost", "error", "foul", "card", "off target", "conceded")) else
            "positive_output" if any(word in str(row.get("name") or "").casefold() for word in ("expected goal", "expected assist", "goals", "assists", "accurate", "won", "on target", "chances created")) else
            "contextual"
        )} for row in rows if row.get("name") and row.get("perMatch") is not None]
        for group, rows in team_metrics.items() if rows
    }
    flat = [(group, str(row["name"]), row["valuePer90"], "rate" if row["isRate"] else "per90") for group, rows in evidence.items() for row in rows]
    confirmed_strengths = strengths or {}
    strength_metric_names = {
        str(metric.get("label") or "").strip()
        for theme in confirmed_strengths.get("themes") or [] if isinstance(theme, dict)
        for metric in theme.get("metrics") or [] if isinstance(metric, dict)
        if str(metric.get("label") or "").strip()
    }
    weakness_visible_flat = [
        row for row in flat
        if row[1] not in strength_metric_names
        and not any(word in row[1].casefold() for word in ("expected goal", "expected assist", "xg", "xa"))
    ]

    def metric_cell(name: str, value: Any, basis: str) -> dict[str, str]:
        numeric = float(value)
        shown = str(int(numeric)) if numeric.is_integer() else f"{numeric:.2f}".rstrip("0").rstrip(".")
        return {"label": name, "value": f"%{shown}" if basis == "rate" else shown}

    rules = [
        ("Son Aksiyon Verimsizliği" if lang == "tr" else "Final-Action Inefficiency", ("miss", "off target", "conversion", "chance", "shot", "expected")),
        ("Top Güvenliği ve Süreklilik" if lang == "tr" else "Ball Security & Continuity", ("lost", "error", "pass", "possession", "dribbl")),
        ("Savunma Kırılganlığı" if lang == "tr" else "Defensive Vulnerability", ("error", "foul", "card", "duel lost", "aerial lost", "goal conceded")),
    ]
    used: set[tuple[str, str]] = set()
    fallback_themes = []
    for title, keywords in rules:
        ranked = sorted(weakness_visible_flat, key=lambda row: (not any(word in row[1].casefold() for word in keywords), row[0], row[1]))
        chosen = []
        for row in ranked:
            key = (row[0], row[1])
            if key in used:
                continue
            chosen.append(row)
            used.add(key)
            if len(chosen) == 4:
                break
        fallback_themes.append({
            "title": title,
            "metrics": [metric_cell(name, value, basis) for _, name, value, basis in chosen],
            "analysis": ([
                f"{team_name}, bu performans boyutunda üretim ile sonuç arasındaki bağlantıyı aynı istikrarla koruyamadığında oyun içindeki avantajını kaybediyor. Görünür göstergeler sorunun yönünü özetlerken, tamamlayıcı takım verileri kırılganlığın tek bir aksiyondan değil birbirini besleyen karar ve verimlilik kayıplarından oluştuğunu gösteriyor.",
                "Bu zayıflık, yalnızca olumsuz olay hacminden değil, ilk aksiyon sonrasında takımın dengeyi yeniden kurmakta zorlanmasından değer kazanıyor. İlgili başarı oranları ile kayıp göstergeleri ayrıştığında sorun daha sık tekrar ediyor ve takımın sonraki oyun fazına güvenli geçişini sınırlıyor.",
            ] if lang == "tr" else [
                f"{team_name} loses part of its in-game advantage when the connection between production and outcome cannot be sustained in this dimension. The visible indicators summarize the direction, while supporting evidence shows a vulnerability built from related decision and efficiency losses rather than one isolated action.",
                "The weakness is shaped not only by negative-event volume, but by difficulty restoring balance after the first action. When success rates and loss indicators separate, the issue becomes more repeatable and limits secure progression into the next phase.",
            ]),
        })
    fallback = {"themes": fallback_themes[:2]}
    if not flat or not os.getenv("OPENAI_API_KEY"):
        return _profile_output(fallback, build_narratives)
    try:
        from langchain_openai import ChatOpenAI
        response = ChatOpenAI(model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")), api_key=os.environ["OPENAI_API_KEY"], temperature=.25, timeout=120, max_retries=1).invoke([
            ("system", _profile_prompt("You are ScoutWise Enterprise's senior team-performance analyst. Return only valid JSON with exactly themes. themes must be an array of exactly TWO dynamically named, non-overlapping team weaknesses inferred from ALL supplied metric categories. CONFIRMED_STRENGTHS is binding editorial context: never describe a confirmed strength, its visible metrics, or the same causal relationship as a weakness. A genuine trade-off may be discussed only when distinct adverse evidence explicitly proves its separate cost. Do not use fixed generic headings. Every theme must contain exactly title, metrics and analysis. metrics must contain exactly FOUR objects with exactly label and value; copy each metric label EXACTLY from the supplied evidence and use one numeric value only. Select the four most diagnostic visible metrics for that weakness, but use every related supplied metric internally when interpreting it. evidenceDirection is binding: adverse metrics may directly support a weakness; contextual metrics require a demonstrated relationship; positive_output metrics must NEVER be treated as weak merely because their value seems low or high without an external benchmark. Expected-goal production—including set-play expected goals—is positive chance-production evidence and must not appear as weakness evidence unless a directly supplied matching outcome proves under-conversion. Do not combine low crossing accuracy with strong set-play expected-goal production and call both evidence of the same weakness. analysis MUST be a JSON array of exactly ONE complete professional string of 20-30 words. Lead with a direct team-specific conclusion about the recurring vulnerability, connect volume, efficiency, errors and outcome where supported, and explain its practical football cost. At least 80% of each bullet must be inference; use at most one short numerical fact per bullet and never enumerate a statistical line. Diagnose weaknesses directly without praise, recommendations or softening language. Do not claim inferiority to external teams or leagues because no benchmark is supplied; identify internal inefficiency, imbalance, dependency or repeated exposure from relationships within the supplied evidence. Never invent tactics, roles, causation or unavailable context. All volume metrics are already normalized per 90; never write /90. Rate metrics retain percentages. In Turkish use established professional football terminology. Never use 'teslimat' for a cross or set-piece service; use 'servis', 'orta' or 'topun doğru bölgeye gönderilmesi'. Never use 'sürtünme'; use 'verim kaybı', 'üretim kopukluğu' or 'tehdide dönüşüm sorunu'. Never explain missing or irrelevant metrics. No markdown.", build_narratives)),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nTeam: {team_name}\nCONFIRMED_STRENGTHS: {json.dumps(confirmed_strengths, ensure_ascii=False)}\nAll aggregated team metrics: {json.dumps(evidence, ensure_ascii=False)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        generated = parsed.get("themes") if isinstance(parsed, dict) and isinstance(parsed.get("themes"), list) else []
        def naturalize(value: Any) -> str:
            text = str(value or "").strip()
            if lang == "tr":
                text = re.sub(r"\bteslimat(?:ı|ın|lar|ları|larda|lardan)?\b", "servis", text, flags=re.I)
                text = re.sub(r"\bsürtünme\b", "verim kaybı", text, flags=re.I)
            return text

        valid_metric_names = {name for _, name, _, _ in weakness_visible_flat}
        themes = []
        for index, fallback_theme in enumerate(fallback_themes[:2]):
            item = generated[index] if index < len(generated) and isinstance(generated[index], dict) else {}
            metrics = [{"label": str(row.get("label") or ""), "value": str(row.get("value") or "")} for row in (item.get("metrics") or [])[:4] if isinstance(row, dict)]
            analysis_value = item.get("analysis")
            analysis = [naturalize(text) for text in analysis_value[:3] if isinstance(text, str) and text.strip()] if isinstance(analysis_value, list) else []
            metrics_valid = len(metrics) == 4 and all(metric["label"] in valid_metric_names for metric in metrics)
            themes.append({"title": naturalize(item.get("title") or fallback_theme["title"]), "metrics": metrics if metrics_valid else fallback_theme["metrics"], "analysis": analysis if len(analysis) >= 1 else fallback_theme["analysis"]})
        if len(generated) != 2:
            print(f"[enterprise_team_report] event=weaknesses_validation_repair themes={len(generated)}")
        return _profile_output({"themes": themes}, build_narratives)
    except Exception as exc:
        print(f"[enterprise_team_report] event=weaknesses_fallback error={exc}")
        return _profile_output(fallback, build_narratives)


def build_team_report_overview(
    reports: list[dict[str, Any]],
    team_id: int,
    team_perspectives: dict[str, str],
    player_perspectives: dict[str, dict[str, str]],
    momentum_perspectives: dict[str, str],
    regional_perspective: str,
    attack_profile: dict[str, Any],
    defense_profile: dict[str, Any],
    score_flow_profile: dict[str, Any],
    strengths: dict[str, Any],
    weaknesses: dict[str, Any],
    lang: str = "en",
) -> list[dict[str, Any]]:
    """Synthesize every team-report page into a compact section-by-section overview."""
    team = next((team for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == int(team_id)), {})
    team_name = str(team.get("name") or ("Takım" if lang == "tr" else "The team"))
    formations = [str(team.get("formation") or "") for report in reports for team in report.get("teams") or [] if int(team.get("id") or 0) == int(team_id) and team.get("formation")]
    lineups = [row for report in reports for row in report.get("lineups") or [] if int(row.get("team_id") or 0) == int(team_id)]
    fixtures = [{"fixture": report.get("fixture"), "summary": report.get("summary")} for report in reports]
    section_titles = {
        "team_card": "Takım Kartı" if lang == "tr" else "Team Card",
        "form_results": "Form ve Sonuçlar" if lang == "tr" else "Form & Results",
        "squad_tactics": "Kadro Profili ve Taktik" if lang == "tr" else "Squad Profile & Tactics",
        "momentum": "Momentum" if lang == "tr" else "Momentum",
        "score_flow": "Skor Akış Profili" if lang == "tr" else "Score Flow Profile",
        "regional": "Bölgesel Oyun Dağılımı" if lang == "tr" else "Regional Play Distribution",
        "attack": "Hücum Profili" if lang == "tr" else "Attacking Profile",
        "defense": "Savunma Profili" if lang == "tr" else "Defensive Profile",
        "strengths": "Güçlü Yönler" if lang == "tr" else "Strengths",
        "weaknesses": "Zayıf Yönler" if lang == "tr" else "Weaknesses",
        "players": "Oyuncu Verileri" if lang == "tr" else "Player Data",
        "team_data": "Takım Verileri" if lang == "tr" else "Team Data",
    }
    evidence = {
        "team_card": {"team": team, "league": next((report.get("league") for report in reports if report.get("league")), None), "venue": next((report.get("venue") for report in reports if report.get("venue")), None)},
        "form_results": fixtures,
        "squad_tactics": {"formations": formations, "players_with_appearances": len({str(row.get("player_id") or row.get("player_name")) for row in lineups})},
        "momentum": momentum_perspectives,
        "score_flow": score_flow_profile,
        "regional": regional_perspective,
        "attack": attack_profile,
        "defense": defense_profile,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "players": player_perspectives,
        "team_data": team_perspectives,
    }
    fallback = [
        {"key": key, "category": title, "summary": (
            f"{team_name} için bu bölümdeki seçili maç verileri ve ScoutWise çıkarımları, takım profilinin ilgili performans boyutunu özetliyor."
            if lang == "tr" else
            f"The selected-match evidence and ScoutWise conclusions in this section summarize the relevant performance dimension of {team_name}."
        ), "subBullets": []}
        for key, title in section_titles.items()
    ]
    if not reports or not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from langchain_openai import ChatOpenAI
        keys = list(section_titles)
        response = ChatOpenAI(model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")), api_key=os.environ["OPENAI_API_KEY"], temperature=.2).invoke([
            ("system", "You are ScoutWise Enterprise's lead team-report editor. Return only one valid JSON object whose top-level keys exactly match REQUIRED_KEYS. Each value must contain exactly summary and subBullets. summary must be a polished 20-32 word synthesis of that report page's decisive football conclusion, not a description of what the page contains. subBullets must be an array of zero to two objects with exactly label and text; label is a concise 2-4 word insight name and text is an 8-16 word supporting conclusion. Synthesize the supplied existing evidence and ScoutWise interpretations without contradicting them. In particular, Strengths and Weaknesses must remain mutually consistent: never present the same metric relationship as both an advantage and a vulnerability. Cover the team's identity across every section, prioritize inference over facts, and avoid repeating the same conclusion across pages. Team Card may be factual. Form & Results summarizes outcome pattern; Squad Profile & Tactics summarizes usage and formation evidence; Momentum summarizes first/second-half pressure; Score Flow summarizes level/ahead/behind control; Regional summarizes recurring spatial character; Attack and Defense summarize their strongest identity findings; Strengths synthesizes the three most repeatable positive qualities; Weaknesses synthesizes the three most important recurring vulnerabilities; Player Data summarizes standout/development patterns without listing players unnecessarily; Team Data summarizes the broad statistical identity. Never mention missing data, fallback, API, match count, raw internal keys, unavailable evidence, recommendations or limitations. Do not invent tactics, causation or benchmarks. When the requested language is Turkish, use only natural Turkish football terminology except proper names and established metric abbreviations; never use foreign adjectives such as 'consequential'. No markdown."),
            ("human", f"Language: {'Turkish' if lang == 'tr' else 'English'}\nTeam: {team_name}\nREQUIRED_KEYS: {json.dumps(keys)}\nSection titles: {json.dumps(section_titles, ensure_ascii=False)}\nExisting report evidence: {json.dumps(evidence, ensure_ascii=False, default=str)}"),
        ])
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I))
        if not isinstance(parsed, dict):
            raise ValueError("team overview response root was not a JSON object")
        rows = []
        for index, (key, title) in enumerate(section_titles.items()):
            item = parsed.get(key) if isinstance(parsed.get(key), dict) else {}
            def naturalize_overview(value: Any) -> str:
                text = str(value or "").strip()
                if lang == "tr":
                    replacements = {
                        r"\bconsequential\b": "belirleyici",
                        r"\bfriction\b|\bsürtünme\b": "verim kaybı",
                        r"\bdelivery\b|\bteslimat\b": "servis",
                    }
                    for pattern, replacement in replacements.items():
                        text = re.sub(pattern, replacement, text, flags=re.I)
                return text
            summary = naturalize_overview(item.get("summary") or "")
            bullets = [{"label": naturalize_overview(row.get("label") or ""), "text": naturalize_overview(row.get("text") or "")} for row in (item.get("subBullets") or [])[:2] if isinstance(row, dict) and str(row.get("text") or "").strip()]
            rows.append({"key": key, "category": title, "summary": summary or fallback[index]["summary"], "subBullets": bullets})
        missing = [row["key"] for row in rows if row["summary"] == fallback[keys.index(row["key"])]["summary"]]
        if missing:
            print(f"[enterprise_team_report] event=overview_partial_fallback missing_keys={missing}")
        return rows
    except Exception as exc:
        print(f"[enterprise_team_report] event=overview_fallback error={exc}")
        return fallback
