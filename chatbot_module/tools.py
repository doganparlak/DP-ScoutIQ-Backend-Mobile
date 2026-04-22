import re
import json
import math
import unicodedata
from typing import Dict, Any, Tuple, Iterable, Optional, List
import matplotlib.pyplot as plt

LANG_DIRECTIVES = {
    "en": (
        "LANGUAGE POLICY — ENGLISH ONLY:\n"
        "- You must respond in English only.\n"
        "- Do not switch languages for any reason (even if the user writes or asks in another language).\n"
        "- If the user writes in another language, reply in English and briefly note you will continue in English.\n"
        "- Do not include side-by-side translations. Keep proper nouns as-is. Numbers are fine.\n"
        "- If asked to translate or switch language, refuse and state you can only reply in English."
    ),
    "tr": (
        "DİL POLİTİKASI — YALNIZCA TÜRKÇE:\n"
        "- Yalnızca Türkçe yanıt ver.\n"
        "- Hiçbir koşulda dil değiştirme (kullanıcı başka dilde yazsa veya istese bile).\n"
        "- Kullanıcı başka dilde yazarsa, Türkçe yanıt ver ve kısaca Türkçe devam edeceğini belirt.\n"
        "- Yan yana çeviriler verme. Özel isimleri olduğu gibi bırak. Sayılar sorun değil.\n"
        "- Dili değiştirme veya çeviri talebi gelirse, yalnızca Türkçe yanıt verebildiğini belirt ve reddet."
    ),
}
PLAYER_PROFILE_OPEN_TAG_RE = re.compile(r"\[\[\s*PLAYER_PROFILE\s*:\s*(.*?)\s*\]\]", re.IGNORECASE)
PAYLOAD_JSON_BLOCK_RE = re.compile(
    r"\[\[\s*PAYLOAD_JSON\s*\]\](?P<body>[\s\S]*?)\[\[\/PAYLOAD_JSON\]\]",
    re.IGNORECASE,
)
HEAVY_TAGS_RE = re.compile(r"(<img[^>]*>|<table[\s\S]*?</table>)", re.IGNORECASE)
# Flagged block delimiters (exact tokens instructed in system_message)
FLAG_BLOCK_START_RE = re.compile(r"^\s*\[\[(PLAYER_PROFILE|PLAYER_STATS)(?::[^\]]+)?\]\]\s*$", re.IGNORECASE)
FLAG_BLOCK_END_RE   = re.compile(r"^\s*\[\[/(PLAYER_PROFILE|PLAYER_STATS)\]\]\s*$", re.IGNORECASE)
PLAYER_ANALYSIS_HEADER_RE = re.compile(
    r"^\s*\*\*(?:Player\s+Analysis\s*:?\s*)?(?P<name>.+?)\*\*\s*$",
    re.IGNORECASE,
)
META_LINE_RE = re.compile(
    r"""^\s*-\s*\*\*(?:
        Nationality
        |Age(?:\s*(?:\(as\s*of\s*2025\)|\(2025\))?)?
        |Primary\s*Role
        |Secondary\s*Roles?
        |Roles?
        |Potential
    )\*\*:\s*.+$""",
    re.IGNORECASE | re.VERBOSE,
)
STATS_HEADER_RE = re.compile(r"^\s*\*\*Performance\s+Statistics\*\*\s*:?\s*$", re.IGNORECASE)
STATS_ITEM_RE = re.compile(
    r"^\s*(?:\d+\.\s+|\-\s+\*\*[^*]+?\*\*:\s+).+?$",
    re.IGNORECASE,
)
PROFILE_BLOCK_RE = re.compile(
    r"\[\[\s*PLAYER_PROFILE\s*:\s*(?P<name>[^\]]+)\s*\]\](?P<body>[\s\S]*?)\[\[\/PLAYER_PROFILE\]\]",
    re.IGNORECASE
)
BUL_NAT_RE  = re.compile(r"^\s*-\s*Nationality\s*:\s*(?P<val>.+?)\s*$", re.IGNORECASE)
BUL_AGE_RE  = re.compile(r"^\s*-\s*Age(?:\s*\(.*?\))?\s*:\s*(?P<val>\d{1,3})\s*$", re.IGNORECASE)
BUL_ROLE_RE = re.compile(r"^\s*-\s*Roles?\s*:\s*(?P<val>.+?)\s*$", re.IGNORECASE)
BUL_POT_RE  = re.compile(r"^\s*-\s*Potential\s*:\s*(?P<val>\d{1,3})\s*$", re.IGNORECASE)

