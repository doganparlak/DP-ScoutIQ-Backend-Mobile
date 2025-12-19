# report_module/report.py

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from langchain_deepseek import ChatDeepSeek
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from report_module.prompts import report_system_prompt
from report_module.utilities import (_score_candidate,
                                      _first_non_empty, 
                                      _normalize_roles)

CHAT_LLM = ChatDeepSeek(model="deepseek-chat", temperature=0.3)

_report_prompt = ChatPromptTemplate.from_messages([
    ("system", report_system_prompt),
    ("human", "lang: {lang}\n\n{input_text}")
])


report_chain = _report_prompt | CHAT_LLM | StrOutputParser()

# -----------------------------
# document fetch
# -----------------------------

def fetch_docs_for_favorite(
    db,
    player_identity: Dict[str, Any],
    limit: int = 30
) -> List[Dict[str, Any]]:
    """
    Behavior (aligned with your single-row-per-player DB reality):
    1) Broad candidate search in player_data using name/team/nationality filters.
       - name search uses BOTH raw and diacritic-folded patterns (Šeško vs Sesko).
    2) Score candidates with _score_candidate and pick BEST id
    3) Fetch that exact row by id and return it as a single-doc list
    """
    name = player_identity.get("name")
    if not name or not str(name).strip():
        return []

    raw_name = str(name).strip()
    name_q = f"%{raw_name}%"

    team = player_identity.get("team")
    nat  = player_identity.get("nationality")

    rows = db.execute(text("""
        SELECT id, content, metadata
        FROM player_data
        WHERE
          (
            (metadata->>'player_name') ILIKE :name_q
            OR (metadata->>'name') ILIKE :name_q
            OR (metadata->>'player') ILIKE :name_q
            OR content ILIKE :name_q
          )
          AND (
            :team_q IS NULL
            OR (metadata->>'team_name') ILIKE :team_q
            OR (metadata->>'team') ILIKE :team_q
            OR content ILIKE :team_q
          )
          AND (
            :nat_q IS NULL
            OR (metadata->>'nationality_name') ILIKE :nat_q
            OR (metadata->>'nationality') ILIKE :nat_q
            OR content ILIKE :nat_q
          )
        ORDER BY id DESC
        LIMIT 250
    """), {
        "name_q": name_q,
        "team_q": (f"%{team.strip()}%" if isinstance(team, str) and team.strip() else None),
        "nat_q":  (f"%{nat.strip()}%"  if isinstance(nat, str) and nat.strip() else None),
    }).mappings().all()

    if not rows:
        return []

    # Pick best ID by scoring
    best: Tuple[float, Optional[int]] = (-1.0, None)
    for r in rows:
        meta = r.get("metadata") or {}
        sc = _score_candidate(meta, player_identity)
        rid = r.get("id")
        if rid is not None and sc > best[0]:
            best = (sc, int(rid))

    best_id = best[1]
    if best_id is None:
        # fallback: return top rows as-is
        return [{"id": r["id"], "content": r.get("content"), "metadata": r.get("metadata")} for r in rows[:limit]]

    # Fetch the single row by ID (since each player has exactly one row)
    doc = db.execute(text("""
        SELECT id, content, metadata
        FROM player_data
        WHERE id = :id
        LIMIT 1
    """), {"id": best_id}).mappings().first()

    if not doc:
        return []

    return [{
        "id": doc.get("id"),
        "content": doc.get("content"),
        "metadata": doc.get("metadata"),
    }]

def build_player_card_from_docs(metric_docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    card: Dict[str, Any] = {}

    for d in metric_docs:
        meta = d.get("metadata") or {}

        name = _first_non_empty(meta.get("player_name"), meta.get("name"), meta.get("player"))
        team = _first_non_empty(meta.get("team"), meta.get("team_name"), meta.get("club"))
        nationality = _first_non_empty(meta.get("nationality"), meta.get("nationality_name"), meta.get("country"))
        gender = _first_non_empty(meta.get("gender"))
        age = _first_non_empty(meta.get("age"))
        height = _first_non_empty(meta.get("height"), meta.get("height_cm"))
        weight = _first_non_empty(meta.get("weight"), meta.get("weight_kg"))
        potential = _first_non_empty(meta.get("potential"))
        roles_raw = _first_non_empty(meta.get("roles"), meta.get("roles_json"), meta.get("position"), meta.get("position_name"))

        if "name" not in card and name is not None:
            card["name"] = name
        if "team" not in card and team is not None:
            card["team"] = team
        if "nationality" not in card and nationality is not None:
            card["nationality"] = nationality
        if "gender" not in card and gender is not None:
            card["gender"] = gender
        if "age" not in card and age is not None:
            card["age"] = age
        if "height" not in card and height is not None:
            card["height"] = height
        if "weight" not in card and weight is not None:
            card["weight"] = weight
        if "potential" not in card and potential is not None:
            card["potential"] = potential

        if "roles" not in card:
            roles = _normalize_roles(roles_raw)
            if roles:
                card["roles"] = roles

    if "roles" not in card:
        card["roles"] = []

    return card


def _build_llm_input(player_card: Dict[str, Any], metric_docs: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    parts.append("PLAYER_CARD_JSON:")
    parts.append(str(player_card or {}))

    parts.append("\nMETRIC_DOCUMENTS (newest first):")
    if not metric_docs:
        parts.append("[]")
    else:
        for d in metric_docs[:30]:
            meta = d.get("metadata") or {}
            content = (d.get("content") or "").strip()
            if len(content) > 1200:
                content = content[:1200] + "…"
            parts.append(f"\n- doc_id: {d.get('id')}")
            parts.append(f"  metadata: {meta}")
            parts.append(f"  content: {content}")

    return "\n".join(parts)

def generate_report_content(
    db,
    favorite_id: str,
    lang: str = "en",
    version: int = 1,
    player_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    docs = fetch_docs_for_favorite(
        db,
        player_identity=player_identity or {},
        limit=30
    )
    player_card = build_player_card_from_docs(docs)

    input_text = _build_llm_input(player_card, docs)

    report_text = (report_chain.invoke({"input_text": input_text, "lang": lang}) or "").strip()

    content_json = {
        "favorite_player_id": favorite_id,
        "language": lang,
        "version": version,
        "player_identity": player_identity or {},  
        "player_card": player_card,
        "metrics_docs": docs,
        "report_text": report_text,
    }
    return {"content": report_text, "content_json": content_json}

