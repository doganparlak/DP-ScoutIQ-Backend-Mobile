import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Tuple

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from sqlalchemy import text

from api_module.utilities import get_db
from chatbot_module.tools import (
    collect_recent_human_constraints,
    extract_target_team_from_question,
    filter_players_by_seen,
    get_candidate_rejection_reason,
    has_required_discovery_fields,
    is_direct_player_lookup_request,
    is_generic_alternative_request,
    is_premium_allowed_club,
    is_premium_request,
    is_same_club,
    is_transfer_fallback_club,
    is_turkish,
    is_weak_generic_suggestion_request,
    player_matches_requested_position,
    request_allows_non_senior_squads,
    request_allows_turkish_entities,
    rewrite_position_reference_phrases,
    strip_target_team_from_question,
    summarize_doc_candidate,
    TRANSFER_FALLBACK_CLUBS,
)
from chatbot_module.tools_extensions import _score_candidate, build_player_payload_new
from report_module.utilities import norm_name


AGENTIC_LOOKUP_DEBUG = os.getenv("AGENTIC_LOOKUP_DEBUG", "1").lower() not in {"0", "false", "no", "off"}
AGENTIC_LOOKUP_VERBOSE = os.getenv("AGENTIC_LOOKUP_VERBOSE", "0").lower() in {"1", "true", "yes", "on"}
AGENTIC_QUALITY_DEBUG = os.getenv("AGENTIC_QUALITY_DEBUG", "1").lower() not in {"0", "false", "no", "off"}


def _lookup_debug(event: str, payload: Dict[str, Any]) -> None:
    if not AGENTIC_LOOKUP_DEBUG:
        return
    if not AGENTIC_LOOKUP_VERBOSE:
        compact = dict(payload or {})
        if "sample_rows" in compact:
            compact["sample_count"] = len(compact.pop("sample_rows") or [])
        if "candidates" in compact:
            compact["candidates"] = [
                {
                    "name": c.get("name"),
                    "team": c.get("team"),
                    "league_name": c.get("league_name"),
                    "match_count": c.get("match_count"),
                    "stats_count": c.get("stats_count"),
                }
                for c in (compact.get("candidates") or [])[:3]
            ]
        if "top_scored_rows" in compact:
            compact["top_scored_rows"] = [
                {
                    "player_name": r.get("player_name"),
                    "team_name": r.get("team_name"),
                    "score": r.get("score"),
                }
                for r in (compact.get("top_scored_rows") or [])[:3]
            ]
        if "pattern_stages" in compact:
            compact["pattern_stages"] = [stage for stage, _ in compact.get("pattern_stages") or []]
        payload = compact
    try:
        body = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        body = str(payload)
    #print(f"[chatbot_agentic_lookup] event={event} {body}", flush=True)


def _quality_debug(event: str, payload: Dict[str, Any]) -> None:
    if not AGENTIC_QUALITY_DEBUG:
        return
    try:
        body = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        body = str(payload)
    #print(f"[chatbot_agentic_quality] event={event} {body}", flush=True)


ALLOWED_METRICS = {
    "Duels Won", "Clearances", "Chances Created", "Accurate Crosses", "Clearance Offline",
    "Ball Recovery", "Saves Insidebox", "Man Of Match", "Penalties Committed",
    "Dispossessed", "Fouls", "Goals Conceded", "Shots On Target", "Shots On Target (%)",
    "Accurate Passes", "Penalties Scored", "Tackles Won", "Aerials Won (%)",
    "Through Balls", "Offsides Provoked", "Penalties Missed", "Good High Claim",
    "Big Chances Created", "Penalties Won", "Dribbled Past", "Punches", "Yellow Cards",
    "Assists", "Blocked Shots", "Backward Passes", "Hit Woodwork", "Shots Total",
    "Shots Blocked", "Dribble Attempts", "Penalties Saved", "Long Balls Won (%)",
    "Long Balls Won", "Long Balls", "Tackles", "Aerials", "Offsides", "Possession Lost",
    "Successful Dribbles", "Goalkeeper Goals Conceded", "Total Crosses", "Total Duels",
    "Error Lead To Goal", "Saves", "Successful Crosses (%)", "Big Chances Missed",
    "Own Goals", "Key Passes", "Yellow & Red Cards", "Minutes Played",
    "Accurate Passes (%)", "Aerials Won", "Goals", "Touches", "Passes", "Duels Lost",
    "Last Man Tackle", "Shots Off Target", "Interceptions", "Turn Over",
    "Tackles Won (%)", "Aerials Lost", "Duels Won (%)", "Red Cards", "Captain",
    "Passes In Final Third", "Rating", "Fouls Drawn", "Error Lead To Shot",
    "Through Balls Won",
}

POSITIVE_METRICS = {
    "Duels Won", "Clearances", "Chances Created", "Accurate Crosses", "Ball Recovery",
    "Saves Insidebox", "Man Of Match", "Shots On Target", "Shots On Target (%)",
    "Accurate Passes", "Penalties Scored", "Tackles Won", "Aerials Won (%)",
    "Through Balls", "Good High Claim", "Big Chances Created", "Penalties Won",
    "Assists", "Blocked Shots", "Hit Woodwork", "Shots Total", "Dribble Attempts",
    "Penalties Saved", "Long Balls Won (%)", "Long Balls Won", "Long Balls", "Tackles",
    "Aerials", "Successful Dribbles", "Total Crosses", "Total Duels", "Saves",
    "Successful Crosses (%)", "Key Passes", "Minutes Played", "Accurate Passes (%)",
    "Aerials Won", "Goals", "Touches", "Passes", "Last Man Tackle", "Shots Off Target",
    "Interceptions", "Tackles Won (%)", "Duels Won (%)", "Passes In Final Third",
    "Rating", "Fouls Drawn", "Through Balls Won",
}

NEGATIVE_METRICS = {
    "Penalties Committed", "Dispossessed", "Fouls", "Goals Conceded",
    "Penalties Missed", "Dribbled Past", "Yellow Cards", "Shots Blocked",
    "Offsides", "Possession Lost", "Goalkeeper Goals Conceded", "Error Lead To Goal",
    "Big Chances Missed", "Own Goals", "Yellow & Red Cards", "Duels Lost",
    "Turn Over", "Aerials Lost", "Red Cards", "Error Lead To Shot",
}