DISALLOWED_TURKISH_CLUBS = [
    "Galatasaray", "Fenerbahce", "Fenerbahçe", "Besiktas", "Beşiktaş", "Trabzonspor",
    "Goztepe", "Göztepe", "Istanbul Basaksehir", "İstanbul Başakşehir", "Samsunspor",
    "Gaziantep FK", "Kocaelispor", "Alanyaspor", "Genclerbirligi", "Gençlerbirliği",
    "Caykur Rizespor", "Çaykur Rizespor", "Kayserispor", "Kasimpasa", "Kasımpaşa",
    "Fatih Karagumruk", "Fatih Karagümrük", "Eyupspor", "Eyüpspor", "Antalyaspor",
    "Hatayspor", "Adana Demirspor", "Altay", "Amed SK", "Ankara Keciorengucu",
    "Ankara Keçiörengücü", "Bandirmaspor", "Bandırmaspor", "Boluspor", "Bodrum FK",
    "Corum FK", "Çorum FK", "Erzurumspor FK", "Esenler Erokspor", "Igdir FK",
    "Iğdır FK", "Istanbulspor", "İstanbulspor", "Manisa FK", "Pendikspor",
    "Sakaryaspor", "Sariyer", "Sarıyer", "Serik Belediyespor", "Umraniyespor",
    "Ümraniyespor", "Van Spor FK", "Sivasspor",
]

PREMIUM_ALLOWED_CLUBS = [
    "Real Madrid", "Real Madrid CF",
    "Bayern Munich", "FC Bayern Munich", "Bayern Munchen", "FC Bayern Munchen",
    "Liverpool FC", "Liverpool",
    "Inter Milan", "Inter", "FC Internazionale Milano", "Internazionale", "Inter Milano",
    "Paris Saint-Germain", "Paris Saint Germain", "PSG",
    "Manchester City", "Manchester City FC", "Man City",
    "Bayer Leverkusen", "Bayer 04 Leverkusen",
    "Borussia Dortmund", "BVB", "BV Borussia Dortmund",
    "FC Barcelona", "Barcelona", "Barca", "FC Barcellona",
    "AS Roma", "Roma", "A S Roma",
    "SL Benfica", "Benfica", "Sport Lisboa e Benfica",
    "Atletico Madrid", "Atletico de Madrid", "Club Atletico de Madrid", "Atletico Madrid", "Atletico",
    "Manchester United", "Manchester United FC", "Man United",
    "Chelsea FC", "Chelsea",
    "Arsenal FC", "Arsenal",
    "Eintracht Frankfurt", "SG Eintracht Frankfurt",
    "West Ham United", "West Ham", "West Ham United FC",
    "Feyenoord", "Feyenoord Rotterdam",
    "AC Milan", "Milan", "AC Milan", "Associazione Calcio Milan",
    "Atalanta BC", "Atalanta", "Atalanta Bergamasca Calcio",
    "Fiorentina", "ACF Fiorentina",
    "Juventus", "Juventus FC", "Juve",
    "RB Leipzig", "RasenBallsport Leipzig", "Red Bull Leipzig",
    "Napoli", "SSC Napoli",
    "Lazio", "SS Lazio",
    "Sevilla FC", "Sevilla",
    "Villarreal CF", "Villarreal",
    "Ajax", "AFC Ajax",
    "Sporting CP", "Sporting", "Sporting Clube de Portugal",
    "Porto", "FC Porto",
]

TRANSFER_FALLBACK_CLUBS = [
    *PREMIUM_ALLOWED_CLUBS,
    "Club Brugge", "Club Brugge KV",
    "Shakhtar Donetsk", "FC Shakhtar Donetsk", "Shakhtar",
    "PSV Eindhoven", "PSV", "PSV Eindhoven",
    "Olympique Lyonnais", "Lyon", "OL",
    "Marseille", "Olympique de Marseille", "OM",
    "Real Sociedad", "Real Sociedad de Futbol",
    "AS Monaco", "Monaco",
    "Rangers FC", "Rangers",
    "Celtic FC", "Celtic",
    "Sparta Prague", "AC Sparta Praha", "Sparta Praha",
    "Dinamo Zagreb", "GNK Dinamo Zagreb",
    "Red Star Belgrade", "FK Crvena Zvezda", "Crvena Zvezda", "Red Star",
    "Basel", "FC Basel",
    "Young Boys", "BSC Young Boys",
    "Lille OSC", "Lille",
    "Wolfsburg", "VfL Wolfsburg",
    "Brighton & Hove Albion", "Brighton", "Brighton and Hove Albion",
    "Real Betis", "Real Betis Balompie", "Betis",
]

COMMON_TURKISH_NAME_TOKENS = {
    "ahmet", "ali", "arda", "berk", "berkay", "bugra", "burak", "can", "cem",
    "deniz", "emir", "emre", "enes", "eren", "furkan", "hakan", "halil", "ibrahim",
    "ismail", "kaan", "kerem", "mert", "mehmet", "mustafa", "oguz", "omer", "orhun",
    "salih", "serdar", "tolga", "ugur", "umut", "yasin", "yunus",
}

