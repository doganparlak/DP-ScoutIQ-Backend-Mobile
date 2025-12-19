from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import text
import re
import json
from report_module.utilities import _num, _norm
from api_module.utilities import get_db 

META_ID_KEYS = {
    # identity / grouping
    "player_key", "player_name", "name", "player",
    "team_name", "team", "club",
    "nationality_name", "nationality", "country",
    "gender", "position_name",

    # demographics
    "age", "height", "weight", "match_count",

    # storage/other (if present)
    "id", "content", "metadata", "vector",
}

PROFILE_BLOCK_RE = re.compile(
    r"""
    \[\[\s*PLAYER_PROFILE\s*:\s*(?P<name>[^\]]+)\s*\]\]
    (?P<body>[\s\S]*?)
    (?:
        \[\[\/PLAYER_PROFILE\]\]                             # correct close
        |
        \[\[\s*\/?PLAYER_STATS\s*:\s*[^\]]+\]\]              # handles [[PLAYER_STATS:..]] AND [[/PLAYER_STATS:..]]
    )
    """,
    re.IGNORECASE | re.VERBOSE
)

HEAVY_TAGS_RE = re.compile(r"(<img[^>]*>|<table[\s\S]*?</table>)", re.IGNORECASE)
def strip_heavy_html(text: str) -> str:
    """Remove <img> (esp. base64) and <table> blocks before sending to LLMs."""
    return HEAVY_TAGS_RE.sub("", text or "").strip()

def fallback_parse_profile_block_new(raw_text: str) -> Dict[str, Any]:
    """
    Extended fallback parser capturing gender, height, weight, team.
    """
    m = PROFILE_BLOCK_RE.search(raw_text or "")
    if not m:
        return {"players": []}

    name = (m.group("name") or "").strip()
    body = m.group("body") or ""

    gender = None
    height = None
    weight = None
    age = None
    nationality = None
    team = None
    roles = []
    potential = None
    match_count = None

    for line in body.splitlines():
        ln = line.strip()
        if ln.lower().startswith("- gender:"):
            gender = ln.split(":", 1)[1].strip()
        elif ln.lower().startswith("- height"):
            try: height = float(ln.split(":", 1)[1])
            except: pass
        elif ln.lower().startswith("- weight"):
            try: weight = float(ln.split(":", 1)[1])
            except: pass
        elif ln.lower().startswith("- age"):
            try: age = int(ln.split(":", 1)[1])
            except: pass
        elif ln.lower().startswith("- nationality"):
            nationality = ln.split(":", 1)[1].strip()
        elif ln.lower().startswith("- team"):
            team = ln.split(":", 1)[1].strip()
        elif ln.lower().startswith("- roles"):
            role_raw = ln.split(":", 1)[1]
            roles = [r.strip() for r in role_raw.split(",") if r.strip()]
        elif ln.lower().startswith("- potential"):
            try: potential = int(ln.split(":", 1)[1])
            except: pass
        elif ln.lower().startswith("- match_count"):
            try: match_count = int(ln.split(":", 1)[1])
            except: pass

    return {
        "players": [
            {
                "name": name,
                "gender": gender,
                "height": height,
                "weight": weight,
                "age": age,
                "nationality": nationality,
                "team": team,
                "match_count": match_count,
                "roles": roles,
                "potential": potential,
            }
        ]
    }

def parse_player_meta_new(meta_parser_chain, raw_text: str) -> Dict[str, Any]:
    """
    Extended meta parser supporting gender, height, weight, team, match_count.
    Does NOT modify prompts — only adapts Python processing to match new schema.
    """
    safe = strip_heavy_html(raw_text)

    # Step 1 — LLM JSON
    data = {}
    try:
        raw = meta_parser_chain.invoke({"raw_text": safe})
        data = raw if isinstance(raw, dict) else json.loads(raw)
    except:
        data = {}

    players_out = []

    for p in (data.get("players") or []):
        if not p:
            continue

        out = {
            "name": p.get("name"),
            "gender": p.get("gender"),
            "height": p.get("height"),
            "weight": p.get("weight"),
            "age": p.get("age"),
            "nationality": p.get("nationality"),
            "team": p.get("team"),
            "match_count": p.get("match_count"),
            "roles": p.get("roles") or [],
            "potential": None
        }

        # normalize potential
        pot = p.get("potential")
        if pot is not None:
            try:
                pot = int(float(pot))
                pot = max(0, min(100, pot))
            except:
                pot = None
        out["potential"] = pot

        # ensure roles always => list
        if not isinstance(out["roles"], list):
            out["roles"] = [str(out["roles"])]

        players_out.append(out)

    # fallback if necessary
    if not players_out:
        fb = fallback_parse_profile_block_new(safe)
        return fb

    return {"players": players_out}

