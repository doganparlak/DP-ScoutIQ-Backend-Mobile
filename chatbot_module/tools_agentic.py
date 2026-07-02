import json
import os
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Tuple

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from sqlalchemy import text

from api_module.utilities import ROLE_SHORT_TO_LONG, get_db
from chatbot_module.tools import (
    collect_recent_human_constraints,
    extract_target_team_from_question,
    filter_players_by_seen,
    get_candidate_rejection_reason,
    is_disallowed_turkish_club,
    is_direct_player_lookup_request,
    is_generic_alternative_request,
    is_non_senior_team,
    is_premium_request,
    is_same_club,
    is_turkish,
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
from chatbot_module.metrics import ALLOWED_METRICS, POSITIVE_METRICS


SQL_FOLD_FROM = "ÁÀÂÃÄÅĀĂĄáàâãäåāăąÉÈÊËĒĖĘĚéèêëēėęěÍÌÎÏĪİıíìîïīÓÒÔÕÖØŌóòôõöøōÚÙÛÜŪúùûüūÇĆČçćčÑñĞğŞŠşšÝŸýÿŽŹŻžźżÐð"
SQL_FOLD_TO = "AAAAAAAAAaaaaaaaaaEEEEEEEEeeeeeeeeIIIIIIiiiiiiOOOOOOOoooooooUUUUUuuuuuCCCcccNnGgSSssYYyyZZZzzzDd"


AGENTIC_LOOKUP_DEBUG = os.getenv("AGENTIC_LOOKUP_DEBUG", "1").lower() not in {"0", "false", "no", "off"}
AGENTIC_LOOKUP_VERBOSE = os.getenv("AGENTIC_LOOKUP_VERBOSE", "0").lower() in {"1", "true", "yes", "on"}
AGENTIC_QUALITY_DEBUG = os.getenv("AGENTIC_QUALITY_DEBUG", "0").lower() not in {"0", "false", "no", "off"}
SELECTOR_CANDIDATE_LIMIT = 24


def _lookup_debug(event: str, payload: Dict[str, Any]) -> None:
    if not AGENTIC_LOOKUP_DEBUG:
        return
    concise_events = {
        "direct_lookup_start",
        "direct_lookup_sql_before",
        "direct_lookup_sql_after",
        "direct_lookup_result",
        "direct_candidates_start",
        "direct_candidates_fuzzy_sql_before",
        "direct_candidates_fuzzy_sql_after",
        "direct_candidates_result",
    }
    if not AGENTIC_LOOKUP_VERBOSE and event not in concise_events:
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
    print(f"[chatbot_db_search] event={event} {body}", flush=True)


def _quality_debug(event: str, payload: Dict[str, Any]) -> None:
    if not AGENTIC_QUALITY_DEBUG:
        return
    try:
        body = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        body = str(payload)
    print(f"[chatbot_quality] event={event} {body}", flush=True)


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

ALLOWED_SELECTION_LEAGUES = [
    "Championship",
    "Eerste Divisie",
    "La Liga",
    "Stars League",
    "Primera Division",
    "Admiral Bundesliga",
    "Bundesliga",
    "2. Bundesliga",
    "Premier League",
    "1. Lig",
    "La Liga 2",
    "Liga Profesional de Fútbol",
    "Serie B",
    "First Division",
    "Major League Soccer",
    "Chance Liga",
    "Veikkausliiga",
    "FNL",
    "Ekstraklasa",
    "Eliteserien",
    "Premiership",
    "First League",
    "Eredivisie",
    "Allsvenskan",
    "Enterprise National League",
    "Liga Portugal",
    "Challenger Pro League",
    "Superliga",
    "Botola Pro",
    "Super League",
    "Liga MX",
    "League One",
    "Ligue 2",
    "League Two",
    "1. HNL",
    "Serie A",
    "Super Lig",
    "Ligue 1",
]
ALLOWED_SELECTION_LEAGUE_KEYS = {norm_name(league) for league in ALLOWED_SELECTION_LEAGUES}
CANONICAL_LEAGUES = {
    "Championship", "Eerste Divisie", "UAE Pro League", "La Liga", "V-League", "Stars League",
    "Primera Division", "Admiral Bundesliga", "Bundesliga", "2. Bundesliga", "Premier League",
    "1. Lig", "A-League Women", "La Liga 2", "Liga Profesional de Fútbol", "Indian Super League",
    "Serie B", "First Division", "Major League Soccer", "Chance Liga", "Veikkausliiga",
    "Women's Super League", "FNL", "Ekstraklasa", "Eliteserien", "Premiership", "First League",
    "Ligat ha'Al", "Eredivisie", "Allsvenskan", "Brasileiro Women", "Enterprise National League",
    "Liga Portugal", "Challenger Pro League", "Persian Gulf Pro League", "Superliga", "Botola Pro",
    "Super League", "Liga MX", "League One", "A-League Men", "K League 1", "Pro League",
    "Ligue 2", "League Two", "1. HNL", "Serie A", "Super Lig", "Ligue 1",
}
LEAGUE_BY_KEY = {norm_name(league): league for league in CANONICAL_LEAGUES}
LEAGUE_COMPACT_BY_KEY = {
    re.sub(r"[^a-z0-9]+", "", norm_name(league)): league
    for league in CANONICAL_LEAGUES
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
    allow_all_selection_leagues: bool = False
    retrieval_debug: List[Dict[str, Any]] = field(default_factory=list)
    constraints: Dict[str, Any] = field(default_factory=dict)
    constraint_relaxation_level: int = 0


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
    if extract_named_player_lookup_query(original_question, translated_question):
        return True
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


def _looks_like_player_name_fragment(value: Optional[str], *, allow_single: bool = False) -> Optional[str]:
    text = re.sub(r"\s+", " ", (value or "").strip(" .?!,:;\"'’“”")).strip()
    if not text:
        return None
    lowered = norm_name(text)
    blocked = {
        "bana", "bir", "oyuncu", "futbolcu", "player", "footballer", "profile", "scouting",
        "report", "analysis", "analyze", "analyse", "tell", "about", "what", "kind", "how",
        "is", "the", "a", "an", "young", "powerful", "explosive", "striker", "forward",
    }
    tokens = _lookup_tokens(lowered)
    tokens = [token for token in tokens if token not in blocked]
    if len(tokens) > 5:
        return None
    if len(tokens) < 2 and not (allow_single and len(tokens) == 1 and len(tokens[0]) >= 5):
        return None
    if any(len(token) < 2 for token in tokens):
        return None
    if any(token in {"player", "oyuncu", "futbolcu", "striker", "forward", "midfielder", "defender"} for token in tokens):
        return None
    original_tokens = [
        token
        for token in re.split(r"\s+", text)
        if norm_name(token) in set(tokens)
    ]
    return " ".join(original_tokens[: len(tokens)]).strip() or " ".join(tokens).strip()


def extract_named_player_lookup_query(
    original_question: Optional[str],
    translated_question: Optional[str] = None,
    effective_query: Optional[str] = None,
) -> Optional[str]:
    candidates = [original_question or "", effective_query or "", translated_question or ""]
    patterns = [
        r"^\s*(?P<name>.+?)\s+(?:nasil|nasıl)\s+bir\s+(?:oyuncu|futbolcu)\b",
        r"^\s*(?P<name>.+?)\s+(?:nasildir|nasıldır|kimdir)\b",
        r"^\s*(?P<name>.+?)\s+(?:player\s+profile|scouting\s+report|profile|analysis|analizi)\b",
        r"^\s*(?:tell\s+me\s+about|analy[sz]e|profile)\s+(?P<name>.+?)\s*$",
        r"^\s*(?:what\s+kind\s+of\s+player\s+is|who\s+is|how\s+is)\s+(?P<name>.+?)\s*$",
        r"^\s*(?:how\s+would)\s+(?P<name>.+?)\s+(?:fit|be)\s+(?:for|at|in|to)\s+.+$",
        r"^\s*(?P<name>[A-Za-zÀ-ÿ.'’\- ]{3,50}?)\s+\S+(?:'?[aeıiuü]|ye|ya|de|da|te|ta|e|a)?\s+(?:nasil|nasıl)\s+olur\b",
    ]
    for text in candidates:
        compact = re.sub(r"\s+", " ", text or "").strip()
        if not compact:
            continue
        for pattern in patterns:
            match = re.search(pattern, compact, flags=re.IGNORECASE)
            if not match:
                continue
            name = _looks_like_player_name_fragment(match.group("name"), allow_single=True)
            if name:
                return name

    # Controller often returns "Name player profile scouting report"; recover the leading identity.
    compact_effective = re.sub(r"\s+", " ", effective_query or "").strip()
    match = re.search(r"^(?P<name>.+?)\s+(?:player\s+profile|scouting\s+report|profile|analysis)\b", compact_effective, flags=re.IGNORECASE)
    if match:
        return _looks_like_player_name_fragment(match.group("name"))
    return None


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


def _mentions_club_as_source_team(question: Optional[str], team_name: Optional[str]) -> bool:
    text = re.sub(r"\s+", " ", norm_name(question or "")).strip()
    team_tokens = [re.escape(token) for token in norm_name(team_name or "").strip().split() if token]
    team = r"\s+".join(team_tokens)
    if not text or not team:
        return False
    source_patterns = [
        rf"\b(?:play|plays|playing|played|currently plays|currently playing)\s+(?:for|at|in)\s+{team}\b",
        rf"\b(?:from|at|in)\s+{team}\b",
        rf"\b{team}\s+(?:player|footballer|striker|forward|winger|midfielder|defender|goalkeeper)\b",
        rf"\b{team}\s*(?:de|da|te|ta|den|dan|ten|tan)\s+(?:oynayan|forma giyen|top oynayan)\b",
        rf"\b{team}\s+(?:oyuncusu|futbolcusu|formasi giyen|forma giyen)\b",
    ]
    return any(re.search(pattern, text) for pattern in source_patterns)


def _has_search_constraints(constraints: Dict[str, Any]) -> bool:
    cleaned = clean_constraints(constraints)
    scalar_keys = (
        "position", "nationality", "league", "team",
        "age_min", "age_max", "height_min", "height_max", "weight_min", "weight_max",
    )
    return any(cleaned.get(key) is not None for key in scalar_keys) or bool(
        cleaned.get("preferred_stats")
        or cleaned.get("stat_requirements")
        or cleaned.get("excluded_nationalities")
        or cleaned.get("excluded_positions")
        or cleaned.get("excluded_leagues")
        or cleaned.get("excluded_teams")
    )


def _looks_like_generic_player_search(question: Optional[str]) -> bool:
    normalized = re.sub(r"\s+", " ", norm_name(question or "")).strip()
    if not normalized:
        return False
    return bool(re.search(
        r"\b(player|footballer|oyuncu|futbolcu|striker|forward|forvet|winger|midfielder|defender|goalkeeper)\b",
        normalized,
    ))


def is_constraint_modification_request(question: Optional[str]) -> bool:
    normalized = re.sub(r"\s+", " ", norm_name(question or "")).strip()
    if not normalized:
        return False
    return bool(re.search(
        r"\b(also|with|having|add|include|instead of|rather than|without|remove|no longer|not anymore|any nationality|any league|any team|any age|any height|any weight)\b",
        normalized,
    ))


def _stats_count_from_metadata(metadata: Dict[str, Any]) -> int:
    return len(extract_allowed_stats_from_metadata(metadata or {}))


MIN_SELECTION_STATS = 3


def _league_from_metadata(metadata: Dict[str, Any]) -> str:
    return str((metadata or {}).get("league_name") or (metadata or {}).get("league") or "").strip()


def _is_allowed_selection_league(league_name: Optional[str]) -> bool:
    return norm_name(league_name or "") in ALLOWED_SELECTION_LEAGUE_KEYS


def _is_requested_constraint_league(league_name: Optional[str], ctx: Optional[AgenticContext]) -> bool:
    if not ctx:
        return False
    requested = canonical_league((ctx.constraints or {}).get("league"))
    return bool(requested and norm_name(league_name or "") == norm_name(requested))


def _is_selectable_league(league_name: Optional[str], ctx: Optional[AgenticContext]) -> bool:
    return _is_allowed_selection_league(league_name) or _is_requested_constraint_league(league_name, ctx)


def _has_explicit_league_constraint(ctx: Optional[AgenticContext]) -> bool:
    return bool(clean_constraints(getattr(ctx, "constraints", {}) or {}).get("league"))


def _missing_discovery_rejection(team_name: Optional[str], position_name: Optional[str], ctx: Optional[AgenticContext]) -> Optional[str]:
    level = int(getattr(ctx, "constraint_relaxation_level", 0) or 0)
    if not (team_name or "").strip() and level < 7:
        return "missing team"
    if not (position_name or "").strip() and level < 6:
        return "missing position"
    return None


def _requested_non_senior(ctx: Optional[AgenticContext]) -> bool:
    return bool(getattr(ctx, "allow_non_senior", False))


def _sort_for_squad_preference(docs: List[Document], ctx: Optional[AgenticContext]) -> List[Document]:
    wants_non_senior = _requested_non_senior(ctx)
    return sorted(
        docs or [],
        key=lambda doc: (
            0 if is_non_senior_team((doc.metadata or {}).get("team_name") or (doc.metadata or {}).get("team")) == wants_non_senior else 1,
            -(_num((doc.metadata or {}).get("Rating")) or 0),
            -(_num((doc.metadata or {}).get("match_count")) or 0),
        ),
    )


def canonical_league(value: Optional[Any]) -> Optional[str]:
    key = norm_name(str(value or ""))
    compact = re.sub(r"[^a-z0-9]+", "", key)
    return LEAGUE_BY_KEY.get(key) or LEAGUE_COMPACT_BY_KEY.get(compact)


def infer_league_from_text(*texts: Optional[str]) -> Optional[str]:
    raw = " ".join(text or "" for text in texts)
    normalized = norm_name(raw)
    compact_text = re.sub(r"[^a-z0-9]+", "", normalized)
    if not normalized:
        return None
    for key, league in sorted(LEAGUE_BY_KEY.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", normalized):
            return league
    for compact, league in sorted(LEAGUE_COMPACT_BY_KEY.items(), key=lambda item: len(item[0]), reverse=True):
        if compact and compact in compact_text:
            return league
    return None


ROLE_CODE_BY_KEY = {norm_name(code): long_name for code, long_name in ROLE_SHORT_TO_LONG.items()}
ROLE_LONG_BY_KEY = {norm_name(long_name): long_name for long_name in ROLE_SHORT_TO_LONG.values()}


def canonical_position(value: Optional[Any]) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    key = norm_name(text)
    return ROLE_CODE_BY_KEY.get(key) or ROLE_LONG_BY_KEY.get(key) or text


def infer_position_from_text(*texts: Optional[str]) -> Optional[str]:
    raw = " ".join(text or "" for text in texts)
    normalized = norm_name(raw)
    if not normalized:
        return None
    for code, long_name in sorted(ROLE_SHORT_TO_LONG.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(code.lower())}\b", raw.lower()):
            return long_name
    for key, long_name in sorted(ROLE_LONG_BY_KEY.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", normalized):
            return long_name
    return None


CANONICAL_NATIONALITIES = {
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Antigua and Barbuda",
    "Argentina", "Armenia", "Aruba", "Australia", "Austria", "Azerbaijan", "Bahrain",
    "Bangladesh", "Barbados", "Belarus", "Belgium", "Belize", "Benin", "Bermuda",
    "Bhutan", "Bolivia", "Bosnia and Herzegovina", "Botswana", "Brazil", "British Virgin Islands",
    "Brunei", "Bulgaria", "Burkina Faso", "Burundi", "Cambodia", "Cameroon", "Canada",
    "Cape Verde", "Caribbean Netherlands", "Central African Republic", "Chad", "Chile", "China",
    "Colombia", "Comoros", "Cook Islands", "Costa Rica", "Croatia", "Cuba", "Curaçao",
    "Cyprus", "Czech Republic", "Denmark", "Dominican Republic", "DR Congo", "Ecuador",
    "Egypt", "El Salvador", "England", "Equatorial Guinea", "Eritrea", "Estonia", "Ethiopia",
    "Faroe Islands", "Fiji", "Finland", "France", "French Guiana", "Gabon", "Gambia",
    "Georgia", "Germany", "Ghana", "Gibraltar", "Greece", "Grenada", "Guadeloupe", "Guatemala",
    "Guinea", "Guinea-Bissau", "Guyana", "Haiti", "Honduras", "Hong Kong", "Hungary",
    "Iceland", "India", "Indonesia", "Iran", "Iraq", "Israel", "Italy", "Ivory Coast",
    "Jamaica", "Japan", "Jordan", "Kazakhstan", "Kenya", "Korea DPR", "Kosovo", "Kuwait",
    "Kyrgyz Republic", "Kyrgyzstan", "Laos", "Latvia", "Lebanon", "Lesotho", "Liberia",
    "Libya", "Liechtenstein", "Lithuania", "Luxembourg", "Macau", "Macedonia", "Madagascar",
    "Malawi", "Malaysia", "Maldives", "Mali", "Malta", "Martinique", "Mauritania", "Mauritius",
    "Mexico", "Moldova", "Mongolia", "Montenegro", "Montserrat", "Morocco", "Mozambique",
    "Namibia", "Nepal", "Netherlands", "New Caledonia", "New Zealand", "Nicaragua", "Niger",
    "Nigeria", "North & Central America", "Northern Ireland", "Norway", "Oman", "Pakistan",
    "Palestine", "Panama", "Papua New Guinea", "Paraguay", "Peru", "Philippines", "Poland",
    "Portugal", "Puerto Rico", "Qatar", "Republic of Ireland", "Republic of the Congo",
    "Romania", "Russia", "Rwanda", "Saint Kitts and Nevis", "Saint Lucia",
    "Saint Vincent and the Grenadines", "San Marino", "São Tomé and Príncipe", "Saudi Arabia",
    "Scotland", "Senegal", "Serbia", "Sierra Leone", "Singapore", "Sint Maarten", "Slovakia",
    "Slovenia", "Solomon Islands", "Somalia", "South Africa", "South America", "South Korea",
    "South Sudan", "Spain", "Sri Lanka", "Sudan", "Suriname", "Sweden", "Switzerland",
    "Syria", "Taiwan", "Tajikistan", "Tanzania", "Thailand", "Togo", "Trinidad and Tobago",
    "Tunisia", "Turkmenistan", "Turkey", "Türkiye", "Uganda", "Ukraine", "United Arab Emirates",
    "United States", "Uruguay", "Uzbekistan", "Vanuatu", "Venezuela", "Vietnam", "Wales",
    "Yemen", "Zambia", "Zimbabwe",
}
NATIONALITY_BY_KEY = {norm_name(name): name for name in CANONICAL_NATIONALITIES}
NATIONALITY_ALIASES = {
    "american": "United States", "usa": "United States", "us": "United States",
    "spanish": "Spain", "french": "France", "german": "Germany", "italian": "Italy",
    "english": "England", "british": "England", "welsh": "Wales", "scottish": "Scotland",
    "irish": "Republic of Ireland", "portuguese": "Portugal", "dutch": "Netherlands",
    "argentinian": "Argentina", "argentine": "Argentina", "brazilian": "Brazil",
    "uruguayan": "Uruguay", "turkey": "Türkiye", "turkiye": "Türkiye", "turkish": "Türkiye", "turk": "Türkiye",
    "moroccan": "Morocco", "nigerian": "Nigeria", "croatian": "Croatia",
    "polish": "Poland", "swedish": "Sweden", "norwegian": "Norway", "danish": "Denmark",
    "belgian": "Belgium", "austrian": "Austria", "swiss": "Switzerland",
    "mexican": "Mexico", "colombian": "Colombia", "chilean": "Chile", "paraguayan": "Paraguay",
    "peruvian": "Peru", "venezuelan": "Venezuela", "japanese": "Japan", "korean": "South Korea",
    "senegalese": "Senegal", "ghanaian": "Ghana", "cameroonian": "Cameroon",
    "egyptian": "Egypt", "algerian": "Algeria", "tunisian": "Tunisia",
    "ingiliz": "England", "brezilyali": "Brazil", "arjantinli": "Argentina",
    "norvecli": "Norway", "turkiye": "Türkiye", "türkiye": "Türkiye", "turk": "Türkiye", "alman": "Germany", "fransiz": "France",
    "ispanyol": "Spain", "italyan": "Italy", "portekizli": "Portugal", "hollandali": "Netherlands",
}
NATIONALITY_ALIAS_KEYS = {norm_name(key): value for key, value in NATIONALITY_ALIASES.items()}

POSITION_NEGATION_ALIASES = {
    "goalkeeper": "Goalkeeper", "goal keeper": "Goalkeeper", "keeper": "Goalkeeper", "gk": "Goalkeeper",
    "kaleci": "Goalkeeper",
    "defender": "Center Back", "centre back": "Center Back", "center back": "Center Back", "cb": "Center Back",
    "stoper": "Center Back", "savunmaci": "Center Back",
    "midfielder": "Center Midfield", "central midfielder": "Center Midfield", "cm": "Center Midfield",
    "orta saha": "Center Midfield", "ortasaha": "Center Midfield",
    "winger": "Left Wing", "kanat": "Left Wing",
    "forward": "Center Forward", "striker": "Center Forward", "attacker": "Center Forward", "cf": "Center Forward",
    "forvet": "Center Forward", "santrafor": "Center Forward",
}


def normalize_constraint_value(value: Optional[Any]) -> str:
    key = norm_name(str(value or ""))
    canonical = NATIONALITY_BY_KEY.get(key) or NATIONALITY_ALIAS_KEYS.get(key)
    return norm_name(canonical or key)


def canonical_nationality(value: Optional[Any]) -> Optional[str]:
    key = norm_name(str(value or ""))
    if not key:
        return None
    return NATIONALITY_ALIAS_KEYS.get(key) or NATIONALITY_BY_KEY.get(key)


def infer_nationality_from_text(*texts: Optional[str]) -> Optional[str]:
    normalized = norm_name(" ".join(text or "" for text in texts))
    if not normalized:
        return None
    padded = f" {normalized} "
    for alias_key, canonical in sorted(NATIONALITY_ALIAS_KEYS.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(alias_key)}\b", normalized):
            return canonical
    for nat_key, canonical in sorted(NATIONALITY_BY_KEY.items(), key=lambda item: len(item[0]), reverse=True):
        if f" {nat_key} " in padded:
            return canonical
    return None


def _unique_list(values: List[str], limit: int = 5) -> List[str]:
    out: List[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _is_negated_term(normalized_text: str, term: str) -> bool:
    term = norm_name(term)
    if not normalized_text or not term:
        return False
    escaped = re.escape(term)
    tr_suffix = r"(?:de|da|te|ta|den|dan|ten|tan|nde|nda|inde|inda|deki|daki|teki|taki)?"
    tr_negative = r"(?:olmayan|olmasin|oynamayan|oynamasin|oynamiyor|oynam\s*yor|istemiyorum|haric|haricinde|disinda)"
    return bool(
        re.search(rf"\b(?:non|not|no|without|except|excluding|exclude)\s+(?:a|an|any)?\s*{escaped}\b", normalized_text)
        or re.search(rf"\b{escaped}{tr_suffix}\s+{tr_negative}\b", normalized_text)
        or re.search(rf"\b{escaped}{tr_suffix}\s+(?:player|footballer|oyuncu|futbolcu)?\s*(?:olmasin|oynamasin|oynamiyor|oynam\s*yor|istemiyorum)\b", normalized_text)
    )


def infer_excluded_constraints_from_text(*texts: Optional[str]) -> Dict[str, List[str]]:
    normalized = norm_name(" ".join(text or "" for text in texts))
    excluded_nationalities: List[str] = []
    excluded_positions: List[str] = []
    excluded_leagues: List[str] = []

    for alias_key, canonical in sorted(NATIONALITY_ALIAS_KEYS.items(), key=lambda item: len(item[0]), reverse=True):
        if _is_negated_term(normalized, alias_key):
            excluded_nationalities.append(canonical)
    for nat_key, canonical in sorted(NATIONALITY_BY_KEY.items(), key=lambda item: len(item[0]), reverse=True):
        if _is_negated_term(normalized, nat_key):
            excluded_nationalities.append(canonical)

    position_terms = {
        **POSITION_NEGATION_ALIASES,
        **ROLE_CODE_BY_KEY,
        **ROLE_LONG_BY_KEY,
    }
    for term, canonical in sorted(position_terms.items(), key=lambda item: len(item[0]), reverse=True):
        if _is_negated_term(normalized, term):
            excluded_positions.append(canonical_position(canonical) or canonical)

    for league_key, canonical in sorted(LEAGUE_BY_KEY.items(), key=lambda item: len(item[0]), reverse=True):
        if _is_negated_term(normalized, league_key):
            excluded_leagues.append(canonical)

    return {
        "excluded_nationalities": _unique_list(excluded_nationalities),
        "excluded_positions": _unique_list(excluded_positions),
        "excluded_leagues": _unique_list(excluded_leagues),
    }


STAT_PREFERENCE_PATTERNS: List[Tuple[str, List[str]]] = [
    (r"\b(pass|passing|build.?up|distribution|playmaker|tempo)\b", ["Passes", "Accurate Passes", "Key Passes", "Passes In Final Third"]),
    (r"\b(creativ|chance|vision|final ball)\b", ["Chances Created", "Big Chances Created", "Key Passes", "Assists"]),
    (r"\b(shoot|shot|finishing|finish|scor|goal)\b", ["Goals", "Shots On Target", "Shots Total", "Big Chances Created"]),
    (r"\b(dribbl|carry|take.?on|1v1)\b", ["Successful Dribbles", "Dribble Attempts", "Dispossessed"]),
    (r"\b(cross|crossing|wide delivery)\b", ["Accurate Crosses", "Total Crosses", "Successful Crosses (%)"]),
    (r"\b(defend|tackl|ball.?win|press|intercept)\b", ["Tackles", "Tackles Won", "Interceptions", "Ball Recovery"]),
    (r"\b(aerial|header|duel|physical)\b", ["Aerials Won", "Aerials Won (%)", "Duels Won", "Total Duels"]),
    (r"\b(goal.?keep|keeper|save|shot.?stop|claim)\b", ["Saves", "Saves Insidebox", "Good High Claim", "Penalties Saved"]),
]


def infer_preferred_stats_from_text(*texts: Optional[str], limit: int = 4) -> List[str]:
    normalized = norm_name(" ".join(text or "" for text in texts))
    if not normalized:
        return []
    stats: List[str] = []
    for pattern, metrics in STAT_PREFERENCE_PATTERNS:
        if re.search(pattern, normalized):
            for metric in metrics:
                if metric in ALLOWED_METRICS and metric not in stats:
                    stats.append(metric)
                if len(stats) >= limit:
                    return stats
    return stats


def clean_constraints(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for key in [
        "gender", "position", "nationality", "league", "team",
        "age_min", "age_max", "height_min", "height_max", "weight_min", "weight_max",
    ]:
        value = raw.get(key)
        if isinstance(value, str) and value.strip() in {"", "null", "None", "none"}:
            value = None
        if key == "gender":
            gender_key = norm_name(str(value or ""))
            value = gender_key if gender_key in {"male", "female", "unknown"} else "male"
        if key == "position" and value:
            value = canonical_position(value) or value
        if key == "nationality" and value:
            value = canonical_nationality(value) or value
        if key == "league" and value:
            value = canonical_league(value)
        cleaned[key] = value

    preferred_stats = []
    for metric in raw.get("preferred_stats") or []:
        if metric in ALLOWED_METRICS and metric not in preferred_stats:
            preferred_stats.append(metric)
        if len(preferred_stats) >= 4:
            break
    cleaned["preferred_stats"] = preferred_stats

    requirements = []
    for item in raw.get("stat_requirements") or []:
        if not isinstance(item, dict):
            continue
        metric = item.get("metric")
        op = item.get("operator")
        value = _num(item.get("value"))
        if metric not in ALLOWED_METRICS or op not in {">", ">=", "<", "<=", "="} or value is None:
            continue
        requirements.append({"metric": metric, "operator": op, "value": value})
        if len(requirements) >= 3:
            break
    cleaned["stat_requirements"] = requirements

    excluded_nationalities = []
    for value in raw.get("excluded_nationalities") or []:
        canonical = canonical_nationality(value)
        if canonical and canonical not in excluded_nationalities:
            excluded_nationalities.append(canonical)
    cleaned["excluded_nationalities"] = excluded_nationalities[:5]

    excluded_positions = []
    for value in raw.get("excluded_positions") or []:
        canonical = canonical_position(value)
        if canonical and canonical not in excluded_positions:
            excluded_positions.append(canonical)
    cleaned["excluded_positions"] = excluded_positions[:5]

    excluded_leagues = []
    for value in raw.get("excluded_leagues") or []:
        canonical = canonical_league(value)
        if canonical and canonical not in excluded_leagues:
            excluded_leagues.append(canonical)
    cleaned["excluded_leagues"] = excluded_leagues[:5]

    excluded_teams = _unique_list([str(value).strip() for value in raw.get("excluded_teams") or []], limit=5)
    cleaned["excluded_teams"] = excluded_teams

    if cleaned.get("nationality") and any(
        normalize_constraint_value(cleaned["nationality"]) == normalize_constraint_value(value)
        for value in cleaned["excluded_nationalities"]
    ):
        cleaned["nationality"] = None
    if cleaned.get("position"):
        position_excluded = any(
            player_matches_requested_position(
                value,
                cleaned.get("position"),
                [cleaned.get("position")] if cleaned.get("position") else [],
            )[0]
            for value in cleaned["excluded_positions"]
        )
        if position_excluded:
            cleaned["position"] = None
    if cleaned.get("league") and any(_constraint_text_match(cleaned["league"], value) for value in cleaned["excluded_leagues"]):
        cleaned["league"] = None
    if cleaned.get("team") and any(_constraint_team_match(cleaned["team"], value) for value in cleaned["excluded_teams"]):
        cleaned["team"] = None

    cleaned["notes"] = str(raw.get("notes") or "").strip()[:160]
    return cleaned


def _constraint_text_match(candidate_value: Optional[Any], requested_value: Optional[Any], *, nationality: bool = False) -> bool:
    requested = normalize_constraint_value(requested_value) if nationality else norm_name(str(requested_value or ""))
    candidate = normalize_constraint_value(candidate_value) if nationality else norm_name(str(candidate_value or ""))
    if not requested:
        return True
    if not candidate:
        return False
    return requested == candidate or requested in candidate or candidate in requested


def _constraint_exact_match(candidate_value: Optional[Any], requested_value: Optional[Any]) -> bool:
    requested = norm_name(str(requested_value or ""))
    candidate = norm_name(str(candidate_value or ""))
    return bool(requested and candidate and requested == candidate)


def _constraint_team_match(candidate_value: Optional[Any], requested_value: Optional[Any]) -> bool:
    requested = str(requested_value or "").strip()
    candidate = str(candidate_value or "").strip()
    if not requested:
        return True
    if not candidate:
        return False
    return is_same_club(candidate, requested)


def _passes_numeric_bound(value: Optional[float], min_value: Optional[Any], max_value: Optional[Any]) -> bool:
    numeric = _num(value)
    min_num = _num(min_value)
    max_num = _num(max_value)
    if numeric is None and (min_num is not None or max_num is not None):
        return False
    if min_num is not None and numeric < min_num:
        return False
    if max_num is not None and numeric > max_num:
        return False
    return True


def _passes_stat_requirement(candidate_stats: Dict[str, float], requirement: Dict[str, Any]) -> bool:
    value = candidate_stats.get(requirement.get("metric"))
    target = _num(requirement.get("value"))
    if value is None or target is None:
        return False
    op = requirement.get("operator")
    if op == ">":
        return value > target
    if op == ">=":
        return value >= target
    if op == "<":
        return value < target
    if op == "<=":
        return value <= target
    if op == "=":
        return abs(value - target) <= 0.05
    return False


def candidate_constraint_rejection(candidate: Dict[str, Any], ctx: Optional[AgenticContext]) -> Optional[str]:
    if not ctx or ctx.direct_player_lookup:
        return None
    constraints = clean_constraints(ctx.constraints)
    if not constraints:
        return None
    level = int(getattr(ctx, "constraint_relaxation_level", 0) or 0)

    for nationality in constraints.get("excluded_nationalities") or []:
        if _constraint_text_match(candidate.get("nationality"), nationality, nationality=True):
            return "excluded nationality"

    for position in constraints.get("excluded_positions") or []:
        position_match, _, _ = player_matches_requested_position(
            position,
            candidate.get("position_name"),
            [candidate.get("position_name")] if candidate.get("position_name") else [],
        )
        if position_match:
            return "excluded position"

    for team in constraints.get("excluded_teams") or []:
        if _constraint_team_match(candidate.get("team"), team):
            return "excluded team"

    for league in constraints.get("excluded_leagues") or []:
        if _constraint_text_match(candidate.get("league_name"), league):
            return "excluded league"

    if level < 8 and constraints.get("gender") and not _constraint_exact_match(candidate.get("gender"), constraints.get("gender")):
        return "constraint gender"

    if level < 5 and constraints.get("nationality") and not _constraint_text_match(
        candidate.get("nationality"),
        constraints.get("nationality"),
        nationality=True,
    ):
        return "constraint nationality"

    if level < 6 and constraints.get("position"):
        position_ok, _, _ = player_matches_requested_position(
            constraints.get("position"),
            candidate.get("position_name"),
            [candidate.get("position_name")] if candidate.get("position_name") else [],
        )
        if not position_ok:
            return "constraint position"

    if level < 7 and constraints.get("team") and not _constraint_team_match(candidate.get("team"), constraints.get("team")):
        return "constraint team"

    if constraints.get("league") and not _constraint_text_match(candidate.get("league_name"), constraints.get("league")):
        return "constraint league"

    if level < 4 and not _passes_numeric_bound(candidate.get("age"), constraints.get("age_min"), constraints.get("age_max")):
        return "constraint age"

    if level < 3 and not _passes_numeric_bound(candidate.get("height"), constraints.get("height_min"), constraints.get("height_max")):
        return "constraint height"

    if level < 2 and not _passes_numeric_bound(candidate.get("weight"), constraints.get("weight_min"), constraints.get("weight_max")):
        return "constraint weight"

    if level < 1:
        candidate_stats = {
            stat.get("metric"): _num(stat.get("value"))
            for stat in candidate.get("stats") or []
            if stat.get("metric") and _num(stat.get("value")) is not None
        }
        for requirement in constraints.get("stat_requirements") or []:
            if not _passes_stat_requirement(candidate_stats, requirement):
                return "constraint stat requirement"
        preferred_stats = constraints.get("preferred_stats") or []
        if preferred_stats and not any(metric in candidate_stats for metric in preferred_stats):
            return "constraint preferred stats"

    return None


def metadata_constraint_rejection(metadata: Dict[str, Any], ctx: Optional[AgenticContext]) -> Optional[str]:
    md = metadata or {}
    age = _num(md.get("age"))
    return candidate_constraint_rejection({
        "gender": md.get("gender"),
        "height": _num(md.get("height")),
        "weight": _num(md.get("weight")),
        "age": int(round(age)) if age is not None else None,
        "nationality": md.get("nationality_name") or md.get("nationality") or md.get("country"),
        "team": md.get("team_name") or md.get("team") or md.get("club"),
        "league_name": _league_from_metadata(md),
        "position_name": md.get("position_name") or md.get("position"),
        "stats": extract_allowed_stats_from_metadata(md),
    }, ctx)


def constraint_relaxation_label(level: int) -> str:
    labels = {
        0: "strict",
        1: "relaxed_stats",
        2: "relaxed_weight",
        3: "relaxed_height",
        4: "relaxed_age",
        5: "relaxed_nationality",
        6: "relaxed_position",
        7: "relaxed_team",
        8: "relaxed_gender",
    }
    return labels.get(int(level or 0), "relaxed_all")


def _quality_thresholds(ctx: Optional[AgenticContext]) -> Dict[str, int]:
    return {
        "min_age": 20,
        "max_age": 30,
        "min_match_count": 15,
    }


def _passes_quality_discovery_metadata(metadata: Dict[str, Any], ctx: Optional[AgenticContext] = None) -> bool:
    thresholds = _quality_thresholds(ctx)
    age = _num((metadata or {}).get("age"))
    match_count = _num((metadata or {}).get("match_count"))
    if age is None or int(round(age)) < thresholds["min_age"] or int(round(age)) > thresholds["max_age"]:
        return False
    if match_count is None or match_count < thresholds["min_match_count"]:
        return False
    return _stats_count_from_metadata(metadata or {}) >= MIN_SELECTION_STATS


def _is_transfer_fallback_club_strict(team_name: Optional[str]) -> bool:
    team_norm = norm_name(team_name or "")
    if not team_norm:
        return False
    return any(team_norm == norm_name(club_name) for club_name in TRANSFER_FALLBACK_CLUBS)


def _doc_identity_key(doc: Document) -> str:
    md = doc.metadata or {}
    name = md.get("player_name") or md.get("name") or ""
    team = md.get("team_name") or md.get("team") or md.get("club") or ""
    return f"{norm_name(str(name))}|{norm_name(str(team))}"


def _merge_docs(existing: List[Document], new_docs: List[Document], *, limit: int = SELECTOR_CANDIDATE_LIMIT) -> List[Document]:
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


def _diverse_doc_cap(
    docs: List[Document],
    *,
    limit: int = SELECTOR_CANDIDATE_LIMIT,
    diversify_teams: bool = True,
) -> List[Document]:
    if not diversify_teams:
        return list(docs or [])[:limit]

    selected: List[Document] = []
    selected_teams = set()
    selected_leagues = set()
    remaining: List[Document] = []

    for doc in docs or []:
        md = doc.metadata or {}
        team_key = norm_name(str(md.get("team_name") or md.get("team") or md.get("club") or ""))
        league_key = norm_name(_league_from_metadata(md))
        if team_key and team_key in selected_teams:
            continue
        if league_key and league_key not in selected_leagues:
            selected.append(doc)
            selected_teams.add(team_key)
            selected_leagues.add(league_key)
            if len(selected) >= limit:
                return selected
        else:
            remaining.append(doc)

    for doc in remaining:
        md = doc.metadata or {}
        team_key = norm_name(str(md.get("team_name") or md.get("team") or md.get("club") or ""))
        if team_key and team_key in selected_teams:
            continue
        selected.append(doc)
        selected_teams.add(team_key)
        if len(selected) >= limit:
            break
    return selected


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
        "Prefer senior first-team players with reliable recent performance evidence."
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
    constraints: Optional[Dict[str, Any]] = None,
) -> AgenticContext:
    translated = rewrite_position_reference_phrases(translated_question)
    planner_data = planner_data or {}
    generic_alternative = is_generic_alternative_request(translated)
    planner_intent = planner_data.get("intent")
    named_lookup_query = extract_named_player_lookup_query(
        original_question,
        translated,
        planner_data.get("effective_query") or "",
    )
    heuristic_direct_lookup = is_direct_player_lookup_request_agentic(original_question, translated)
    direct_lookup = planner_intent == "direct_player_lookup" or (
        not planner_intent and heuristic_direct_lookup
    )
    if named_lookup_query:
        direct_lookup = True

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

    effective_query = (named_lookup_query or planner_data.get("effective_query") or translated).strip()
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
    if (
        intent == "seen_player_followup"
        and not mentions_seen
        and is_constraint_modification_request(translated)
    ):
        intent = "alternative_recommendation"
    if len(comparison_players) >= 2:
        intent = "comparison"
    if named_lookup_query:
        intent = "direct_player_lookup"
    if intent != "direct_player_lookup":
        direct_lookup = False

    premium_only = is_premium_request(translated)
    cleaned_constraints = clean_constraints(constraints)
    inferred_exclusions = infer_excluded_constraints_from_text(
        original_question,
        translated,
        planner_data.get("effective_query") or "",
    )
    for key, values in inferred_exclusions.items():
        if values:
            cleaned_constraints[key] = _unique_list([*(cleaned_constraints.get(key) or []), *values])
    cleaned_constraints = clean_constraints(cleaned_constraints)
    source_team_phrase = bool(
        target_team
        and (
            _mentions_club_as_source_team(original_question, target_team)
            or _mentions_club_as_source_team(translated, target_team)
        )
    )
    if source_team_phrase:
        cleaned_constraints["team"] = cleaned_constraints.get("team") or target_team
        note = cleaned_constraints.get("notes") or ""
        suffix = f" Treated {target_team} as source/current team constraint, not target team."
        cleaned_constraints["notes"] = (note + suffix).strip()[:220]
        target_team = None
    elif target_team and cleaned_constraints.get("team") and is_same_club(target_team, cleaned_constraints.get("team")):
        cleaned_constraints["team"] = None
        note = cleaned_constraints.get("notes") or ""
        suffix = f" Treated {target_team} as target team, not source team."
        cleaned_constraints["notes"] = (note + suffix).strip()[:160]

    if direct_lookup and not named_lookup_query and _has_search_constraints(cleaned_constraints) and (
        _looks_like_generic_player_search(original_question)
        or _looks_like_generic_player_search(translated)
    ):
        direct_lookup = False
        if intent == "direct_player_lookup":
            intent = "new_recommendation"

    discovery_mode = not mentions_seen and not direct_lookup
    quality_discovery_mode = (
        discovery_mode
        and intent in {"new_recommendation", "alternative_recommendation"}
        and not is_narrow_filtered_suggestion_request(translated, strategy)
        and premium_only
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
        initial_strong_club_default=False,
        discovery_mode=discovery_mode,
        allow_turkish=(
            request_allows_turkish_entities(translated)
            or normalize_constraint_value(cleaned_constraints.get("nationality")) == "turkiye"
            or bool(cleaned_constraints.get("team") and is_disallowed_turkish_club(cleaned_constraints.get("team")))
        ),
        allow_non_senior=request_allows_non_senior_squads(translated),
        premium_only=premium_only,
        quality_discovery_mode=quality_discovery_mode,
        constraints=cleaned_constraints,
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
        league_name = _league_from_metadata(md)
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
            premium_only=False,
        )
        if rejection_reason:
            rejection_counts[rejection_reason] += 1
            continue
        if not ctx.direct_player_lookup and _has_explicit_league_constraint(ctx) and not _is_selectable_league(league_name, ctx):
            rejection_counts["league_restriction"] += 1
            continue
        if restrict_to_fallback_clubs and not _is_transfer_fallback_club_strict(team_name):
            rejection_counts["fallback_club_restriction"] += 1
            continue
        missing_discovery = _missing_discovery_rejection(team_name, position_name, ctx) if require_complete_discovery_fields else None
        if missing_discovery:
            rejection_counts[missing_discovery] += 1
            continue
        if ctx.quality_discovery_mode and not _passes_quality_discovery_metadata(md, ctx):
            rejection_counts["quality_metadata_floor"] += 1
            continue
        if _stats_count_from_metadata(md) < MIN_SELECTION_STATS:
            rejection_counts["stats_floor"] += 1
            continue
        constraint_rejection = metadata_constraint_rejection(md, ctx)
        if constraint_rejection:
            rejection_counts[constraint_rejection] += 1
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
    else:
        random.SystemRandom().shuffle(filtered_docs)
    docs_out = filtered_docs[:limit]
    ctx.retrieval_debug.append({
        "pass": pass_label,
        "raw_count": len(raw_docs_list),
        "accepted_count": len(filtered_docs),
        "returned_count": len(docs_out),
        "top_rejections": rejection_counts.most_common(5),
    })
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


def fetch_quality_suggestion_docs_from_db(ctx: AgenticContext, *, limit: int = SELECTOR_CANDIDATE_LIMIT) -> List[Document]:
    thresholds = _quality_thresholds(ctx)
    db = get_db()
    try:
        rows = db.execute(text("""
            SELECT id, metadata, content
            FROM player_data
            WHERE
                (metadata->>'age') IS NOT NULL
                AND (metadata->>'match_count') IS NOT NULL
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
        league_name = _league_from_metadata(md)
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
            premium_only=False,
        )
        if rejection_reason:
            rejection_counts[rejection_reason] += 1
            continue
        if _has_explicit_league_constraint(ctx) and not _is_selectable_league(league_name, ctx):
            rejection_counts["league_restriction"] += 1
            continue
        if ctx.initial_strong_club_default and not _is_transfer_fallback_club_strict(team_name):
            rejection_counts["fallback_club_restriction"] += 1
            continue
        missing_discovery = _missing_discovery_rejection(team_name, position_name, ctx)
        if missing_discovery:
            rejection_counts[missing_discovery] += 1
            continue
        if not _passes_quality_discovery_metadata(md, ctx):
            rejection_counts["quality_metadata_floor"] += 1
            continue
        if _stats_count_from_metadata(md) < MIN_SELECTION_STATS:
            rejection_counts["stats_floor"] += 1
            continue
        constraint_rejection = metadata_constraint_rejection(md, ctx)
        if constraint_rejection:
            rejection_counts[constraint_rejection] += 1
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
    ctx.retrieval_debug.append({
        "pass": "db_quality_pass",
        "raw_count": len(rows or []),
        "accepted_count": len(docs),
        "returned_count": len(docs_out),
        "top_rejections": rejection_counts.most_common(5),
    })
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


def fetch_selection_suggestion_docs_from_db(
    ctx: AgenticContext,
    *,
    limit: int = SELECTOR_CANDIDATE_LIMIT,
    enforce_allowed_leagues: bool = True,
) -> List[Document]:
    constraints = clean_constraints(ctx.constraints)
    relaxation_level = int(ctx.constraint_relaxation_level or 0)
    where_parts = ["TRUE"]
    params: Dict[str, Any] = {"lim": 1200}
    sql_filters_applied: List[str] = []

    def add_text_filter(field: str, param: str, value: Any) -> None:
        if value is None:
            return
        where_parts.append(f"LOWER(metadata->>'{field}') = :{param}")
        params[param] = str(value).lower()
        sql_filters_applied.append(param)

    def add_team_filter(value: Any) -> None:
        if value is None:
            return
        tokens = [token for token in norm_name(str(value)).split() if len(token) >= 3]
        if not tokens:
            add_text_filter("team_name", "team", value)
            return
        folded_team_sql = (
            "LOWER(TRANSLATE(COALESCE(metadata->>'team_name', ''), "
            ":sql_fold_from, :sql_fold_to))"
        )
        params["sql_fold_from"] = SQL_FOLD_FROM
        params["sql_fold_to"] = SQL_FOLD_TO
        clauses = []
        for index, token in enumerate(tokens):
            param = f"team_token_{index}"
            clauses.append(f"{folded_team_sql} LIKE :{param}")
            params[param] = f"%{token}%"
        where_parts.append("(" + " OR ".join(clauses) + ")")
        sql_filters_applied.append("team")

    def add_nationality_filter(value: Any) -> None:
        if value is None:
            return
        canonical = canonical_nationality(value) or str(value)
        keys = {norm_name(str(value)), norm_name(canonical)}
        if canonical == "Türkiye":
            keys.update({"turkiye", "turkey", "turkish", "turk"})
        keys = {key for key in keys if key}
        folded_nat_sql = (
            "LOWER(TRANSLATE(COALESCE(metadata->>'nationality_name', ''), "
            ":sql_fold_from, :sql_fold_to))"
        )
        params["sql_fold_from"] = SQL_FOLD_FROM
        params["sql_fold_to"] = SQL_FOLD_TO
        clauses = []
        for index, key in enumerate(sorted(keys)):
            param = f"nationality_key_{index}"
            clauses.append(f"{folded_nat_sql} = :{param}")
            params[param] = key
        if clauses:
            where_parts.append("(" + " OR ".join(clauses) + ")")
            sql_filters_applied.append("nationality")

    def add_numeric_min(field: str, param: str, value: Any) -> None:
        numeric_value = _num(value)
        if numeric_value is None:
            return
        where_parts.append(
            f"(metadata->>'{field}') ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            f"AND (metadata->>'{field}')::numeric >= :{param}"
        )
        params[param] = numeric_value
        sql_filters_applied.append(param)

    def add_numeric_max(field: str, param: str, value: Any) -> None:
        numeric_value = _num(value)
        if numeric_value is None:
            return
        where_parts.append(
            f"(metadata->>'{field}') ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            f"AND (metadata->>'{field}')::numeric <= :{param}"
        )
        params[param] = numeric_value
        sql_filters_applied.append(param)

    if relaxation_level < 8:
        add_text_filter("gender", "gender", constraints.get("gender"))
    if relaxation_level < 5:
        add_nationality_filter(constraints.get("nationality"))
    add_text_filter("league_name", "league", constraints.get("league"))
    if relaxation_level < 7:
        add_team_filter(constraints.get("team"))
    if relaxation_level < 4:
        add_numeric_min("age", "age_min", constraints.get("age_min"))
        add_numeric_max("age", "age_max", constraints.get("age_max"))
    if relaxation_level < 3:
        add_numeric_min("height", "height_min", constraints.get("height_min"))
        add_numeric_max("height", "height_max", constraints.get("height_max"))
    if relaxation_level < 2:
        add_numeric_min("weight", "weight_min", constraints.get("weight_min"))
        add_numeric_max("weight", "weight_max", constraints.get("weight_max"))

    where_sql = "\n                AND ".join(where_parts)
    db = get_db()
    try:
        rows = db.execute(text(f"""
            SELECT id, metadata, content
            FROM player_data
            WHERE
                {where_sql}
            ORDER BY COALESCE((metadata->>'Rating')::numeric, 0) DESC
            LIMIT :lim
        """), params).mappings().all()
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
        league_name = _league_from_metadata(md)
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
            premium_only=False,
        )
        if rejection_reason:
            rejection_counts[rejection_reason] += 1
            continue
        if enforce_allowed_leagues and constraints.get("league") and not _is_selectable_league(league_name, ctx):
            rejection_counts["league_restriction"] += 1
            continue
        missing_discovery = _missing_discovery_rejection(team_name, position_name, ctx)
        if missing_discovery:
            rejection_counts[missing_discovery] += 1
            continue
        if _stats_count_from_metadata(md) < MIN_SELECTION_STATS:
            rejection_counts["stats_floor"] += 1
            continue
        constraint_rejection = metadata_constraint_rejection(md, ctx)
        if constraint_rejection:
            rejection_counts[constraint_rejection] += 1
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

    random.SystemRandom().shuffle(docs)
    docs = _sort_for_squad_preference(docs, ctx)
    docs_out = _diverse_doc_cap(docs, limit=limit, diversify_teams=not bool(constraints.get("team")))
    ctx.retrieval_debug.append({
        "pass": (
            f"db_selection_pass:{constraint_relaxation_label(ctx.constraint_relaxation_level)}"
            f":{'allowed_leagues' if enforce_allowed_leagues else 'all_leagues'}"
            f":sql={','.join(sql_filters_applied) or 'none'}"
            f":{'u19_preference' if _requested_non_senior(ctx) else 'senior_preference'}"
        ),
        "raw_count": len(rows or []),
        "accepted_count": len(docs),
        "returned_count": len(docs_out),
        "top_rejections": rejection_counts.most_common(5),
    })
    return docs_out


def build_filtered_retriever_agentic(
    ctx: AgenticContext,
    candidate_retriever: BaseRetriever,
    broad_candidate_retriever: BaseRetriever,
) -> Tuple[BaseRetriever, List[Document]]:
    docs: List[Document] = []
    diversify_teams = not bool(clean_constraints(ctx.constraints).get("team"))
    if ctx.discovery_mode and not ctx.direct_player_lookup:
        original_level = int(ctx.constraint_relaxation_level or 0)
        if ctx.quality_discovery_mode:
            docs = fetch_quality_suggestion_docs_from_db(ctx)
            if not docs:
                ctx.retrieval_debug.append({
                    "pass": "quality_relaxed_to_selection",
                    "raw_count": 0,
                    "accepted_count": 0,
                    "returned_count": 0,
                    "top_rejections": [("quality_docs_empty", 1)],
                })
                for level in range(original_level, 9):
                    ctx.constraint_relaxation_level = level
                    db_docs = fetch_selection_suggestion_docs_from_db(ctx, enforce_allowed_leagues=True)
                    docs = _merge_docs(docs, db_docs)
                    docs = _diverse_doc_cap(docs, limit=SELECTOR_CANDIDATE_LIMIT, diversify_teams=diversify_teams)
                    if len(docs or []) >= SELECTOR_CANDIDATE_LIMIT or docs:
                        break
            if not docs:
                ctx.allow_all_selection_leagues = True
                for level in range(original_level, 9):
                    ctx.constraint_relaxation_level = level
                    db_docs = fetch_selection_suggestion_docs_from_db(ctx, enforce_allowed_leagues=False)
                    docs = _merge_docs(docs, db_docs)
                    docs = _diverse_doc_cap(docs, limit=SELECTOR_CANDIDATE_LIMIT, diversify_teams=diversify_teams)
                    if len(docs or []) >= SELECTOR_CANDIDATE_LIMIT or docs:
                        break
            if not docs:
                ctx.constraint_relaxation_level = 8
                db_docs = fetch_selection_suggestion_docs_from_db(ctx, enforce_allowed_leagues=False)
                docs = _merge_docs(docs, db_docs)
                docs = _diverse_doc_cap(docs, limit=SELECTOR_CANDIDATE_LIMIT, diversify_teams=diversify_teams)
        else:
            for level in range(original_level, 9):
                ctx.constraint_relaxation_level = level
                db_docs = fetch_selection_suggestion_docs_from_db(ctx, enforce_allowed_leagues=True)
                docs = _merge_docs(docs, db_docs)
                docs = _diverse_doc_cap(docs, limit=SELECTOR_CANDIDATE_LIMIT, diversify_teams=diversify_teams)
                if len(docs or []) >= SELECTOR_CANDIDATE_LIMIT or docs:
                    break
            if not docs:
                ctx.allow_all_selection_leagues = True
                for level in range(original_level, 9):
                    ctx.constraint_relaxation_level = level
                    db_docs = fetch_selection_suggestion_docs_from_db(ctx, enforce_allowed_leagues=False)
                    docs = _merge_docs(docs, db_docs)
                    docs = _diverse_doc_cap(docs, limit=SELECTOR_CANDIDATE_LIMIT, diversify_teams=diversify_teams)
                    if len(docs or []) >= SELECTOR_CANDIDATE_LIMIT or docs:
                        break
            if not docs:
                ctx.constraint_relaxation_level = 8
                db_docs = fetch_selection_suggestion_docs_from_db(ctx, enforce_allowed_leagues=False)
                docs = _merge_docs(docs, db_docs)
                docs = _diverse_doc_cap(docs, limit=SELECTOR_CANDIDATE_LIMIT, diversify_teams=diversify_teams)

    if not docs and not clean_constraints(ctx.constraints).get("team"):
        fallback_query = _transfer_target_query(ctx) if ctx.target_team and ctx.intent in {"new_recommendation", "alternative_recommendation"} else (ctx.effective_query or "")
        fallback_docs = filter_candidate_docs(
            broad_candidate_retriever.invoke(fallback_query),
            ctx,
            fallback_query,
            restrict_to_fallback_clubs=False,
            require_complete_discovery_fields=ctx.discovery_mode,
            pass_label="emergency_vector_fallback",
        )
        docs = _merge_docs(docs, fallback_docs)

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
        "position_counts": md.get("position_counts"),
        "position_count_total": md.get("position_count_total"),
        "position_names_seen": md.get("position_names_seen"),
        "primary_position_code": md.get("primary_position_code"),
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
        premium_only=False,
    )
    if reason and not ctx.direct_player_lookup:
        return reason
    if ctx.target_team and is_same_club(ctx.target_team, candidate.get("team")) and not ctx.direct_player_lookup:
        return "same target club"
    if ctx.premium_only and not ctx.direct_player_lookup:
        if candidate.get("age") is None or int(candidate["age"]) < 20 or int(candidate["age"]) > 30:
            return "premium age restriction"
        if candidate.get("rating") is None or float(candidate["rating"]) <= 7:
            return "premium rating restriction"
        if candidate.get("form") is None or int(candidate["form"]) <= 80:
            return "premium form restriction"
        if candidate.get("potential") is None or int(candidate["potential"]) <= 80:
            return "premium potential restriction"
    if ctx.discovery_mode:
        missing_discovery = _missing_discovery_rejection(candidate.get("team"), candidate.get("position_name"), ctx)
        if missing_discovery:
            return missing_discovery
    if (
        not ctx.direct_player_lookup
        and not ctx.allow_all_selection_leagues
        and _has_explicit_league_constraint(ctx)
        and not _is_selectable_league(candidate.get("league_name"), ctx)
    ):
        return "league restriction"
    if not ctx.direct_player_lookup and len(candidate.get("stats") or []) < MIN_SELECTION_STATS:
        return "requires at least 3 available stats"
    constraint_rejection = candidate_constraint_rejection(candidate, ctx)
    if constraint_rejection:
        return constraint_rejection
    if ctx.quality_discovery_mode:
        thresholds = _quality_thresholds(ctx)
        stats_count = len(candidate.get("stats") or [])
        age = candidate.get("age")
        match_count = candidate.get("match_count")
        if stats_count < MIN_SELECTION_STATS:
            return "requires at least 3 available stats"
        if age is None or int(age) < thresholds["min_age"] or int(age) > thresholds["max_age"]:
            return "broad suggestion age band restriction"
        if match_count is None or float(match_count) < thresholds["min_match_count"]:
            return "broad suggestion match-count restriction"
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
            "id": candidate.get("id"),
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
            "position_counts": candidate.get("position_counts"),
            "position_count_total": candidate.get("position_count_total"),
            "position_names_seen": candidate.get("position_names_seen"),
            "primary_position_code": candidate.get("primary_position_code"),
            "potential": candidate.get("potential"),
            "form": candidate.get("form"),
        }]
    }


def build_payload_from_candidate(candidate: Dict[str, Any], seen_players: set[str]) -> Tuple[Dict[str, Any], set[str]]:
    meta = candidate_to_meta(candidate)
    meta_new, new_names = filter_players_by_seen(meta, seen_players)
    payload = build_player_payload_new(meta_new) if new_names else {"players": []}
    if payload.get("players"):
        payload["players"][0].setdefault("id", candidate.get("id"))
        payload_meta = payload["players"][0].setdefault("meta", {})
        payload_meta["potential"] = candidate.get("potential")
        payload_meta["form"] = candidate.get("form")
        for key in ("position_counts", "position_count_total", "position_names_seen", "primary_position_code"):
            if candidate.get(key) is not None:
                payload_meta[key] = candidate.get(key)
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
