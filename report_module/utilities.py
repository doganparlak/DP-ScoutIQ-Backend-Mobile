import re
from typing import Any, Dict, List, Optional
from sqlalchemy import text
import unicodedata

def _num(v: Any) -> Optional[float]:
    try:
        if v is None: return None
        return float(v)
    except:
        return None
    
def norm_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))  # remove accents
    s = re.sub(r"[^a-z0-9\s]", " ", s)  # drop punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s
    
def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()

def _score_candidate(meta: Dict[str, Any], ident: Dict[str, Any]) -> float:
    score = 0.0

    name_i = norm_name(ident.get("name") or "")
    team_i = norm_name(ident.get("team") or "")
    nat_i  = _norm(ident.get("nationality"))
    gen_i  = _norm(ident.get("gender"))

    name_m = meta.get("player_name_norm") or norm_name(meta.get("player_name") or "")
    team_m = meta.get("team_name_norm") or norm_name(meta.get("team_name") or meta.get("team"))
    nat_m  = _norm(meta.get("nationality_name") or meta.get("nationality") or meta.get("country"))
    gen_m  = _norm(meta.get("gender"))

    # name is most important
    if name_i and name_m:
        if name_i == name_m:
            score += 10
        elif name_i in name_m or name_m in name_i:
            score += 7

    if team_i and team_m:
        if team_i == team_m:
            score += 6
        elif team_i in team_m or team_m in team_i:
            score += 4

    if nat_i and nat_m:
        if nat_i == nat_m:
            score += 4
        elif nat_i in nat_m or nat_m in nat_i:
            score += 2

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

def _extract_player_group_key(meta: Dict[str, Any]) -> Optional[str]:
    pk = meta.get("player_key")
    if pk and str(pk).strip():
        return str(pk).strip()
    # fallback
    pn = meta.get("player_name")
    tn = meta.get("team_name")
    if pn and tn:
        return f"{str(pn).strip()}|{str(tn).strip()}"
    return None

def _first_non_empty(*vals):
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def _normalize_roles(val: Any) -> List[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x) for x in val if x]
    if isinstance(val, str):
        # allow comma separated roles
        if "," in val:
            return [x.strip() for x in val.split(",") if x.strip()]
        return [val.strip()] if val.strip() else []
    return []