import re
import json
import math
from typing import Dict, Any, Tuple, Iterable, Optional
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


# === GET SEEN PLAYERS TOOL ===

def get_seen_players_from_history(history) -> set[str]:
    """
    Scan ASSISTANT messages in history and collect any names that appear in
    [[PLAYER_PROFILE:<Name>]] tags. Returns a normalized set of names.
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

    print(safe)
    data = safe_json_load(raw)
    print(data)
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

def build_recent_context(history_rows: list, max_chars: int = 3000) -> str:
    # last N messages is usually better than last N chars, but this works
    parts = []
    for row in reversed(history_rows):
        role = row.get("role")
        txt = (row.get("content") or "").strip()
        if not txt:
            continue
        # include both human+ai for disambiguation
        parts.append(f"{role.upper()}: {txt}")
        if sum(len(p) for p in parts) > max_chars:
            break
    return "\n".join(reversed(parts))

