from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import text
import re
import json
import unicodedata
from report_module.utilities import _num, _norm, norm_name
from api_module.utilities import get_db 

META_ID_KEYS = {
    # identity / grouping
    "player_name_norm","team_name_norm", "player_key_norm",
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

    name_i = norm_name(ident.get("name") or "")
    nat_i  = _norm(ident.get("nationality"))
    gen_i  = _norm(ident.get("gender"))

    name_m = meta.get("player_name_norm") or norm_name(meta.get("player_name") or "")
    nat_m  = _norm(meta.get("nationality_name") or meta.get("nationality") or meta.get("country"))
    gen_m  = _norm(meta.get("gender"))

    if name_i and name_m:
        if name_i == name_m: score += 10
        elif name_i in name_m or name_m in name_i: score += 7

    if nat_i and nat_m:
        if nat_i == nat_m: score += 4
        elif nat_i in nat_m or nat_m in nat_i: score += 2

    if gen_i and gen_m and gen_i == gen_m:
        score += 2

    for k, w, tol in [("age", 2.5, 2.0), ("height", 2.0, 4.0), ("weight", 2.0, 5.0)]:
        iv = _num(ident.get(k))
        mv = _num(meta.get(k) or meta.get(f"{k}_cm") or meta.get(f"{k}_kg"))
        if iv is None or mv is None:
            continue
        diff = abs(iv - mv)
        if diff <= tol: score += w
        elif diff <= tol * 2: score += w * 0.5

    return score


def fetch_player_nonzero_stats(
    db,
    player_identity: Dict[str, Any],
    limit_docs: int = 250
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    1) Broad candidate search in `player_data` (name + nationality) using BOTH original and folded name
    2) Score candidates to select best id (int8)
    3) Fetch that single row by id and collect metadata stats
    4) Filter out stats that are zero
    5) Return (stats, resolved_identity_fields) where resolved fields come from the winning row
    """
    name = player_identity.get("name")
    if not name or not str(name).strip():
        return [], {}

    name_raw = str(name).strip()
    name_norm = norm_name(name_raw)

    name_raw_q  = f"%{name_raw}%"
    name_norm_q = f"%{name_norm}%"

    nat = player_identity.get("nationality")
    nat_raw = nat.strip() if isinstance(nat, str) else ""
    nat_q = f"%{nat_raw}%" if nat_raw else None

    # Broad candidate search (name + nationality) with folded variants
    rows = db.execute(text("""
        SELECT id, metadata, content
        FROM player_data
        WHERE
        (
            (metadata->>'player_name_norm') ILIKE :name_norm_q
            OR (metadata->>'player_name') ILIKE :name_raw_q
            OR (content ILIKE :name_raw_q)
        )
        AND (
            :nat_q IS NULL
            OR (metadata->>'nationality_name') ILIKE :nat_q
            OR (content ILIKE :nat_q)
        )
        ORDER BY id DESC
        LIMIT :lim
    """), {
        "name_norm_q": name_norm_q,
        "name_raw_q": name_raw_q,
        "nat_q": nat_q,
        "lim": int(limit_docs),
    }).mappings().all()

    # ✅ fallback: name-only search if nothing returned
    if not rows:
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
            "lim": int(limit_docs),
        }).mappings().all()
    if not rows:
        return [], {}
    
    # pick best id (each row == one player)
    best: Tuple[float, Optional[int]] = (-1.0, None)
    for r in rows:
        meta = r.get("metadata") or {}
        sc = _score_candidate(meta, player_identity)
        rid = r.get("id")
        if rid is not None and sc > best[0]:
            best = (sc, int(rid))
    best_id = best[1]
    if best_id is None:
        # fallback: extract stats from broad rows
        raw_stats: List[Dict[str, Any]] = []
        for r in rows:
            doc_meta = r.get("metadata") or {}
            raw_stats.extend(_extract_stats_from_doc_meta(doc_meta))
        nonzero = [s for s in raw_stats if _is_non_zero_stat(s)]
        return nonzero, {}

    # fetch the single player row by id
    doc = db.execute(text("""
        SELECT id, metadata
        FROM player_data
        WHERE id = :id
        LIMIT 1
    """), {"id": best_id}).mappings().first()

    if not doc:
        return [], {}

    doc_meta = doc.get("metadata") or {}

    # stats
    raw_stats = _extract_stats_from_doc_meta(doc_meta)
    nonzero = [s for s in raw_stats if _is_non_zero_stat(s)]

    # optional dedupe
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for s in nonzero:
        key = _norm(str(s.get("metric") or s.get("stat") or s.get("label") or "")) or None
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(s)

    # resolved identity from winning row (authoritative when present)
    resolved_raw = {
        "team": doc_meta.get("team_name") or doc_meta.get("team"),
        "age": _num(doc_meta.get("age")),
        "height": _num(doc_meta.get("height")),
        "weight": _num(doc_meta.get("weight")),

        # optional
        "nationality": doc_meta.get("nationality_name") or doc_meta.get("nationality"),
        "gender": doc_meta.get("gender"),
        "position_name": doc_meta.get("position_name"),
        "match_count": _num(doc_meta.get("match_count")),
        "id": doc.get("id"),
    }
    # Remove None (and empty-string) fields so caller never overwrites with blanks
    resolved: Dict[str, Any] = {}
    for k, v in resolved_raw.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        resolved[k] = v

    # Normalize ints where appropriate (only if age exists after filtering)
    if "age" in resolved:
        try:
            resolved["age"] = int(round(float(resolved["age"])))
        except:
            # if conversion fails, remove to avoid bad overwrite
            resolved.pop("age", None)

    return deduped, resolved

def build_player_payload_new(meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build payload and overwrite team/age/height/weight (and optionally gender/nationality/match_count)
    from the winning DB row, if present.
    """
    meta_by = {(p["name"]).strip(): p for p in meta.get("players", []) if p.get("name")}
    names = sorted(set(meta_by.keys()))
    output = {"players": []}

    db = get_db()
    try:
        for name in names:
            m = meta_by.get(name, {}) or {}

            player_identity = {
                "name": name,
                "team": m.get("team"),
                "nationality": m.get("nationality"),
                "gender": m.get("gender"),
                "age": m.get("age"),
                "height": m.get("height"),
                "weight": m.get("weight"),
            }
            stats, resolved = fetch_player_nonzero_stats(db, player_identity)

            # overwrite only if resolved has values (resolved has no None/empty keys)
            team_final   = resolved.get("team", m.get("team"))
            age_final    = resolved.get("age", m.get("age"))
            height_final = resolved.get("height", m.get("height"))
            weight_final = resolved.get("weight", m.get("weight"))

            # optional but usually helpful to reduce confusion:
            nat_final        = resolved.get("nationality", m.get("nationality"))
            gender_final     = resolved.get("gender", m.get("gender"))
            match_count_final= resolved.get("match_count", m.get("match_count"))
            pos_final = resolved.get("position_name", m.get("position_name"))

            # Prefer DB position -> roles (frontend consumes roles)
            roles_final = m.get("roles") or []
            if pos_final:
                roles_final = [str(pos_final)]
            elif not roles_final:
                roles_final = []


            output["players"].append({
                "name": name,
                "meta": {
                    "gender": gender_final,
                    "height": height_final,
                    "weight": weight_final,
                    "nationality": nat_final,
                    "position_name": pos_final,
                    "team": team_final,
                    "match_count": match_count_final,
                    "age": age_final,
                    "roles": roles_final,
                    "potential": m.get("potential"),
                },
                "stats": stats or []
            })

        return output
    finally:
        db.close()