ROLE_METRICS = {
    "attacker": {
        "Shots Total", "Shots On Target", "Shots On Target (%)", "Shots Off Target",
        "Big Chances Created", "Goals", "Assists", "Key Passes", "Chances Created",
        "Passes In Final Third", "Accurate Passes", "Accurate Passes (%)",
        "Total Crosses", "Accurate Crosses", "Successful Crosses (%)",
        "Dribble Attempts", "Successful Dribbles", "Hit Woodwork",
    },
    "midfielder": {
        "Passes", "Key Passes", "Chances Created", "Dribble Attempts",
        "Successful Dribbles", "Interceptions", "Tackles", "Tackles Won",
        "Tackles Won (%)", "Ball Recovery", "Duels Won", "Duels Won (%)",
        "Total Duels", "Blocked Shots", "Fouls Drawn", "Passes In Final Third",
    },
    "defender": {
        "Tackles", "Tackles Won", "Tackles Won (%)", "Interceptions", "Clearances",
        "Last Man Tackle", "Duels Won", "Duels Won (%)", "Total Duels", "Aerials",
        "Aerials Won", "Aerials Won (%)", "Blocked Shots", "Shots Blocked",
    },
    "goalkeeper": {
        "Saves", "Saves Insidebox", "Penalties Saved", "Punches", "Good High Claim",
        "Long Balls", "Long Balls Won", "Long Balls Won (%)", "Accurate Passes",
        "Accurate Passes (%)", "Touches",
    },
}


@dataclass
class AgenticContext:
    original_question: str
    translated_question: str
    effective_query: str
    lang: str
    history_rows: list
    seen_players: set[str]
    strategy: Optional[str] = None
    target_team: Optional[str] = None
    intent: str = "new_recommendation"
    direct_player_lookup: bool = False
    comparison_players: List[str] = field(default_factory=list)
    generic_alternative: bool = False
    recent_constraints: List[str] = field(default_factory=list)
    initial_strong_club_default: bool = False
    discovery_mode: bool = True
    allow_turkish: bool = False
    allow_non_senior: bool = False
    premium_only: bool = False
    quality_discovery_mode: bool = False


class StaticDocsRetriever(BaseRetriever):
    docs: List[Document]

    def _get_relevant_documents(self, query: str) -> List[Document]:
        return list(self.docs)

    async def _aget_relevant_documents(self, query: str) -> List[Document]:
        return list(self.docs)


def extract_json_object(text: str) -> Dict[str, Any]:
    if isinstance(text, dict):
        return text
    raw = (text or "").strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except Exception:
        return {}


