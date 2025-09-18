import re
import io
import base64
import json
import math
from typing import Dict, List, Any, Tuple
import matplotlib.pyplot as plt

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
    )\*\*:\s*.+$""",
    re.IGNORECASE | re.VERBOSE,
)
STATS_HEADER_RE = re.compile(r"^\s*\*\*Performance\s+Statistics\*\*\s*:?\s*$", re.IGNORECASE)
STATS_ITEM_RE = re.compile(
    r"^\s*(?:\d+\.\s+|\-\s+\*\*[^*]+?\*\*:\s+).+?$",
    re.IGNORECASE,
)

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
    raw = stats_parser_chain.predict(report_text=safe).strip()
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

# === PARSE PLAYER META DATA TOOL ===
def parse_player_meta(meta_parser_chain, raw_text: str) -> Dict[str, Any]:      
    # Always guard the parser so it cannot crash the request
    safe = strip_heavy_html(raw_text) 
    try:
        raw = meta_parser_chain.run(raw_text=safe).strip()
    except Exception:
        return {"players": []}
    try:
        data = json.loads(raw)
    except Exception:
        return {"players": []}
    
    # normalize
    out = []
    for p in (data.get("players") or []):
        name = (p or {}).get("name")
        if not name:
            continue
        nat = (p or {}).get("nationality")
        age = (p or {}).get("age")
        roles = (p or {}).get("roles") or []
        if not isinstance(roles, list):
            roles = [str(roles)]
        out.append({
            "name": str(name),
            "nationality": str(nat) if nat is not None else None,
            "age": int(age) if isinstance(age, (int, float)) else None,
            "roles": [str(r) for r in roles if r is not None],
        })
    return {"players": out}

# === FILTER SEEN PLAYERS TOOL ===

def filter_players_by_seen(meta: Dict[str, Any], stats: Dict[str, Any], seen_names: set[str]):
    """
    Keep only players NOT already in 'seen_names'.
    Returns (filtered_meta, filtered_stats, new_player_names_set).
    """
    def norm(n: str) -> str:
        return (n or "").strip()

    seen_norm = {norm(n) for n in (seen_names or set())}

    meta_players = meta.get("players") or []
    stats_players = stats.get("players") or []

    # All names in current answer
    current_names = {norm(p.get("name") or "") for p in meta_players if p.get("name")}
    current_names.update({norm(p.get("name") or "") for p in stats_players if p.get("name")})

    new_names = {n for n in current_names if n and n not in seen_norm}

    filt_meta = {"players": [p for p in meta_players if norm(p.get("name") or "") in new_names]}
    filt_stats = {"players": [p for p in stats_players if norm(p.get("name") or "") in new_names]}

    return filt_meta, filt_stats, new_names


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
            saw_stats = False
            k = i + 1
            while k < n and lines[k].strip() == "":
                k += 1
            if k < n and STATS_HEADER_RE.match(lines[k]):
                saw_stats = True
            if saw_meta or saw_stats:
                i += 1
                continue  # skip the header line itself

        # Remove meta bullet lines
        if META_LINE_RE.match(line):
            i += 1
            continue

        # Remove standalone Performance Statistics block
        if STATS_HEADER_RE.match(line):
            i += 1
            while i < n:
                nxt = lines[i]
                if not nxt.strip():
                    i += 1
                    continue
                if STATS_ITEM_RE.match(nxt):
                    i += 1
                    continue
                break
            continue

        out.append(line)
        i += 1

    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return cleaned

# ------------------------------------------------------------------------------
# Plotting tool (per-player normalized bar charts for mixed scales)
# ------------------------------------------------------------------------------

def plot_player_stats(player_name: str, stats: List[Dict[str, Any]]) -> str:
    """
    Per-player 'bullet panel' with per-metric axes, returned as a data URI (no files).
    """
    def _fig_to_data_uri(fig) -> str:
        """Return data:image/png;base64,... for a Matplotlib figure."""
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode("ascii")
        buf.close()
        return f"data:image/png;base64,{b64}"
    
    if not stats:
        return ""

    metrics: List[str] = []
    values: List[float] = []
    for s in stats:
        metric = str(s.get("metric", "")).strip()
        val = s.get("value", None)
        try:
            val = float(val)
        except Exception:
            continue
        metrics.append(metric)
        values.append(val)

    if not values:
        return ""

    n = len(values)
    fig_w = max(25, n * 1.2 * 2.0)         # doubled width
    fig_h = max(18, (0.9 * n + 1.2) * 2.0) # doubled height

     # ---- Font sizes (3x baseline) ----
    FS_YTICKS = 28   # stat (metric) label on the left (baseline ~9 => 27)
    FS_XTICKS = 26   # axis tick labels (baseline ~8 => 24)
    FS_VALTXT = 28   # numeric annotation at the end of each bar (baseline ~9 => 27)
    FS_TITLE  = 60   # figure title (slightly larger for balance)

    BAR_BG_HEIGHT = 2.2
    BAR_FG_HEIGHT = 1.7

    fig, axes = plt.subplots(nrows=n, ncols=1, figsize=(fig_w, fig_h), constrained_layout=True)
    if n == 1:
        axes = [axes]

    for ax, metric, val in zip(axes, metrics, values):
        lo, hi = infer_limits(metric, val)
        ax.barh([0], [hi - lo], left=lo, height=BAR_BG_HEIGHT, alpha=0.15)
        ax.barh([0], [val - lo], left=lo, height=BAR_FG_HEIGHT)
        ax.set_yticks([0])
        ax.set_yticklabels([metric], fontsize=FS_YTICKS)
        ax.text(val, 0, f" {val:.2f}", va="center", ha="left",  fontsize=FS_VALTXT)
        ax.set_xlim(lo, hi)
        ax.grid(axis="x", linestyle=":", alpha=0.4)
        for spine in ("top", "right", "left"):
            ax.spines[spine].set_visible(False)
        ax.tick_params(axis="y", labelsize=FS_YTICKS)
        ax.tick_params(axis="x", labelsize=FS_XTICKS)

    fig.suptitle(f"{player_name} — Statistical Highlights", size=FS_TITLE, weight = "bold")
    plt.subplots_adjust(bottom=0.05)

    data_uri = _fig_to_data_uri(fig)
    plt.close(fig)
    return data_uri

def plot_stats_bundle(
    parsed: Dict[str, Any],
    # Doubled defaults for rendered width
    base_width_px: int = 1800,    # was 900
    step_per_stat_px: int = 80,   # was 40
    min_width_px: int = 1600,     # was 800
    max_width_px: int = 3500,     # was 1750
) -> Dict[str, Any]:
    """
    Returns {
      "Player Name": {
          "src": "data:image/png;base64,...",
          "width_px": int
      },
      ...
    }
    """
    out: Dict[str, Any] = {}
    for p in parsed.get("players", []):
        name = p.get("name") or "Player"
        stats = p.get("stats") or []
        n_stats = len(stats)

        uri = plot_player_stats(name, stats)
        if not uri:
            continue

        # Width grows with number of stats (clamped)
        # Interpreting base_width for ~3 stats, add step per extra stat
        width = base_width_px + max(0, n_stats - 3) * step_per_stat_px
        width = max(min_width_px, min(max_width_px, width))

        out[name] = {"src": uri, "width_px": width}
    return out

# === BUILD PLAYER TABLES TOOL ===

def table_markup(title: str, rows: List[tuple[str, str]], small: bool=False) -> str:
    size = "14px" if small else "15px"
    head_css = "font-weight:600;margin:6px 0"
    td_l = "padding:8px 10px;border:1px solid #2b2b2f;color:#c9c9cf;width:42%;vertical-align:top"
    td_r = "padding:8px 10px;border:1px solid #2b2b2f;color:#e5e5e8;vertical-align:top"
    trs = "".join(
        f"<tr><td style='{td_l}'>{k}</td><td style='{td_r}'>{v}</td></tr>"
        for k, v in rows
    )
    return (
        f"<div style='margin:8px 0 14px 0;'>"
        f"<div style='{head_css}'>{title}</div>"
        f"<table style='width:100%;border-collapse:collapse;font-size:{size};'><tbody>{trs}</tbody></table>"
        f"</div>"
    )

def build_player_tables(meta: Dict[str, Any], stats: Dict[str, Any]) -> str:

    # Index by player name
    meta_by = { (p.get("name") or "").strip(): p for p in (meta.get("players") or []) if p.get("name") }
    stats_by = { (p.get("name") or "").strip(): p for p in (stats.get("players") or []) if p.get("name") }

    names = sorted(set(meta_by.keys()) | set(stats_by.keys()), key=lambda s: s.lower())
    blocks: list[str] = []
    for name in names:
        m = meta_by.get(name, {})
        s = stats_by.get(name, {})
        # meta rows
        roles = m.get("roles") or []
        roles_text = ", ".join(roles) if isinstance(roles, list) else str(roles or "")
        meta_rows = []
        if m.get("nationality"): meta_rows.append(("Nationality", str(m["nationality"])))
        if m.get("age") is not None: meta_rows.append(("Age (as of 2025)", str(m["age"])))
        if roles_text: meta_rows.append(("Roles", roles_text))
        meta_table = table_markup(name, meta_rows) if meta_rows else ""

        # stats rows
        stat_rows = []
        for st in (s.get("stats") or []):
            metric = str(st.get("metric") or "").strip()
            val = st.get("value")
            if metric and (val is not None):
                # keep original units if you wish—here just show the number
                stat_rows.append((metric, f"{val}"))
        stats_table = table_markup("Performance Statistics", stat_rows, small=True) if stat_rows else ""

        if meta_table or stats_table:
            blocks.append(meta_table + stats_table)
    return "\n".join(blocks)


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
            saw_stats = False
            k = i + 1
            while k < n and lines[k].strip() == "":
                k += 1
            if k < n and STATS_HEADER_RE.match(lines[k]):
                saw_stats = True
            if saw_meta or saw_stats:
                i += 1
                continue  # skip the header line itself

        # Remove meta bullet lines
        if META_LINE_RE.match(line):
            i += 1
            continue

        # Remove standalone Performance Statistics block
        if STATS_HEADER_RE.match(line):
            i += 1
            while i < n:
                nxt = lines[i]
                if not nxt.strip():
                    i += 1
                    continue
                if STATS_ITEM_RE.match(nxt):
                    i += 1
                    continue
                break
            continue

        out.append(line)
        i += 1

    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return cleaned

# === CONTENT TO SEND TO FRONTEND RAW DATA ===
# tools.py (add near your existing builders)

def build_player_payload(meta: Dict[str, Any], stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge the parsed meta and stats into a frontend-friendly JSON structure.

    Input shapes (already produced by your parsers):
      meta  = {"players": [{"name", "nationality", "age", "roles": [...]}, ...]}
      stats = {"players": [{"name", "stats": [{"metric", "value"}, ...]}, ...]}

    Returns:
      {"players": [{"name", "meta": {...}, "stats": [...]}, ...]}
    """
    meta_by = { (p.get("name") or "").strip(): p
                for p in (meta.get("players") or []) if p.get("name") }
    stats_by = { (p.get("name") or "").strip(): p
                 for p in (stats.get("players") or []) if p.get("name") }

    names = sorted(set(meta_by.keys()) | set(stats_by.keys()), key=lambda s: s.lower())

    out: List[Dict[str, Any]] = []
    for name in names:
        m = meta_by.get(name, {})
        s = stats_by.get(name, {})
        out.append({
            "name": name,
            "meta": {
                "nationality": m.get("nationality"),
                "age": m.get("age"),
                "roles": (m.get("roles") or []),
            },
            "stats": (s.get("stats") or []),
        })
    return {"players": out}