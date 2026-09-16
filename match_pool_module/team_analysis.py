"""Three concise, evidence-based team insights for mobile post-match reports."""
from typing import Any
import os
import re
import json
import time
import threading
from collections import OrderedDict
from team_analysis_module.report import MatchReportError

_CACHE = OrderedDict()
_LOCK = threading.Lock()
_TTL_SECONDS = 1800

def cached_analysis(key):
    with _LOCK:
        item = _CACHE.get(key)
        if item and time.monotonic() - item[0] < _TTL_SECONDS:
            _CACHE.move_to_end(key)
            return item[1]
        _CACHE.pop(key, None)
    return None

def cache_analysis(key, value):
    with _LOCK:
        _CACHE[key] = (time.monotonic(), value)
        _CACHE.move_to_end(key)
        while len(_CACHE) > 128:
            _CACHE.popitem(last=False)

def build_team_analysis(report: dict[str, Any], lang: str, include_locked: bool = True) -> dict[str, list[dict[str, str]]]:
    expected_count = 3 if include_locked else 2
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
    if not teams or not os.getenv("OPENAI_API_KEY"):
        raise MatchReportError("Team analysis is unavailable")
    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"))
        language = "Turkish" if lang == "tr" else "English"
        response = ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"], temperature=0.25, timeout=180, max_retries=1)
        messages = [
            (
                "system",
                "You are ScoutWise's senior team-performance analyst, not a match-timeline narrator. Return only valid JSON keyed by the supplied team IDs. Each team value must be an array of exactly three objects with exactly header, text, and tone. tone must be exactly positive, negative, or neutral. Assign positive when the central conclusion is a strength, successful effect, superiority, or effective response; negative when it is a weakness, inefficiency, vulnerability, deterioration, or failed conversion; neutral when it is genuinely balanced or mixed. Classify the central conclusion, not isolated sentences. Select three distinct, dynamic headers from the evidence; never reuse a fixed template or generic headings such as 'General Analysis'. Each 2-5 word header must name a football characteristic or structural insight, not an event, score, minute, formation number, or chronological phase. Formations may appear factually only inside the text. Keep every text within 30-45 words, matching the depth and approximate visual length of the player scouting report's weakness explanations. Every text must connect at least two relevant pieces of evidence, identify the recurring team characteristic or mechanism they indicate, explain the match phase or game situation where it matters, then state its practical implication for control, progression, chance quality, defensive security, or adaptability. Prefer relationships such as volume versus efficiency, possession versus penetration, shot volume versus shot quality, pass security versus chance creation, duel output versus defensive exposure, and first-half versus second-half stability. Compare the team with its opponent where that sharpens the diagnosis. Across the three bullets, combine related evidence to cover: structure and player relationships; ball progression and control; chance creation and finishing profile; defensive behaviour and discipline; adaptability or the structural effect of substitutions. Do not retell goals, cards, substitutions, or score changes as a story. Events, timing, score flow, and pressure may be used only as supporting evidence for a broader team characteristic, never as the subject of a bullet. Enforce exact numerical logic: an unchanged value is stable, never an increase or decrease; only call a value higher or lower after checking both numbers; identify clearly whether a comparison is between teams, halves, totals, or subsets; never compare unrelated metrics. Never write a self-correction, contradiction, fragment, or construction equivalent to 'three to two, not...' inside the final text. If evidence is ambiguous, omit that comparison instead of repairing it in prose. Do not claim that a late substitute merely added freshness, that a change had limited impact, or that a team reacted well solely because of its timing or the final score; demonstrate any substitution effect through supplied position, metric, event, or pressure evidence. For Turkish, use natural professional football terminology: say 'oyuncu değişiklikleri' or contextually 'kadro hamleleri', never 'personel değişimleri'; use 'ikinci yarı', 'şut denemesi', 'ceza sahası', 'topla ilerleme', 'üretkenlik', and 'bitiricilik' naturally, and avoid literal translations or corporate vocabulary. Formation fields establish only a player's line and slot, not a specific tactical role. Never infer unsupported roles, causation, attack direction, pressing scheme, build-up pattern, or tactical intent. Do not invent data. Do not use markdown, bullet characters, recommendations, or raw pressure numbers."
                + (" No Pressure Index evidence is available: never mention pressure, momentum, dominance derived from pressure, pressure changes, or a Pressure Index." if not pressure else " Translate supplied pressure values into relative qualitative language."),
            ),
            ("human", f"Language: {language}\nFull match evidence by team:\n{json.dumps(compact, ensure_ascii=False, default=str)}"),
        ]
        if not include_locked:
            messages[0] = ('system', messages[0][1]
                .replace('exactly three', 'exactly two').replace('exactly 3', 'exactly 2')
                .replace('Select three distinct', 'Select distinct')
                .replace('Across the three bullets', 'Across the two bullets')
                + ' ACCESS CONTRACT: Select the same three distinct insights you would select for the full report internally, classify their tones using the rules above, and order them positive, neutral, negative. Return only the first TWO, with unchanged depth and word targets. Do not write the third insight. The first two may share a tone; do not force a positive or neutral conclusion unsupported by evidence.')
        response = response.invoke(messages)
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
                for row in rows[:expected_count]
                if str(row.get("header") or "").strip() and str(row.get("text") or "").strip()
            ]
            if len(cleaned) == expected_count:
                tone_order = {"positive": 0, "neutral": 1, "negative": 2}
                result[team_key] = sorted(
                    cleaned,
                    key=lambda item: tone_order.get(item.get("tone", "neutral"), 1),
                )
            else:
                raise MatchReportError("Incomplete team analysis")
        if not include_locked:
            for rows in result.values():
                rows.append({'header': '', 'text': '', 'tone': 'negative', 'locked': True})
        return result
    except Exception as exc:
        raise MatchReportError("Team analysis could not be generated") from None