def is_direct_player_lookup_request_agentic(original_question: Optional[str], translated_question: Optional[str]) -> bool:
    if is_direct_player_lookup_request(original_question):
        return True

    raw = (translated_question or original_question or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if any(ch.isdigit() for ch in raw):
        return False
    if any(sep in lowered for sep in ["?", ",", ".", "!", " for ", " to "]):
        return False
    blocked_patterns = [
        r"\b(suggest|recommend|find|need|looking|look|want|searching|give|show|another|different|new|other)\b",
        r"\b(player|footballer|signing|transfer|target|striker|winger|midfielder|defender|goalkeeper|forward)\b",
        r"\b(top class|elite|world class|very good|high budget|big budget|money)\b",
    ]
    if any(re.search(pattern, lowered) for pattern in blocked_patterns):
        return False

    folded = norm_name(raw)
    tokens = _lookup_tokens(folded)
    if len(tokens) == 1:
        return len(tokens[0]) >= 5
    return 2 <= len(tokens) <= 5 and all(len(token) >= 2 for token in tokens)


def is_narrow_filtered_suggestion_request(question: Optional[str], strategy: Optional[str] = None) -> bool:
    text = f"{question or ''}\n{strategy or ''}".lower()
    normalized = re.sub(r"\s+", " ", norm_name(text)).strip()
    if not normalized:
        return False
    if extract_target_team_from_question(question):
        return False
    narrow_patterns = [
        r"\b(age|aged|older|younger|between|under|over|min|max|below|above|\d+\+|u\d{1,2})\b",
        r"\b(veteran|experienced|old player|older player|teenager|wonderkid)\b",
        r"\b(reserve|academy|youth|b team|second team)\b",
    ]
    return any(re.search(pattern, normalized) for pattern in narrow_patterns)


def _stats_count_from_metadata(metadata: Dict[str, Any]) -> int:
    return len(extract_allowed_stats_from_metadata(metadata or {}))


def _quality_thresholds(ctx: Optional[AgenticContext]) -> Dict[str, int]:
    strong_target = bool(ctx and ctx.initial_strong_club_default)
    return {
        "min_age": 20,
        "max_age": 30 if strong_target else 32,
        "min_match_count": 15,
        "min_potential": 70 if strong_target else 65,
        "min_form": 70 if strong_target else 65,
    }


def _passes_quality_discovery_metadata(metadata: Dict[str, Any], ctx: Optional[AgenticContext] = None) -> bool:
    thresholds = _quality_thresholds(ctx)
    age = _num((metadata or {}).get("age"))
    match_count = _num((metadata or {}).get("match_count"))
    if age is None or int(round(age)) < thresholds["min_age"] or int(round(age)) > thresholds["max_age"]:
        return False
    if match_count is None or match_count < thresholds["min_match_count"]:
        return False
    return _stats_count_from_metadata(metadata or {}) > 0


def _is_transfer_fallback_club_strict(team_name: Optional[str]) -> bool:
    team_norm = norm_name(team_name or "")
    if not team_norm:
        return False
    return any(team_norm == norm_name(club_name) for club_name in TRANSFER_FALLBACK_CLUBS)


STRONG_TARGET_TRANSFER_TEAMS = {
    norm_name(name)
    for name in [
        "Real Madrid", "Manchester City", "Arsenal", "Paris Saint-Germain", "Paris Saint Germain",
        "PSG", "Barcelona", "FC Barcelona", "Liverpool", "Bayern Munich", "FC Bayern Munich",
        "Chelsea", "Manchester United", "Tottenham Hotspur", "Tottenham", "Spurs",
        "Newcastle United", "Newcastle", "Aston Villa", "Inter Milan", "Inter",
        "Internazionale", "AC Milan", "Milan", "Juventus", "Juve", "Juventus FC",
        "Atletico Madrid", "Atletico de Madrid", "Borussia Dortmund", "Dortmund", "BVB",
        "Bayer Leverkusen", "Leverkusen", "RB Leipzig", "Napoli", "Benfica", "SL Benfica",
        "Sporting CP", "Sporting", "Porto", "FC Porto", "Ajax", "AFC Ajax",
        "PSV Eindhoven", "PSV", "Feyenoord", "West Ham United", "West Ham",
        "Brighton & Hove Albion", "Brighton", "Everton", "Roma", "AS Roma", "Lazio",
        "Atalanta", "Marseille", "Olympique Marseille", "Olympique de Marseille",
        "Monaco", "AS Monaco", "Lille", "Lille OSC", "Lyon", "Olympique Lyonnais",
        "Sevilla", "Sevilla FC", "Real Sociedad", "Villarreal", "Villarreal CF",
        "Athletic Club", "Athletic Bilbao", "Real Betis", "Galatasaray", "Celtic",
        "Rangers", "Fenerbahce", "Fenerbahçe", "Besiktas", "Beşiktaş", "Club Brugge",
        "Trabzonspor",
    ]
}


def _is_strong_target_transfer_team(team_name: Optional[str]) -> bool:
    team_norm = norm_name(team_name or "")
    return bool(team_norm and team_norm in STRONG_TARGET_TRANSFER_TEAMS)


def _doc_identity_key(doc: Document) -> str:
    md = doc.metadata or {}
    name = md.get("player_name") or md.get("name") or ""
    team = md.get("team_name") or md.get("team") or md.get("club") or ""
    return f"{norm_name(str(name))}|{norm_name(str(team))}"


def _merge_docs(existing: List[Document], new_docs: List[Document], *, limit: int = 24) -> List[Document]:
    merged: List[Document] = []
    seen = set()
    for doc in [*(existing or []), *(new_docs or [])]:
        key = _doc_identity_key(doc) or (doc.page_content or "")[:80]
        if key in seen:
            continue
        seen.add(key)
        merged.append(doc)
        if len(merged) >= limit:
            break
    return merged


def _needs_more_quality_docs(docs: List[Document], ctx: AgenticContext, *, minimum: int = 8) -> bool:
    return bool(ctx.quality_discovery_mode and len(docs or []) < minimum)


def _transfer_target_query(ctx: AgenticContext, base_query: Optional[str] = None) -> str:
    query = (base_query or ctx.effective_query or "").strip()
    if not ctx.target_team:
        return query
    stripped = strip_target_team_from_question(query, ctx.target_team)
    if stripped == query:
        stripped = re.sub(re.escape(ctx.target_team), " ", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s+", " ", stripped).strip()
    stripped = stripped or "suggest a player"
    return (
        f"{stripped}\n"
        f"Find a realistic transfer target for {ctx.target_team}. "
        f"Do not retrieve players currently at {ctx.target_team} or any same-club variant. "
        + (
            "Prefer strong senior first-team players from strong clubs."
            if ctx.initial_strong_club_default
            else "Prefer players whose current level is realistic for this target club, not top-tier names beyond the club's level."
        )
    )


def build_agentic_context(
    *,
    original_question: str,
    translated_question: str,
    lang: str,
    history_rows: list,
    seen_players: set[str],
    strategy: Optional[str],
    planner_data: Optional[Dict[str, Any]] = None,
) -> AgenticContext:
    translated = rewrite_position_reference_phrases(translated_question)
    planner_data = planner_data or {}
    generic_alternative = is_generic_alternative_request(translated)
    planner_intent = planner_data.get("intent")
    heuristic_direct_lookup = is_direct_player_lookup_request_agentic(original_question, translated)
    direct_lookup = planner_intent == "direct_player_lookup" or (
        not planner_intent and heuristic_direct_lookup
    )

    target_team = extract_target_team_from_question(translated)
    if not target_team and generic_alternative:
        for row in reversed(history_rows):
            if row.get("role") != "human":
                continue
            target_team = extract_target_team_from_question(row.get("content") or "")
            if target_team:
                break

    recent_constraints: List[str] = []
    if generic_alternative:
        recent_constraints = collect_recent_human_constraints(
            history_rows,
            is_generic_alternative_fn=is_generic_alternative_request,
            limit=3,
        )

    effective_query = (planner_data.get("effective_query") or translated).strip()
    if generic_alternative and recent_constraints:
        constraints_block = "\n".join(f"- {msg}" for msg in recent_constraints)
        effective_query = (
            "Carry over the same scouting constraints from these recent user requests:\n"
            f"{constraints_block}\n"
            "Follow-up request: suggest another different player with the same criteria."
        )

    seen_lower = {(name or "").lower().strip() for name in seen_players}
    mentions_seen = any(
        name and (name in (original_question or "").lower() or name in translated.lower())
        for name in seen_lower
    )
    comparison_players = planner_data.get("comparison_players") or []
    if not isinstance(comparison_players, list):
        comparison_players = []
    comparison_players = [
        str(name).strip()
        for name in comparison_players
        if isinstance(name, str) and str(name).strip()
    ][:2]

    intent = (
        planner_intent if planner_intent else
        "direct_player_lookup" if direct_lookup else
        "alternative_recommendation" if generic_alternative else
        "seen_player_followup" if mentions_seen else
        "new_recommendation"
    )
    if len(comparison_players) >= 2:
        intent = "comparison"
    if intent != "direct_player_lookup":
        direct_lookup = False

    initial_high_quality_default = (
        not seen_players
        and not mentions_seen
        and not generic_alternative
        and is_weak_generic_suggestion_request(translated)
    )
    initial_fallback_club_target_default = (
        not seen_players
        and not mentions_seen
        and not generic_alternative
        and bool(target_team)
        and _is_strong_target_transfer_team(target_team)
    )

    discovery_mode = not mentions_seen and not direct_lookup
    quality_discovery_mode = (
        discovery_mode
        and intent in {"new_recommendation", "alternative_recommendation"}
        and not is_narrow_filtered_suggestion_request(translated, strategy)
        and not is_premium_request(translated)
    )
    return AgenticContext(
        original_question=original_question,
        translated_question=translated,
        effective_query=effective_query,
        lang=lang,
        history_rows=history_rows,
        seen_players=seen_players,
        strategy=strategy,
        target_team=target_team,
        intent=intent,
        direct_player_lookup=direct_lookup,
        comparison_players=comparison_players,
        generic_alternative=generic_alternative,
        recent_constraints=recent_constraints,
        initial_strong_club_default=initial_high_quality_default or initial_fallback_club_target_default,
        discovery_mode=discovery_mode,
        allow_turkish=request_allows_turkish_entities(translated),
        allow_non_senior=request_allows_non_senior_squads(translated),
        premium_only=is_premium_request(translated),
        quality_discovery_mode=quality_discovery_mode,
    )


def filter_candidate_docs(
    raw_docs: Iterable[Document],
    ctx: AgenticContext,
    active_query: Optional[str] = None,
    *,
    restrict_to_fallback_clubs: bool = False,
    require_complete_discovery_fields: bool = False,
    limit: int = 12,
    pass_label: str = "retriever",
) -> List[Document]:
    query = active_query or ctx.effective_query
    raw_docs_list = list(raw_docs or [])
    filtered_docs: List[Document] = []
    seen_doc_keys = set()
    seen_names_norm = {(name or "").strip().lower() for name in ctx.seen_players}
    rejection_counts: Counter[str] = Counter()

    for doc in raw_docs_list:
        md = doc.metadata or {}
        player_name = str(md.get("player_name") or md.get("name") or "").strip()
        team_name = str(md.get("team_name") or md.get("team") or md.get("club") or "").strip()
        nationality = str(md.get("nationality_name") or md.get("nationality") or md.get("country") or "").strip()
        position_name = str(md.get("position_name") or md.get("position") or "").strip()
        if player_name.lower() in seen_names_norm and ctx.intent in {"new_recommendation", "alternative_recommendation"}:
            rejection_counts["already_seen"] += 1
            continue
        rejection_reason = get_candidate_rejection_reason(
            player_name,
            team_name,
            nationality,
            target_team=ctx.target_team,
            allow_turkish=ctx.allow_turkish,
            allow_non_senior=ctx.allow_non_senior,
            premium_only=ctx.premium_only,
        )
        if rejection_reason:
            rejection_counts[rejection_reason] += 1
            continue
        if restrict_to_fallback_clubs and not _is_transfer_fallback_club_strict(team_name):
            rejection_counts["fallback_club_restriction"] += 1
            continue
        if require_complete_discovery_fields and not has_required_discovery_fields(team_name, position_name):
            rejection_counts["missing_discovery_fields"] += 1
            continue
        if ctx.quality_discovery_mode and not _passes_quality_discovery_metadata(md, ctx):
            rejection_counts["quality_metadata_floor"] += 1
            continue
        position_ok, _, _ = player_matches_requested_position(query, position_name, [position_name] if position_name else [])
        if not position_ok:
            rejection_counts["position_mismatch"] += 1
            continue
        doc_key = (player_name or doc.page_content[:80]).strip().lower()
        if doc_key in seen_doc_keys:
            rejection_counts["duplicate_name"] += 1
            continue
        seen_doc_keys.add(doc_key)
        filtered_docs.append(doc)

    if ctx.quality_discovery_mode:
        filtered_docs.sort(
            key=lambda doc: (
                _stats_count_from_metadata(doc.metadata or {}),
                _num((doc.metadata or {}).get("match_count")) or 0,
                _num((doc.metadata or {}).get("Rating")) or 0,
            ),
            reverse=True,
        )
    docs_out = filtered_docs[:limit]
    if ctx.quality_discovery_mode:
        _quality_debug("retriever_pass", {
            "pass": pass_label,
            "query": query,
            "raw_count": len(raw_docs_list),
            "accepted_count": len(filtered_docs),
            "returned_count": len(docs_out),
            "restrict_to_fallback_clubs": restrict_to_fallback_clubs,
            "target_team": ctx.target_team,
            "top_rejections": rejection_counts.most_common(5),
            "sample_accepted": [
                {
                    "name": (doc.metadata or {}).get("player_name") or (doc.metadata or {}).get("name"),
                    "team": (doc.metadata or {}).get("team_name") or (doc.metadata or {}).get("team"),
                    "league": (doc.metadata or {}).get("league_name") or (doc.metadata or {}).get("league"),
                    "age": (doc.metadata or {}).get("age"),
                    "match_count": (doc.metadata or {}).get("match_count"),
                    "stats_count": _stats_count_from_metadata(doc.metadata or {}),
                    "rating": (doc.metadata or {}).get("Rating"),
                }
                for doc in docs_out[:5]
            ],
        })
    return docs_out


def fetch_quality_suggestion_docs_from_db(ctx: AgenticContext, *, limit: int = 24) -> List[Document]:
    thresholds = _quality_thresholds(ctx)
    db = get_db()
    try:
        rows = db.execute(text("""
            SELECT id, metadata, content
            FROM player_data
            WHERE
                (metadata->>'age') IS NOT NULL
                AND (metadata->>'match_count') IS NOT NULL
                AND (metadata->>'position_name') IS NOT NULL
                AND ((metadata->>'age')::numeric BETWEEN :min_age AND :max_age)
                AND ((metadata->>'match_count')::numeric >= :min_match_count)
            ORDER BY COALESCE((metadata->>'Rating')::numeric, 0) DESC
            LIMIT :lim
        """), {
            "min_age": thresholds["min_age"],
            "max_age": thresholds["max_age"],
            "min_match_count": thresholds["min_match_count"],
            "lim": 800,
        }).mappings().all()
    finally:
        db.close()

    docs: List[Document] = []
    seen_doc_keys = set()
    seen_names_norm = {(name or "").strip().lower() for name in ctx.seen_players}
    rejection_counts: Counter[str] = Counter()
    for row in rows or []:
        md = dict(row.get("metadata") or {})
        md.setdefault("id", row.get("id"))
        player_name = str(md.get("player_name") or md.get("name") or "").strip()
        team_name = str(md.get("team_name") or md.get("team") or md.get("club") or "").strip()
        nationality = str(md.get("nationality_name") or md.get("nationality") or md.get("country") or "").strip()
        position_name = str(md.get("position_name") or md.get("position") or "").strip()
        if not player_name or player_name.lower() in seen_names_norm:
            rejection_counts["missing_name_or_seen"] += 1
            continue
        rejection_reason = get_candidate_rejection_reason(
            player_name,
            team_name,
            nationality,
            target_team=ctx.target_team,
            allow_turkish=ctx.allow_turkish,
            allow_non_senior=ctx.allow_non_senior,
            premium_only=ctx.premium_only,
        )
        if rejection_reason:
            rejection_counts[rejection_reason] += 1
            continue
        if ctx.initial_strong_club_default and not _is_transfer_fallback_club_strict(team_name):
            rejection_counts["fallback_club_restriction"] += 1
            continue
        if not has_required_discovery_fields(team_name, position_name):
            rejection_counts["missing_discovery_fields"] += 1
            continue
        if not _passes_quality_discovery_metadata(md, ctx):
            rejection_counts["quality_metadata_floor"] += 1
            continue
        position_ok, _, _ = player_matches_requested_position(
            ctx.effective_query,
            position_name,
            [position_name] if position_name else [],
        )
        if not position_ok:
            rejection_counts["position_mismatch"] += 1
            continue
        doc_key = player_name.lower()
        if doc_key in seen_doc_keys:
            rejection_counts["duplicate_name"] += 1
            continue
        seen_doc_keys.add(doc_key)
        docs.append(Document(page_content=row.get("content") or "", metadata=md))

    docs.sort(
        key=lambda doc: (
            _stats_count_from_metadata(doc.metadata or {}),
            _num((doc.metadata or {}).get("Rating")) or 0,
            _num((doc.metadata or {}).get("match_count")) or 0,
        ),
        reverse=True,
    )
    docs_out = docs[:limit]
    _quality_debug("db_quality_pass", {
        "raw_count": len(rows or []),
        "accepted_count": len(docs),
        "returned_count": len(docs_out),
        "target_team": ctx.target_team,
        "top_rejections": rejection_counts.most_common(5),
        "sample_accepted": [
            {
                "name": (doc.metadata or {}).get("player_name") or (doc.metadata or {}).get("name"),
                "team": (doc.metadata or {}).get("team_name") or (doc.metadata or {}).get("team"),
                "league": (doc.metadata or {}).get("league_name") or (doc.metadata or {}).get("league"),
                "age": (doc.metadata or {}).get("age"),
                "match_count": (doc.metadata or {}).get("match_count"),
                "stats_count": _stats_count_from_metadata(doc.metadata or {}),
                "rating": (doc.metadata or {}).get("Rating"),
            }
            for doc in docs_out[:5]
        ],
    })
    return docs_out


def build_filtered_retriever_agentic(
    ctx: AgenticContext,
    candidate_retriever: BaseRetriever,
    broad_candidate_retriever: BaseRetriever,
) -> Tuple[BaseRetriever, List[Document]]:
    initial_query = _transfer_target_query(ctx) if ctx.target_team and ctx.intent in {"new_recommendation", "alternative_recommendation"} else (ctx.effective_query or "")
    raw_docs = candidate_retriever.invoke(initial_query)
    docs = filter_candidate_docs(
        raw_docs,
        ctx,
        initial_query,
        restrict_to_fallback_clubs=ctx.initial_strong_club_default,
        require_complete_discovery_fields=ctx.discovery_mode,
        pass_label="candidate_retriever_initial",
    )
    alt_query = ctx.effective_query
    pass3_query = ctx.effective_query

    if (not docs or _needs_more_quality_docs(docs, ctx)) and ctx.target_team:
        alt_query = strip_target_team_from_question(ctx.effective_query, ctx.target_team)
        if alt_query != ctx.effective_query:
            pass2_query = _transfer_target_query(ctx, alt_query)
            pass2_docs = filter_candidate_docs(
                candidate_retriever.invoke(pass2_query),
                ctx,
                pass2_query,
                restrict_to_fallback_clubs=ctx.initial_strong_club_default,
                pass_label="candidate_retriever_target_stripped",
            )
            docs = _merge_docs(docs, pass2_docs)

        if not docs or _needs_more_quality_docs(docs, ctx):
            pass3_query = (
                f"{alt_query}\nSearch strong European first-team transfer targets for {ctx.target_team}; "
                "exclude the target club, youth/reserve squads, and Turkish entities unless explicitly requested."
            )
            pass3_docs = filter_candidate_docs(
                candidate_retriever.invoke(pass3_query),
                ctx,
                pass3_query,
                restrict_to_fallback_clubs=True,
                pass_label="candidate_retriever_strong_transfer",
            )
            docs = _merge_docs(docs, pass3_docs)

    if not docs or _needs_more_quality_docs(docs, ctx):
        broad_query = _transfer_target_query(ctx, pass3_query) if ctx.target_team else ctx.effective_query
        broad_docs = filter_candidate_docs(
            broad_candidate_retriever.invoke(broad_query),
            ctx,
            broad_query,
            restrict_to_fallback_clubs=ctx.initial_strong_club_default,
            require_complete_discovery_fields=ctx.discovery_mode,
            pass_label="broad_retriever",
        )
        docs = _merge_docs(docs, broad_docs)

    if (not docs or _needs_more_quality_docs(docs, ctx)) and ctx.quality_discovery_mode:
        db_docs = fetch_quality_suggestion_docs_from_db(ctx)
        docs = _merge_docs(docs, db_docs)

    return StaticDocsRetriever(docs=docs), docs


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def extract_allowed_stats_from_metadata(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    stats = []
    for metric in ALLOWED_METRICS:
        value = _num((metadata or {}).get(metric))
        if value is None:
            continue
        if abs(value) <= 0.05:
            continue
        stats.append({"metric": metric, "value": value})
    return stats


def doc_to_candidate(doc: Document, index: int) -> Dict[str, Any]:
    md = doc.metadata or {}
    stats = extract_allowed_stats_from_metadata(md)
    age = _num(md.get("age"))
    position = md.get("position_name") or md.get("position")
    return {
        "index": index,
        "id": md.get("id"),
        "name": md.get("player_name") or md.get("name"),
        "gender": md.get("gender"),
        "height": _num(md.get("height")),
        "weight": _num(md.get("weight")),
        "age": int(round(age)) if age is not None else None,
        "nationality": md.get("nationality_name") or md.get("nationality") or md.get("country"),
        "team": md.get("team_name") or md.get("team") or md.get("club"),
        "league_name": md.get("league_name") or md.get("league"),
        "position_name": position,
        "match_count": _num(md.get("match_count")),
        "rating": _num(md.get("Rating")),
        "potential": None,
        "form": None,
        "age_upside_score": None,
        "metrics_upside_score": None,
        "stats": stats,
        "summary": summarize_doc_candidate(doc),
        "content": doc.page_content,
    }


def _metadata_to_candidate(metadata: Dict[str, Any], index: int = 1, content: Optional[str] = None) -> Dict[str, Any]:
    doc = Document(page_content=content or "", metadata=metadata or {})
    return doc_to_candidate(doc, index)


def _lookup_tokens(name_norm: str) -> List[str]:
    tokens = [tok for tok in re.split(r"[^a-z0-9]+", name_norm or "") if len(tok) >= 3]
    return list(dict.fromkeys(tokens))


def _token_variants(token: str) -> List[str]:
    return [token]


def _token_fragments(token: str) -> List[str]:
    if len(token) < 5:
        return []
    fragments = {token[:5], token[-5:]}
    if len(token) >= 6:
        fragments.add(token[:6])
        fragments.add(token[-6:])
    if len(token) >= 8:
        for n in (5, 6):
            for start in range(0, len(token) - n + 1):
                fragment = token[start:start + n]
                if any(ch in "aeiou" for ch in fragment):
                    fragments.add(fragment)
    return sorted(frag for frag in fragments if len(frag) >= 5)


def _direct_lookup_pattern_stages(name_norm: str) -> List[Tuple[str, List[str]]]:
    tokens = _lookup_tokens(name_norm)
    if not tokens:
        return []

    stages: List[Tuple[str, List[str]]] = []
    if len(tokens) >= 2:
        last = tokens[-1]
        stages.append(("last_token", [f"%{last}%"]))

    long_tokens = [token for token in tokens if len(token) >= 5]
    if long_tokens:
        stages.append(("long_tokens", [f"%{token}%" for token in long_tokens]))

    distinctive = sorted(long_tokens or tokens, key=len, reverse=True)[:2]
    fragment_patterns = sorted({
        f"%{fragment}%"
        for token in distinctive
        for fragment in _token_fragments(token)
        if len(fragment) >= 5
    })
    if fragment_patterns:
        stages.append(("distinctive_fragments", fragment_patterns))

    deduped: List[Tuple[str, List[str]]] = []
    seen_patterns: set[Tuple[str, ...]] = set()
    for stage, patterns in stages:
        unique_patterns = sorted(set(patterns))
        key = tuple(unique_patterns)
        if unique_patterns and key not in seen_patterns:
            deduped.append((stage, unique_patterns))
            seen_patterns.add(key)
    return deduped


def _direct_lookup_score(meta: Dict[str, Any], player_identity: Dict[str, Any]) -> float:
    base = _score_candidate(meta, player_identity)
    query_norm = norm_name(player_identity.get("name") or "")
    player_name = str(meta.get("player_name") or meta.get("name") or "").strip()
    player_norm = meta.get("player_name_norm") or norm_name(player_name)
    if not query_norm or not player_norm:
        return base

    query_tokens = _lookup_tokens(query_norm)
    player_tokens = _lookup_tokens(player_norm)
    similarity = SequenceMatcher(None, query_norm, player_norm).ratio()
    score = base + (similarity * 8.0)

    if query_norm == player_norm:
        score += 20
    elif query_norm in player_norm or player_norm in query_norm:
        score += 12

    for qtok in query_tokens:
        q_variants = _token_variants(qtok)
        if any(variant in player_tokens for variant in q_variants):
            score += 4
        elif any(
            SequenceMatcher(None, variant, ptok).ratio() >= 0.78
            for variant in q_variants
            for ptok in player_tokens
        ):
            score += 2.5

    if query_tokens and player_tokens:
        query_last = query_tokens[-1]
        player_last = player_tokens[-1]
        if query_last == player_last:
            score += 8
        elif SequenceMatcher(None, query_last, player_last).ratio() >= 0.82:
            score += 5

    return score


def fetch_direct_player_candidate_by_name(name: str) -> Optional[Dict[str, Any]]:
    """
    Resolve direct player-name lookups with the same broad DB search and identity
    scoring pattern used by tools_extensions.fetch_player_nonzero_stats().
    """
    clean_name = (name or "").strip()
    if not clean_name:
        _lookup_debug("direct_lookup_skip_empty_name", {"input_name": name})
        return None

    name_norm = norm_name(clean_name)
    name_raw_q = f"%{clean_name}%"
    name_norm_q = f"%{name_norm}%"
    player_identity = {"name": clean_name}
    tokens = _lookup_tokens(name_norm)
    token_variants = sorted({variant for token in tokens for variant in _token_variants(token)})
    pattern_stages = _direct_lookup_pattern_stages(name_norm)
    _lookup_debug("direct_lookup_start", {
        "input_name": name,
        "clean_name": clean_name,
        "name_norm": name_norm,
        "name_raw_q": name_raw_q,
        "name_norm_q": name_norm_q,
        "player_identity": player_identity,
        "tokens": tokens,
        "token_variants": token_variants,
        "pattern_stages": pattern_stages,
    })

    db = get_db()
    try:
        _lookup_debug("direct_lookup_sql_before", {
            "table": "player_data",
            "where": [
                "metadata->>'player_name_norm' ILIKE :name_norm_q",
                "metadata->>'player_name' ILIKE :name_raw_q",
                "content ILIKE :name_raw_q",
            ],
            "params": {
                "name_norm_q": name_norm_q,
                "name_raw_q": name_raw_q,
                "lim": 250,
            },
        })
        rows = db.execute(text("""
            SELECT id, metadata, content
            FROM player_data
            WHERE
            (
                (metadata->>'player_name_norm') ILIKE :name_norm_q
                OR (metadata->>'player_name') ILIKE :name_raw_q
                OR (content ILIKE :name_raw_q)
            )
            ORDER BY id DESC
            LIMIT :lim
        """), {
            "name_norm_q": name_norm_q,
            "name_raw_q": name_raw_q,
            "lim": 250,
        }).mappings().all()
        _lookup_debug("direct_lookup_sql_after", {
            "stage": "full_name",
            "row_count": len(rows or []),
            "sample_rows": [
                {
                    "id": row.get("id"),
                    "player_name": (row.get("metadata") or {}).get("player_name"),
                    "player_name_norm": (row.get("metadata") or {}).get("player_name_norm"),
                    "team_name": (row.get("metadata") or {}).get("team_name"),
                    "nationality_name": (row.get("metadata") or {}).get("nationality_name"),
                    "position_name": (row.get("metadata") or {}).get("position_name"),
                    "match_count": (row.get("metadata") or {}).get("match_count"),
                }
                for row in list(rows or [])[:10]
            ],
        })

        for stage_name, patterns in pattern_stages:
            if rows:
                break
            _lookup_debug("direct_lookup_fuzzy_sql_before", {
                "stage": stage_name,
                "table": "player_data",
                "where": [
                    "metadata->>'player_name_norm' ILIKE ANY(:patterns)",
                    "metadata->>'player_name' ILIKE ANY(:patterns)",
                    "content ILIKE ANY(:patterns)",
                ],
                "params": {
                    "patterns": patterns,
                    "lim": 250,
                },
            })
            rows = db.execute(text("""
                SELECT id, metadata, content
                FROM player_data
                WHERE
                (
                    (metadata->>'player_name_norm') ILIKE ANY(:patterns)
                    OR (metadata->>'player_name') ILIKE ANY(:patterns)
                    OR (content ILIKE ANY(:patterns))
                )
                ORDER BY id DESC
                LIMIT :lim
            """), {
                "patterns": patterns,
                "lim": 250,
            }).mappings().all()
            _lookup_debug("direct_lookup_sql_after", {
                "stage": stage_name,
                "row_count": len(rows or []),
                "sample_rows": [
                    {
                        "id": row.get("id"),
                        "player_name": (row.get("metadata") or {}).get("player_name"),
                        "player_name_norm": (row.get("metadata") or {}).get("player_name_norm"),
                        "team_name": (row.get("metadata") or {}).get("team_name"),
                        "nationality_name": (row.get("metadata") or {}).get("nationality_name"),
                        "position_name": (row.get("metadata") or {}).get("position_name"),
                        "match_count": (row.get("metadata") or {}).get("match_count"),
                    }
                    for row in list(rows or [])[:10]
                ],
            })

        if not rows:
            _lookup_debug("direct_lookup_no_rows", {
                "clean_name": clean_name,
                "name_norm": name_norm,
                "pattern_stages": pattern_stages,
            })
            return None

        best: Tuple[float, Optional[int]] = (-1.0, None)
        scored_rows: List[Dict[str, Any]] = []
        for row in rows:
            meta = row.get("metadata") or {}
            score = _direct_lookup_score(meta, player_identity)
            row_id = row.get("id")
            scored_rows.append({
                "id": row_id,
                "score": score,
                "player_name": meta.get("player_name"),
                "player_name_norm": meta.get("player_name_norm"),
                "team_name": meta.get("team_name"),
                "nationality_name": meta.get("nationality_name"),
                "position_name": meta.get("position_name"),
                "match_count": meta.get("match_count"),
            })
            if row_id is not None and score > best[0]:
                best = (score, int(row_id))
        _lookup_debug("direct_lookup_scored_rows", {
            "best": {"score": best[0], "id": best[1]},
            "top_scored_rows": sorted(
                scored_rows,
                key=lambda item: (item.get("score") or 0, item.get("match_count") or 0, item.get("id") or 0),
                reverse=True,
            )[:15],
        })

        best_id = best[1]
        if best_id is None:
            _lookup_debug("direct_lookup_no_best_id", {"best": best})
            return None

        _lookup_debug("direct_lookup_fetch_best_before", {"best_id": best_id})
        doc = db.execute(text("""
            SELECT id, metadata, content
            FROM player_data
            WHERE id = :id
            LIMIT 1
        """), {"id": best_id}).mappings().first()

        if not doc:
            _lookup_debug("direct_lookup_best_missing", {"best_id": best_id})
            return None

        meta = dict(doc.get("metadata") or {})
        meta.setdefault("id", doc.get("id"))
        candidate = _metadata_to_candidate(meta, index=1, content=doc.get("content") or "")
        _lookup_debug("direct_lookup_result", {
            "candidate": {
                "index": candidate.get("index"),
                "name": candidate.get("name"),
                "team": candidate.get("team"),
                "nationality": candidate.get("nationality"),
                "position_name": candidate.get("position_name"),
                "match_count": candidate.get("match_count"),
                "rating": candidate.get("rating"),
                "stats_count": len(candidate.get("stats") or []),
            }
        })
        return candidate
    finally:
        db.close()


def fetch_direct_player_candidates_by_name(name: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Build a broad identity candidate pool for direct player lookups. This lets an
    identity resolver agent do the same kind of intent correction the old RAG
    answer step provided before DB enrichment.
    """
    clean_name = (name or "").strip()
    if not clean_name:
        _lookup_debug("direct_candidates_skip_empty_name", {"input_name": name})
        return []

    name_norm = norm_name(clean_name)
    name_raw_q = f"%{clean_name}%"
    name_norm_q = f"%{name_norm}%"
    tokens = _lookup_tokens(name_norm)
    pattern_stages = _direct_lookup_pattern_stages(name_norm)

    _lookup_debug("direct_candidates_start", {
        "input_name": name,
        "clean_name": clean_name,
        "name_norm": name_norm,
        "name_raw_q": name_raw_q,
        "name_norm_q": name_norm_q,
        "pattern_stages": pattern_stages,
    })

    db = get_db()
    try:
        rows = db.execute(text("""
            SELECT id, metadata, content
            FROM player_data
            WHERE
            (
                (metadata->>'player_name_norm') ILIKE :name_norm_q
                OR (metadata->>'player_name') ILIKE :name_raw_q
                OR (content ILIKE :name_raw_q)
            )
            ORDER BY id DESC
            LIMIT :lim
        """), {
            "name_norm_q": name_norm_q,
            "name_raw_q": name_raw_q,
            "lim": 250,
        }).mappings().all()

        for stage_name, patterns in pattern_stages:
            if rows:
                break
            _lookup_debug("direct_candidates_fuzzy_sql_before", {
                "stage": stage_name,
                "patterns": patterns,
                "lim": 500,
            })
            rows = db.execute(text("""
                    SELECT id, metadata, content
                    FROM player_data
                    WHERE
                    (
                        (metadata->>'player_name_norm') ILIKE ANY(:patterns)
                        OR (metadata->>'player_name') ILIKE ANY(:patterns)
                        OR (content ILIKE ANY(:patterns))
                    )
                    ORDER BY id DESC
                    LIMIT :lim
                """), {
                    "patterns": patterns,
                    "lim": 500,
                }).mappings().all()
            _lookup_debug("direct_candidates_fuzzy_sql_after", {
                "stage": stage_name,
                "row_count": len(rows or []),
                "sample_rows": [
                    {
                        "id": row.get("id"),
                        "player_name": (row.get("metadata") or {}).get("player_name"),
                        "player_name_norm": (row.get("metadata") or {}).get("player_name_norm"),
                        "team_name": (row.get("metadata") or {}).get("team_name"),
                        "league_name": (row.get("metadata") or {}).get("league_name"),
                        "match_count": (row.get("metadata") or {}).get("match_count"),
                    }
                    for row in list(rows or [])[:10]
                ],
            })
    finally:
        db.close()

    player_identity = {"name": clean_name}
    scored_rows = []
    for row in rows or []:
        meta = row.get("metadata") or {}
        scored_rows.append((_direct_lookup_score(meta, player_identity), row))

    selected_rows = [row for _, row in sorted(scored_rows, key=lambda item: item[0], reverse=True)[:limit]]
    candidates: List[Dict[str, Any]] = []
    for idx, row in enumerate(selected_rows, start=1):
        meta = dict(row.get("metadata") or {})
        meta.setdefault("id", row.get("id"))
        candidates.append(_metadata_to_candidate(meta, index=idx, content=row.get("content") or ""))

    _lookup_debug("direct_candidates_result", {
        "candidate_count": len(candidates),
        "candidates": [
            {
                "index": c.get("index"),
                "name": c.get("name"),
                "team": c.get("team"),
                "league_name": c.get("league_name"),
                "nationality": c.get("nationality"),
                "position_name": c.get("position_name"),
                "match_count": c.get("match_count"),
                "stats_count": len(c.get("stats") or []),
            }
            for c in candidates[:15]
        ],
    })
    return candidates


def format_candidates_for_selector(candidates: List[Dict[str, Any]], *, max_stats: int = 14) -> str:
    blocks = []
    for c in candidates:
        stats = sorted(c.get("stats") or [], key=lambda s: s.get("metric") or "")[:max_stats]
        compact = {
            "index": c.get("index"),
            "name": c.get("name"),
            "age": c.get("age"),
            "team": c.get("team"),
            "league_name": c.get("league_name"),
            "nationality": c.get("nationality"),
            "position_name": c.get("position_name"),
            "match_count": c.get("match_count"),
            "rating": c.get("rating"),
            "stats": stats,
        }
        blocks.append(json.dumps(compact, ensure_ascii=False))
    return "\n".join(blocks)


def validate_candidate(candidate: Dict[str, Any], ctx: AgenticContext) -> Optional[str]:
    if not candidate.get("name"):
        return "missing player name"
    if not ctx.direct_player_lookup and (candidate.get("potential") is None or candidate.get("form") is None):
        return "missing AI scoring"
    reason = get_candidate_rejection_reason(
        candidate.get("name"),
        candidate.get("team"),
        candidate.get("nationality"),
        target_team=ctx.target_team,
        allow_turkish=ctx.allow_turkish,
        allow_non_senior=ctx.allow_non_senior,
        premium_only=ctx.premium_only,
    )
    if reason and not ctx.direct_player_lookup:
        return reason
    if ctx.target_team and is_same_club(ctx.target_team, candidate.get("team")) and not ctx.direct_player_lookup:
        return "same target club"
    if (
        ctx.target_team
        and ctx.quality_discovery_mode
        and not ctx.initial_strong_club_default
        and _is_transfer_fallback_club_strict(candidate.get("team"))
        and not ctx.direct_player_lookup
    ):
        return "unrealistic strong source club for target level"
    if ctx.premium_only and not ctx.direct_player_lookup:
        if not is_premium_allowed_club(candidate.get("team")):
            return "premium club restriction"
        if (candidate.get("age") is not None) and int(candidate["age"]) > 30:
            return "premium age restriction"
        if (candidate.get("rating") is not None) and float(candidate["rating"]) < 7.25:
            return "premium rating restriction"
        if (candidate.get("potential") is not None) and int(candidate["potential"]) <= 88:
            return "premium potential restriction"
    if ctx.discovery_mode and not has_required_discovery_fields(candidate.get("team"), candidate.get("position_name")):
        return "missing discovery fields"
    if ctx.quality_discovery_mode:
        thresholds = _quality_thresholds(ctx)
        stats_count = len(candidate.get("stats") or [])
        age = candidate.get("age")
        match_count = candidate.get("match_count")
        potential = candidate.get("potential")
        form = candidate.get("form")
        if stats_count < 1:
            return "broad suggestion requires available stats"
        if age is None or int(age) < thresholds["min_age"] or int(age) > thresholds["max_age"]:
            return "broad suggestion age band restriction"
        if match_count is None or float(match_count) < thresholds["min_match_count"]:
            return "broad suggestion match-count restriction"
        if potential is None or int(potential) <= thresholds["min_potential"]:
            return "broad suggestion potential restriction"
        if form is None or int(form) <= thresholds["min_form"]:
            return "broad suggestion form restriction"
    pos_ok, _, _ = player_matches_requested_position(
        ctx.effective_query,
        candidate.get("position_name"),
        [candidate.get("position_name")] if candidate.get("position_name") else [],
    )
    if not pos_ok and not ctx.direct_player_lookup:
        return "position mismatch"
    if ctx.initial_strong_club_default and not _is_transfer_fallback_club_strict(candidate.get("team")):
        return "initial strong-club restriction"
    if candidate.get("name", "").strip().lower() in {(n or "").strip().lower() for n in ctx.seen_players}:
        if ctx.intent in {"new_recommendation", "alternative_recommendation"}:
            return "already seen"
    return None


def candidate_to_meta(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "players": [{
            "name": candidate.get("name"),
            "gender": candidate.get("gender"),
            "height": candidate.get("height"),
            "weight": candidate.get("weight"),
            "age": candidate.get("age"),
            "nationality": candidate.get("nationality"),
            "team": candidate.get("team"),
            "league": candidate.get("league_name"),
            "league_name": candidate.get("league_name"),
            "match_count": candidate.get("match_count"),
            "roles": [candidate.get("position_name")] if candidate.get("position_name") else [],
            "potential": candidate.get("potential"),
            "form": candidate.get("form"),
        }]
    }


def build_payload_from_candidate(candidate: Dict[str, Any], seen_players: set[str]) -> Tuple[Dict[str, Any], set[str]]:
    meta = candidate_to_meta(candidate)
    meta_new, new_names = filter_players_by_seen(meta, seen_players)
    payload = build_player_payload_new(meta_new) if new_names else {"players": []}
    if payload.get("players"):
        payload_meta = payload["players"][0].setdefault("meta", {})
        payload_meta["potential"] = candidate.get("potential")
        payload_meta["form"] = candidate.get("form")
        if candidate.get("league_name"):
            payload_meta.setdefault("league", candidate.get("league_name"))
            payload_meta.setdefault("league_name", candidate.get("league_name"))
        if not payload["players"][0].get("stats"):
            payload["players"][0]["stats"] = candidate.get("stats") or []
    return payload, new_names


def apply_ai_scores_to_candidate(candidate: Dict[str, Any], scoring_data: Dict[str, Any]) -> Dict[str, Any]:
    updated = dict(candidate)
    for key in ("age_upside_score", "metrics_upside_score", "potential", "form"):
        value = scoring_data.get(key)
        if value is None:
            continue
        try:
            updated[key] = int(round(float(value)))
        except Exception:
            continue
    if updated.get("potential") is not None:
        updated["potential"] = max(30, min(100, int(updated["potential"])))
    if updated.get("form") is not None:
        updated["form"] = max(0, min(100, int(updated["form"])))
    return updated


def is_greeting_or_offtopic(text: Optional[str]) -> bool:
    normalized = re.sub(r"[^\w\s]", "", (text or "").lower()).strip()
    if not normalized:
        return True
    return normalized in {
        "hi", "hey", "hello", "whats up", "what is up", "selam", "merhaba", "sa",
        "good morning", "good evening", "good afternoon",
    }


def short_offtopic_response(lang: str) -> str:
    if is_turkish(lang):
        return "Hangi pozisyon, takım veya oyuncu profili için scout önerisi istediğini yaz."
    return "Tell me the position, team, or player profile you want to scout."