COMMON_TURKISH_SURNAME_TOKENS = {
    "arslan", "aslan", "aydin", "cakir", "celik", "demir", "demirci", "dogan",
    "guler", "kara", "kaplan", "kaya", "kilic", "koç", "koc", "ozcan", "ozdemir",
    "sahin", "tekin", "yildirim", "yilmaz",
}


# === GET SEEN PLAYERS TOOL ===

def get_seen_players_from_history(history) -> set[str]:
    """
    Scan ASSISTANT messages in history and collect player names from either:
    - [[PLAYER_PROFILE:<Name>]] tags
    - persisted [[PAYLOAD_JSON]] blocks
    Returns a normalized set of names.
    """
    seen = set()

    def norm(s: str) -> str:
        return (s or "").strip()

    for msg in history:
        role = getattr(msg, "type", "") or getattr(msg, "role", "")
        if "ai" in role or role == "assistant":
            content = getattr(msg, "content", "") or ""

            for m in PLAYER_PROFILE_OPEN_TAG_RE.finditer(content):
                name = norm(m.group(1))
                if name:
                    seen.add(name)

            for m in PAYLOAD_JSON_BLOCK_RE.finditer(content):
                raw_json = (m.group("body") or "").strip()
                if not raw_json:
                    continue
                try:
                    payload = json.loads(raw_json)
                except Exception:
                    continue

                for player in (payload.get("players") or []):
                    if not isinstance(player, dict):
                        continue
                    name = norm(player.get("name") or "")
                    if name:
                        seen.add(name)
    return seen

# === STRIP HEAVY HTML TOOL ===

def strip_heavy_html(text: str) -> str:
    """Remove <img> (esp. base64) and <table> blocks before sending to LLMs."""
    return HEAVY_TAGS_RE.sub("", text or "").strip()

# ------------------------------------------------------------------------------
# Plotting tool (per-player normalized bar charts for mixed scales)
# ------------------------------------------------------------------------------
def infer_limits(metric: str, value: float) -> Tuple[float, float]:
    def _nice_ceiling(x: float) -> float:
        """Round x up to a 'nice' number using 1/2/5 * 10^k steps."""
        if x <= 0:
            return 1.0
        exp = math.floor(math.log10(x))
        base = x / (10 ** exp)
        for m in (1, 2, 5, 10):
            if base <= m:
                return m * (10 ** exp)
        return 10 ** (exp + 1)
    
    m = (metric or "").lower()
    if "%" in metric or "percent" in m:
        return 0.0, 100.0
    if any(tok in m for tok in [
        "per game", "per 90", "goals", "assists", "tackles",
        "interceptions", "clearances", "key passes", "dribbles",
        "shots", "duels", "pressures", "carries", "passes"
    ]):
        upper = _nice_ceiling(max(value * 1.5, 1.0))
        return 0.0, upper
    if "xg" in m or "xa" in m:
        upper = max(1.0, _nice_ceiling(value * 1.5))
        return 0.0, upper
    return 0.0, _nice_ceiling(max(value * 1.2, 1.0))

# === PARSE STATISTICAL HIGHLIGHTS TOOL ===
def parse_statistical_highlights(stats_parser_chain, report_text: str) -> Dict[str, Any]:
    """
    Uses LLM to extract Statistical Highlights into a JSON payload.
    Falls back to a robust heuristic if the LLM returns invalid JSON.
    Handles lines like:
      - Jude Bellingham: 89% pass completion rate, 3.2 tackles per game, 0.5 goals per game.
      - Josko Gvardiol: 2.1 interceptions per game, 3.4 clearances per game, 75% aerial duels won.
    """
    safe = strip_heavy_html(report_text)
    try:
        raw = stats_parser_chain.invoke({"report_text": safe})
    except Exception as e:
        return {"players": []}
    
    if raw is None:
        return {"players": []}
    
    def safe_json_load(s: str) -> Dict[str, Any]:
        try:
            return json.loads(s)
        except Exception:
            return {}

    data = safe_json_load(raw)
    # If LLM JSON is good, normalize and return
    if isinstance(data, dict) and isinstance(data.get("players"), list):
        # Normalize numeric values if they come as strings
        norm_players = []
        for p in data["players"]:
            name = (p or {}).get("name") or "Player"
            stats_in = (p or {}).get("stats") or []
            stats_out = []
            for s in stats_in:
                metric = (s or {}).get("metric")
                val = (s or {}).get("value")
                if metric is None or val is None:
                    continue
                try:
                    val = float(val)
                    stats_out.append({"metric": str(metric), "value": val})
                except Exception:
                    # ignore non-numeric after all
                    pass
            if stats_out:
                norm_players.append({"name": name, "stats": stats_out})
        return {"players": norm_players}
    
    return {"players": []}

