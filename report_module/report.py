# report_module/report.py

from __future__ import annotations

from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from langchain_deepseek import ChatDeepSeek
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from report_module.prompts import report_system_prompt


CHAT_LLM = ChatDeepSeek(model="deepseek-chat", temperature=0.3)

_report_prompt = ChatPromptTemplate.from_messages([
    ("system", report_system_prompt),
    ("human", "{input_text}")
])

report_chain = _report_prompt | CHAT_LLM | StrOutputParser()


# -----------------------------
# documents_v4 fetch
# -----------------------------

def fetch_docs_for_favorite(db, favorite_id: str, limit: int = 30) -> List[Dict[str, Any]]:
    """
    Fetch documents_v4 rows linked to this favorite player.

    This assumes you store favorite linkage in metadata.favorite_player_id.
    If your key is different, change (metadata->>'favorite_player_id').
    """
    rows = db.execute(text("""
        SELECT id, content, metadata
        FROM documents_v4
        WHERE (metadata->>'favorite_player_id') = :fid
        ORDER BY id DESC
        LIMIT :lim
    """), {"fid": favorite_id, "lim": limit}).mappings().all()

    return [{"id": r["id"], "content": r.get("content"), "metadata": r.get("metadata")} for r in rows]


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


def build_player_card_from_docs(metric_docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Merge metadata across docs to get a single 'best effort' player card.
    We do NOT invent; we only take what appears in metadata.
    """
    card: Dict[str, Any] = {}

    # Iterate newest-first; take first non-empty value per field.
    for d in metric_docs:
        meta = d.get("metadata") or {}

        # Common key variants (support multiple)
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

    # Ensure roles is always present as list (even empty)
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
) -> Dict[str, Any]:
    """
    Generate report using only documents_v4.
    Returns: {"content": str, "content_json": {...}}
    """
    docs = fetch_docs_for_favorite(db, favorite_id=favorite_id, limit=30)
    player_card = build_player_card_from_docs(docs)

    # If docs contain no name at all, we still try (LLM will fallback to general),
    # but the report will be less useful; you can choose to error instead.
    input_text = _build_llm_input(player_card, docs)
    report_text = (report_chain.invoke({"input_text": input_text}) or "").strip()

    content_json = {
        "favorite_player_id": favorite_id,
        "language": lang,
        "version": version,
        "player_card": player_card,
        "metrics_docs": docs,
        "report_text": report_text,
    }

    return {"content": report_text, "content_json": content_json}
