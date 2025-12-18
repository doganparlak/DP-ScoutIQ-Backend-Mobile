from typing import Dict, Any
import re
import json

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


def build_player_payload_new(meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extended payload builder that exposes the new meta fields:
    gender, height, weight, nationality, team, match_count, roles, potential.
    """
    meta_by = { (p["name"]).strip(): p for p in meta.get("players", []) if p.get("name") }

    names = sorted(set(meta_by.keys()))

    output = {"players": []}

    for name in names:
        m = meta_by.get(name, {})
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
        })

    return output