# === FILTER SEEN PLAYERS TOOL ===

def filter_players_by_seen(meta: Dict[str, Any], seen_names: set[str]):
    """
    Keep only players NOT already in 'seen_names'.
    Returns (filtered_meta, filtered_stats, new_player_names_set).
    """
    def norm(n: str) -> str:
        return (n or "").strip()

    seen_norm = {norm(n) for n in (seen_names or set())}

    meta_players = meta.get("players") or []

    # All names in current answer
    current_names = {norm(p.get("name") or "") for p in meta_players if p.get("name")}

    new_names = {n for n in current_names if n and n not in seen_norm}

    filt_meta = {"players": [p for p in meta_players if norm(p.get("name") or "") in new_names]}

    return filt_meta, new_names

# === STRIP META STATS TEXT TOOL ===

def strip_meta_stats_text(text: str, known_names: list[str] | None = None) -> str:
    """
    Remove in this order:
      (A) Any flagged blocks emitted by the LLM:
          [[PLAYER_PROFILE:<Name>]] ... [[/PLAYER_PROFILE]]
          [[PLAYER_STATS:<Name>]]   ... [[/PLAYER_STATS]]
      (B) Legacy/meta bullets and standalone 'Performance Statistics' blocks.
    Keep the remaining narrative (interpretation) text.
    """
    if not text:
        return text

    lines = text.splitlines()
    out: list[str] = []

    # ---- (A) First pass: drop flagged blocks entirely ----
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if FLAG_BLOCK_START_RE.match(line):
            # Skip until matching END (or EOF)
            i += 1
            while i < n and not FLAG_BLOCK_END_RE.match(lines[i]):
                i += 1
            if i < n and FLAG_BLOCK_END_RE.match(lines[i]):
                i += 1  # also skip the END line
            continue  # continue outer loop
        out.append(line)
        i += 1

    # Work on the remainder for legacy cleanups
    lines = out
    out = []
    i, n = 0, len(lines)

    def looks_like_name_or_analysis_header(s: str) -> str | None:
        m = PLAYER_ANALYSIS_HEADER_RE.fullmatch(s or "")
        if m:
            return (m.group("name") or "").strip()
        s_strip = (s or "").strip()
        if s_strip and s_strip == s_strip.title() and len(s_strip.split()) >= 2:
            return s_strip
        return None

    while i < n:
        line = lines[i]

        # Drop a name/analysis header if followed by meta bullets or stats block
        nm = looks_like_name_or_analysis_header(line)
        if nm:
            j = i + 1
            saw_meta = False
            while j < n and (lines[j].strip() == "" or META_LINE_RE.match(lines[j])):
                if META_LINE_RE.match(lines[j]):
                    saw_meta = True
                j += 1
            if saw_meta:
                i += 1
                continue  # skip the header line itself

        # Remove meta bullet lines
        if META_LINE_RE.match(line):
            i += 1
            continue

        out.append(line)
        i += 1

    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return cleaned


# ======== Answer Question Helpers =========
def normalize_name(s: str) -> str:
    return (s or "").strip().lower()