def _extract_stats_from_doc_meta(doc_meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Your schema: stats are numeric fields directly in metadata.
    Convert them into list-of-dicts: [{"metric": k, "value": v}, ...]
    """
    out: List[Dict[str, Any]] = []
    for k, v in (doc_meta or {}).items():
        if k in META_ID_KEYS:
            continue
        nv = _num(v)
        if nv is None:
            continue
        out.append({"metric": str(k), "value": nv})
    return out


def _is_non_zero_stat(stat: Dict[str, Any]) -> bool:
    v = _num(stat.get("value"))
    return (v is not None) and (abs(v) > 0.05)

def _score_candidate(meta: Dict[str, Any], ident: Dict[str, Any]) -> float:
    score = 0.0

    name_i = _norm(ident.get("name"))
    nat_i  = _norm(ident.get("nationality"))
    gen_i  = _norm(ident.get("gender"))

    name_m = _norm(meta.get("player_name") or meta.get("name") or meta.get("player"))
    nat_m  = _norm(meta.get("nationality_name") or meta.get("nationality") or meta.get("country"))
    gen_m  = _norm(meta.get("gender"))

    # name is most important
    if name_i and name_m:
        if name_i == name_m: score += 10
        elif name_i in name_m or name_m in name_i: score += 7

    if nat_i and nat_m:
        if nat_i == nat_m: score += 4
        elif nat_i in nat_m or nat_m in nat_i: score += 2

    if gen_i and gen_m and gen_i == gen_m:
        score += 2

    # numeric closeness (don’t punish missing)
    for k, w, tol in [("age", 2.5, 2.0), ("height", 2.0, 4.0), ("weight", 2.0, 5.0)]:
        iv = _num(ident.get(k))
        mv = _num(meta.get(k) or meta.get(f"{k}_cm") or meta.get(f"{k}_kg"))
        if iv is None or mv is None:
            continue
        diff = abs(iv - mv)
        if diff <= tol:
            score += w
        elif diff <= tol * 2:
            score += w * 0.5

    return score


def fetch_player_nonzero_stats(
    db,
    player_identity: Dict[str, Any],
    limit_docs: int = 250
) -> List[Dict[str, Any]]:
    """
    1) Broad candidate search in `player_data`
    2) Score candidates to select best_id
    3) Fetch docs for player_key and collect metadata stats
    4) Filter out stats that are zero
    """
    name = player_identity.get("name")
    if not name or not str(name).strip():
        return []

    name_q = f"%{str(name).strip()}%"
    nat  = player_identity.get("nationality")
    print(name_q, nat)
    rows = db.execute(text("""
        SELECT id, metadata
        FROM player_data
        WHERE
          (
            (metadata->>'player_name') ILIKE :name_q
            OR (metadata->>'name') ILIKE :name_q
            OR (metadata->>'player') ILIKE :name_q
            OR (content ILIKE :name_q)
          )
          AND (
            :nat_q IS NULL
            OR (metadata->>'nationality_name') ILIKE :nat_q
            OR (metadata->>'nationality') ILIKE :nat_q
            OR (content ILIKE :nat_q)
          )
        ORDER BY id DESC
        LIMIT :lim
    """), {
        "name_q": name_q,
        "nat_q":  (f"%{nat.strip()}%"  if isinstance(nat, str) and nat.strip() else None),
        "lim": int(limit_docs),
    }).mappings().all()
    print("ROWS")
    print(rows)
    if not rows:
        return []

    # pick best player_key
    best: Tuple[float, Optional[str]] = (-1.0, None)
    for r in rows:
        meta = r.get("metadata") or {}
        sc = _score_candidate(meta, player_identity)
        rid = r.get("id")
        if rid is not None and sc > best[0]:
            best = (sc, int(rid))
    print("BEST")
    print(best)
    best_id = best[1]
    if best_id is None:
        # fallback: try extracting stats directly from the broad rows
        raw_stats: List[Dict[str, Any]] = []
        for r in rows:
            doc_meta = r.get("metadata") or {}
            raw_stats.extend(_extract_stats_from_doc_meta(doc_meta))
        return [s for s in raw_stats if _is_non_zero_stat(s)]

    # fetch all docs for that player_key
    doc = db.execute(text("""
        SELECT id, metadata
        FROM player_data
        WHERE id = :id
        LIMIT 1
    """), {"id": best_id}).mappings().first()

    if not doc:
       return []

    # non-zero filter
    doc_meta = doc.get("metadata") or {}
    raw_stats = _extract_stats_from_doc_meta(doc_meta)
    nonzero = [s for s in raw_stats if _is_non_zero_stat(s)]

    # optional: de-dupe by stat name/label if present
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for s in nonzero:
        key = _norm(str(s.get("metric") or s.get("stat") or s.get("label") or "")) or None
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(s)

    return deduped

def build_player_payload_new(meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extended payload builder that exposes the new meta fields:
    gender, height, weight, nationality, team, match_count, roles, potential.
    PLUS: fetch non-zero stats from DB and attach as `stats`.
    """
    meta_by = {(p["name"]).strip(): p for p in meta.get("players", []) if p.get("name")}
    names = sorted(set(meta_by.keys()))
    output = {"players": []}

    db = get_db()
    try:
        for name in names:
            m = meta_by.get(name, {}) or {}

            # identity used for matching docs
            player_identity = {
                "name": name,
                "team": m.get("team"),
                "nationality": m.get("nationality"),
                "gender": m.get("gender"),
                "age": m.get("age"),
                "height": m.get("height"),
                "weight": m.get("weight"),
            }
            print("PLAYER IDENTITIY READY")
            print(player_identity)
            # DB step: fetch player's non-zero stats
            stats = fetch_player_nonzero_stats(db, player_identity) 
            print("STATS")
            print(stats)
            output["players"].append({
                "name": name,
                "meta": {
                    "gender": m.get("gender"),
                    "height": m.get("height"),
                    "weight": m.get("weight"),
                    "nationality": m.get("nationality"),
                    "team": m.get("team"),
                    "match_count": m.get("match_count"),
                    "age": m.get("age"),
                    "roles": m.get("roles") or [],
                    "potential": m.get("potential"),
                },
                "stats": stats or []  
            })

        return output
    finally:
        db.close()