def _fold_text(s: str) -> str:
    text = unicodedata.normalize("NFKD", s or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().strip()

def normalize_club_name(s: str) -> str:
    text = _fold_text(s or "")
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(
        r"\b(a\.?s\.?|as|sk|sc|fc|cf|ac|afc|jk|fk|club|kulubu|kulubu|spor kulubu|sports club)\b",
        " ",
        text,
    )
    text = re.sub(
        r"\b(u\d{2}|u\d{1,2}|under\s*\d{1,2}|b\s*team|reserves?|reserve|academy|ii|2nd team|second team|youth)\b",
        " ",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text

def is_same_club(club_a: Optional[str], club_b: Optional[str]) -> bool:
    a = normalize_club_name(club_a or "")
    b = normalize_club_name(club_b or "")
    if not a or not b:
        return False
    if a == b:
        return True
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if a_tokens and b_tokens and a_tokens == b_tokens:
        return True
    if a in b or b in a:
        return True
    return False


def normalize_search_text(value: Optional[str]) -> str:
    text = unicodedata.normalize("NFKD", (value or "").strip()).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_turkish_nationality(value: Optional[str]) -> bool:
    text = normalize_search_text(value)
    return text in {"turkey", "turkish", "turkiye"}


def is_disallowed_turkish_club(team: Optional[str]) -> bool:
    candidate = (team or "").strip()
    if not candidate:
        return False
    return any(is_same_club(candidate, club_name) for club_name in DISALLOWED_TURKISH_CLUBS)


def is_likely_turkish_name(name: Optional[str]) -> bool:
    raw = (name or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if any(ch in lowered for ch in "çğıöşü"):
        return True

    folded = normalize_search_text(raw)
    tokens = [tok for tok in re.split(r"[^a-z]+", folded) if tok]
    if not tokens:
        return False

    score = 0
    for tok in tokens:
        if tok in COMMON_TURKISH_NAME_TOKENS:
            score += 1
        if tok in COMMON_TURKISH_SURNAME_TOKENS:
            score += 1
        if tok.endswith("oglu") or tok.endswith("gil") or tok.endswith("tas") or tok.endswith("turk"):
            score += 1

    return score >= 2


def is_non_senior_team(team: Optional[str]) -> bool:
    text = normalize_search_text(team)
    if not text:
        return False
    return bool(re.search(r"\b(u\d{1,2}|under\s*\d{1,2}|b\s*team|reserves?|reserve|academy|ii|2nd team|second team|youth|juvenil)\b", text))


def request_allows_turkish_entities(question: Optional[str]) -> bool:
    text = normalize_search_text(question)
    if not text:
        return False

    explicit_patterns = [
        r"\bturk(?:ish|iye)?\b",
        r"\bfrom turkey\b",
        r"\bfrom turkish league\b",
        r"\bfrom super lig\b",
        r"\bfrom tff 1 lig\b",
    ]
    if any(re.search(pattern, text) for pattern in explicit_patterns):
        return True
    return False


def request_allows_non_senior_squads(question: Optional[str]) -> bool:
    text = normalize_search_text(question)
    if not text:
        return False
    return bool(re.search(r"\b(youth|academy|reserve|reserves|b team|u\d{1,2}|under\s*\d{1,2}|second team|ii team|juvenil)\b", text))


def is_generic_alternative_request(question: Optional[str]) -> bool:
    text = normalize_search_text(question)
    if not text:
        return False
    patterns = [
        r"\bsuggest another player\b",
        r"\bsuggest another\b",
        r"\banother player\b",
        r"\banother\b",
        r"\banother option\b",
        r"\bsomeone else\b",
        r"\bdifferent player\b",
        r"\bdifferent\b",
        r"\bnew player\b",
        r"\bother player\b",
        r"\bother\b",
        r"\bnext player\b",
        r"\bnext\b",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def is_premium_request(question: Optional[str]) -> bool:
    text = normalize_search_text(question)
    if not text:
        return False
    premium_patterns = [
        r"\btop class\b",
        r"\belite\b",
        r"\bworld class\b",
        r"\bvery good\b",
        r"\bhigh budget\b",
        r"\bbig budget\b",
        r"\bmoney is not an issue\b",
        r"\bunlimited budget\b",
    ]
    return any(re.search(pattern, text) for pattern in premium_patterns)


def is_premium_allowed_club(team: Optional[str]) -> bool:
    candidate = (team or "").strip()
    if not candidate:
        return False
    return any(is_same_club(candidate, club_name) for club_name in PREMIUM_ALLOWED_CLUBS)


def is_transfer_fallback_club(team: Optional[str]) -> bool:
    candidate = (team or "").strip()
    if not candidate:
        return False
    return any(is_same_club(candidate, club_name) for club_name in TRANSFER_FALLBACK_CLUBS)


def get_candidate_rejection_reason(
    player_name: Optional[str],
    team_name: Optional[str],
    nationality: Optional[str],
    *,
    target_team: Optional[str] = None,
    allow_turkish: bool = False,
    allow_non_senior: bool = False,
    premium_only: bool = False,
) -> Optional[str]:
    if target_team and is_same_club(target_team, team_name):
        return "same target club"
    if not allow_non_senior and is_non_senior_team(team_name):
        return "non-senior squad"
    if premium_only and not is_premium_allowed_club(team_name):
        return "premium club restriction"
    if not allow_turkish and (
        is_disallowed_turkish_club(team_name)
        or is_turkish_nationality(nationality)
        or is_likely_turkish_name(player_name)
    ):
        return "Turkish exclusion"
    return None


def _canonical_role_group(role: Optional[str]) -> Optional[str]:
    text = normalize_search_text(role)
    if not text:
        return None
    if "goalkeeper" in text or text == "goal keeper":
        return "goalkeeper"
    if "left wing back" in text:
        return "left_wing_back"
    if "right wing back" in text:
        return "right_wing_back"
    if "left back" in text:
        return "left_back"
    if "right back" in text:
        return "right_back"
    if "left center back" in text or "left centre back" in text:
        return "center_back"
    if "right center back" in text or "right centre back" in text:
        return "center_back"
    if "center back" in text or "centre back" in text:
        return "center_back"
    if "defensive midfield" in text or "center defensive midfield" in text or "central defensive midfield" in text:
        return "defensive_midfield"
    if "attacking midfield" in text or "center attacking midfield" in text or "central attacking midfield" in text:
        return "attacking_midfield"
    if "central midfield" in text or "center midfield" in text or "left center midfield" in text or "right center midfield" in text:
        return "central_midfield"
    if "left wing" in text:
        return "left_wing"
    if "right wing" in text:
        return "right_wing"
    if "center forward" in text or "centre forward" in text or "attacker" in text or "left center forward" in text or "right center forward" in text:
        return "center_forward"
    return None


def rewrite_position_reference_phrases(question: Optional[str]) -> str:
    text = (question or "").strip()
    if not text:
        return ""

    pattern_replacements = [
        ([r"\b(?:no|number)\s*1\b", r"\b1\s*numara\b", r"\bone\s*numara\b"], "goalkeeper"),
        ([r"\b(?:no|number)\s*2\b", r"\b2\s*numara\b", r"\btwo\s*numara\b"], "right back"),
        ([r"\b(?:no|number)\s*3\b", r"\b3\s*numara\b", r"\bthree\s*numara\b"], "left back"),
        ([r"\b(?:no|number)\s*4\b", r"\b4\s*numara\b", r"\bfour\s*numara\b", r"\bstoper\b", r"\bstopper\b"], "center back"),
        ([r"\b(?:no|number)\s*6\b", r"\b6\s*numara\b", r"\bsix\s*numara\b"], "defensive midfielder"),
        ([r"\b(?:no|number)\s*7\b", r"\b7\s*numara\b", r"\bseven\s*numara\b"], "right winger"),
        ([r"\b(?:no|number)\s*8\b", r"\b8\s*numara\b", r"\beight\s*numara\b"], "central midfielder"),
        ([r"\b(?:no|number)\s*9\b", r"\b9\s*numara\b", r"\bnine\s*numara\b"], "striker"),
        ([r"\b(?:no|number)\s*10\b", r"\b10\s*numara\b", r"\bten\s*numara\b"], "attacking midfielder"),
        ([r"\b(?:no|number)\s*11\b", r"\b11\s*numara\b", r"\beleven\s*numara\b"], "left winger"),
    ]

    rewritten = text
    for patterns, replacement in pattern_replacements:
        for pattern in patterns:
            rewritten = re.sub(pattern, replacement, rewritten, flags=re.IGNORECASE)

    return re.sub(r"\s+", " ", rewritten).strip()


def get_requested_position_groups(question: Optional[str]) -> Optional[set[str]]:
    text = normalize_search_text(rewrite_position_reference_phrases(question))
    if not text:
        return None

    pattern_groups = [
        ([r"\bno 1\b", r"\bnumber 1\b", r"\b1 numara\b", r"\bone numara\b", r"\bgk\b", r"\bgoal keeper\b", r"\bgoalkeeper\b"], {"goalkeeper"}),
        ([r"\bno 2\b", r"\bnumber 2\b", r"\b2 numara\b", r"\btwo numara\b", r"\bright back\b", r"\brb\b"], {"right_back", "right_wing_back"}),
        ([r"\bno 3\b", r"\bnumber 3\b", r"\b3 numara\b", r"\bthree numara\b", r"\bleft back\b", r"\blb\b"], {"left_back", "left_wing_back"}),
        ([r"\bno 4\b", r"\bnumber 4\b", r"\b4 numara\b", r"\bfour numara\b", r"\bcb\b", r"\bcenter back\b", r"\bcentre back\b", r"\bstopper\b"], {"center_back"}),
        ([r"\bno 6\b", r"\bnumber 6\b", r"\b6 numara\b", r"\bsix numara\b", r"\bcdm\b", r"\bdefensive midfielder\b", r"\bdefensive midfield\b"], {"defensive_midfield"}),
        ([r"\bno 7\b", r"\bnumber 7\b", r"\b7 numara\b", r"\bseven numara\b", r"\bright winger\b", r"\bright wing\b", r"\bright midfield\b", r"\bright midfielder\b", r"\brm\b"], {"right_wing", "right_midfield"}),
        ([r"\bno 8\b", r"\bnumber 8\b", r"\b8 numara\b", r"\beight numara\b", r"\bcm\b", r"\bcentral midfielder\b", r"\bcentral midfield\b"], {"central_midfield"}),
        ([r"\bno 9\b", r"\bnumber 9\b", r"\b9 numara\b", r"\bnine numara\b", r"\b9\b", r"\bstriker\b", r"\bcenter forward\b", r"\bcentre forward\b", r"\battacker\b", r"\bsantrafor\b", r"\bst\b", r"\bcf\b"], {"center_forward"}),
        ([r"\bno 10\b", r"\bnumber 10\b", r"\b10 numara\b", r"\bten numara\b", r"\bcam\b", r"\battacking midfielder\b", r"\battacking midfield\b"], {"attacking_midfield"}),
        ([r"\bno 11\b", r"\bnumber 11\b", r"\b11 numara\b", r"\beleven numara\b", r"\bleft winger\b", r"\bleft wing\b", r"\bleft midfield\b", r"\bleft midfielder\b", r"\blm\b"], {"left_wing", "left_midfield"}),
        ([r"\bright wing back\b", r"\brwb\b"], {"right_wing_back", "right_back"}),
        ([r"\bleft wing back\b", r"\blwb\b"], {"left_wing_back", "left_back"}),
        ([r"\bwing back\b", r"\bwingback\b"], {"left_wing_back", "right_wing_back", "left_back", "right_back"}),
        ([r"\bfull back\b", r"\bfullback\b"], {"left_back", "right_back", "left_wing_back", "right_wing_back"}),
        ([r"\bright back\b", r"\brb\b"], {"right_back", "right_wing_back"}),
        ([r"\bleft back\b", r"\blb\b"], {"left_back", "left_wing_back"}),
        ([r"\bcenter back\b", r"\bcentre back\b", r"\bcenter half\b", r"\bcentre half\b", r"\bcb\b"], {"center_back"}),
        ([r"\bright winger\b"], {"right_wing"}),
        ([r"\bleft winger\b"], {"left_wing"}),
        ([r"\bwinger\b", r"\bwing\b"], {"left_wing", "right_wing"}),
        ([r"\bcenter forward\b", r"\bcentre forward\b", r"\bcenterforward\b", r"\bcentreforward\b", r"\bstriker\b", r"\bforward\b", r"\bsantrafor\b", r"\bst\b", r"\bcf\b"], {"center_forward"}),
        ([r"\bmidfielder\b", r"\bmidfield\b"], {"defensive_midfield", "central_midfield", "attacking_midfield"}),
    ]
    for patterns, groups in pattern_groups:
        if any(re.search(pattern, text) for pattern in patterns):
            return groups
    return None


def player_matches_requested_position(
    question: Optional[str],
    position_name: Optional[str],
    roles: Optional[Iterable[str]] = None,
) -> Tuple[bool, Optional[set[str]], set[str]]:
    requested_groups = get_requested_position_groups(question)
    if not requested_groups:
        return True, None, set()

    player_groups: set[str] = set()
    for role in [position_name, *(roles or [])]:
        group = _canonical_role_group(role)
        if group:
            player_groups.add(group)

    if not player_groups:
        return False, requested_groups, set()

    return bool(player_groups & requested_groups), requested_groups, player_groups



def extract_target_team_from_question(question: Optional[str]) -> Optional[str]:
    text = (question or "").strip()
    if not text:
        return None

    patterns = [
        r"\b([\w .&'’\-]+?)\s+i[cç]in\b",
        r"\b(?:for|to)\s+([\w .&'’\-]+?)(?=$|\s+(?:a|an|the|need|needs|looking|searching|want|wants|with|who)\b|[,.!?])",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        team = re.sub(r"\s+", " ", (m.group(1) or "").strip(" .,!?:;\"'"))
        if team:
            return team
    return None


def strip_target_team_from_question(question: Optional[str], target_team: Optional[str]) -> str:
    text = (question or "").strip()
    if not text:
        return ""
    team = (target_team or "").strip()
    if not team:
        return text

    patterns = [
        re.compile(rf"\bfor\s+{re.escape(team)}\b", re.IGNORECASE),
        re.compile(rf"\bto\s+{re.escape(team)}\b", re.IGNORECASE),
        re.compile(rf"\b{re.escape(team)}\s+i[cç]in\b", re.IGNORECASE),
    ]
    stripped = text
    for pattern in patterns:
        stripped = pattern.sub(" ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip(" .,!?:;\"'")
    return stripped or text

def compose_selection_preamble(
    seen_players: Iterable[str],
    strategy: str | None,
) -> str:
    """
    Returns a preface instructing the LLM how to behave wrt:
      - one player per response,
      - seen players (no reprint of blocks/plots),
      - new player requests (intent-based, no keywords),
      - collective references to 'others' (ask user to pick one of seen),
      - candidate lists (choose exactly one).
    This contains NO static keyword checks. It asks the LLM to infer intent from semantics.
    """
    seen_list = ", ".join(seen_players) if seen_players else "None"
    strat = (strategy or "").strip()

    strategy_block = (
        f"User strategy preference (use this to shape the analysis and selections): {strat}\n\n"
        if strat else ""
    )

    # Intent rules are semantic: the model decides from user message meaning.
    selection_rules = (
        "Selection rules:\n"
        "- One player per response.\n"
        f"- Seen players in this chat (do not reprint their blocks): {seen_list}\n"
        "- Intention resolution (semantic, not keyword-based):\n"
        "  • If the user clearly refers to one of the seen players by name, do NOT print any blocks; refer back to earlier blocks and add new narrative only.\n"
        "  • If the user indicates they want a different option (in any wording), select a NEW unseen player (not in the seen set) and print their blocks.\n"
        "  • If the user refers collectively to previously discussed players (e.g., talks about 'others' discussed earlier without naming one), do NOT introduce a new player; reply with one short sentence asking them to choose ONE of the previously discussed players to analyze next (no blocks).\n"
        "  • If the user provides a candidate list, choose exactly one from that list only.\n\n"
    )

    return strategy_block + selection_rules


def summarize_doc_candidate(doc: Any) -> str:
    md = getattr(doc, "metadata", None) or {}
    page_content = getattr(doc, "page_content", "") or ""
    player_name = str(md.get("player_name") or md.get("name") or "").strip() or "Unknown"
    team_name = str(md.get("team_name") or md.get("team") or md.get("club") or "").strip() or "Unknown"
    nationality = str(md.get("nationality_name") or md.get("nationality") or md.get("country") or "").strip() or "Unknown"
    position_name = str(md.get("position_name") or md.get("position") or "").strip() or "Unknown"
    similarity = md.get("similarity")
    similarity_text = f"{similarity:.4f}" if isinstance(similarity, (int, float)) else "n/a"
    _ = page_content
    return (
        f"name='{player_name}', team='{team_name}', nationality='{nationality}', "
        f"position='{position_name}', similarity='{similarity_text}'"
    )


def build_pass2_query(
    original_query: str,
    stripped_query: str,
    target_team: Optional[str],
    *,
    allow_turkish: bool,
    allow_non_senior: bool,
    premium_only: bool,
) -> str:
    base_query = (stripped_query or original_query or "").strip()
    constraints: List[str] = []
    if target_team:
        constraints.append(
            f"The player must be a realistic transfer target for {target_team} and must already belong to a different club."
        )
    if not allow_turkish:
        constraints.append(
            "Exclude Turkish players, players from Turkish clubs, and clearly Turkish-looking player names."
        )
    if not allow_non_senior:
        constraints.append(
            "Exclude youth teams, reserve teams, academy teams, and B teams; prefer senior first-team players only."
        )
    if premium_only:
        constraints.append(
            "Keep premium-only quality constraints and restrict candidates to the approved premium club set."
        )
    constraints.append(
        "Prefer realistic first-team players with a clear role match, not random low-signal or unknown-position candidates."
    )
    return base_query + "\n" + " ".join(constraints)


def build_pass3_query(
    original_query: str,
    stripped_query: str,
    target_team: Optional[str],
    *,
    allow_turkish: bool,
    allow_non_senior: bool,
) -> str:
    base_query = (stripped_query or original_query or "").strip()
    clubs_text = ", ".join(TRANSFER_FALLBACK_CLUBS)
    constraints: List[str] = []
    if target_team:
        constraints.append(
            f"The player must be a realistic transfer target for {target_team} and must already belong to a different club."
        )
    constraints.append(
        f"If the normal search has no valid answer, search only among players from these clubs: {clubs_text}."
    )
    if not allow_turkish:
        constraints.append(
            "Exclude Turkish players, players from Turkish clubs, and clearly Turkish-looking player names."
        )
    if not allow_non_senior:
        constraints.append(
            "Exclude youth teams, reserve teams, academy teams, and B teams; prefer senior first-team players only."
        )
    constraints.append(
        "Keep a clear role match and prefer realistic first-team transfer targets from these clubs."
    )
    return base_query + "\n" + " ".join(constraints)


def collect_recent_human_constraints(
    history_rows: list,
    *,
    is_generic_alternative_fn,
    limit: int = 3,
) -> list[str]:
    constraints: list[str] = []
    for row in reversed(history_rows):
        if row.get("role") != "human":
            continue
        text = (row.get("content") or "").strip()
        if not text:
            continue
        if is_generic_alternative_fn(text):
            continue
        constraints.append(text)
        if len(constraints) >= limit:
            break
    constraints.reverse()
    return constraints

# ------- Language Adjustment --------
def _normalize_lang_code(code: Optional[str]) -> str:
    c = (code or "").lower().strip()
    if c.startswith("tr"):
        return "tr"
    if c.startswith("en"):
        return "en"
    return "en"  # fallback

def inject_language(base_system_message: str, lang_code: Optional[str]) -> str:
    """
    Make the language constraint dominate and be hard to override by:
    - Prepending the directive (highest priority),
    - Keeping your original system message,
    - Appending the directive again (redundancy to resist drift).
    """
    lang = _normalize_lang_code(lang_code)
    directive = LANG_DIRECTIVES.get(lang, LANG_DIRECTIVES["en"])
    core = base_system_message.strip()
    return f"{directive}\n\n{core}\n\n{directive}\n"

def is_turkish(lang: Optional[str]) -> bool:
    return (lang or "").lower().startswith("tr")
