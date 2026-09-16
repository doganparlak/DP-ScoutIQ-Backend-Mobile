from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from sqlalchemy import text

from constants_module.constants import ROLE_LONG_TO_SHORT, ROLE_SHORT_TO_LONG
from report_module.phases import with_phase_distributions
from report_module.prompts import report_system_prompt
from report_module.utilities import _first_non_empty, _normalize_roles, _score_candidate, norm_name

load_dotenv()

CHAT_LLM = ChatOpenAI(
    model=os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna"),
    api_key=os.environ["OPENAI_API_KEY"],
    temperature=0.3,
)

_report_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", report_system_prompt),
        ("human", "lang: {lang}\n\n{input_text}"),
    ]
)

report_chain = _report_prompt | CHAT_LLM | StrOutputParser()

_section_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", """You are an expert football scouting analyst. Produce only the requested report section.
Use only the supplied player card and metric evidence. Never invent numeric facts or mention missing data.
Every line after the exact English section header must start with '- ' and use '- Short title: explanation'.
Write the header in English exactly as requested. Write every bullet title and explanation in the requested language.
Keep titles under 42 characters. Use professional tactical interpretation tied to the player's actual role.
When lang is tr, use natural Turkish football terminology and comma decimal separators; do not leave English
metric or tactical terms in the prose.
For strengths, every bullet must combine metric evidence, the match phase or game situation where the quality
matters, and its tactical benefit or usage implication. Give strengths the same depth and approximate length
as weakness bullets; target roughly 30-45 words per explanation.
For weaknesses, prioritize CONCERN_CANDIDATES from the metric guide. If there are fewer than the requested number, use
role-relevant positive/output metrics as conditional development limits or tactical trade-offs. Ground every
point in an actual supplied value, describe the game situation where it matters, and add a practical mitigation.
Do not relabel LOW_RISK_NEGATIVES as poor performance. Never fill a weakness bullet by saying that a concern,
weakness, metric, or evidence is absent, unverified, unavailable, or not a problem.
For role usage, never recommend a role family outside ROLE_CONSTRAINTS."""),
        ("human", """lang: {lang}
SECTION_HEADER: {heading}
SECTION_RULES: {rules}

{input_text}"""),
    ]
)
section_report_chain = _section_prompt | CHAT_LLM | StrOutputParser()

LAZY_NARRATIVE_SECTIONS: Dict[str, Tuple[str, str]] = {
    "strengths": (
        "STRENGTHS",
        "Provide exactly 5 distinct strengths. Each explanation must be roughly 30-45 words and include at least one supplied metric value, a relevant match phase or game situation, and the resulting tactical benefit or usage implication.",
    ),
    "weaknesses": (
        "POTENTIAL WEAKNESSES / CONCERNS",
        "Provide exactly 5 distinct, evidence-led concerns or development limits. Every bullet must cite at least one supplied metric value, explain a relevant match situation, and give a mitigation or coaching cue. Never output a no-concern or no-evidence placeholder.",
    ),
    "role_usage": (
        "CONCLUSION",
        "Provide exactly 5 bullets in this order: Role & System, Development Focus, Usage Recommendation, In Possession, Out of Possession. Use the corresponding Turkish titles when lang is tr.",
    ),
}

_NARRATIVE_SECTIONS = {
    "STRENGTHS",
    "POTENTIAL WEAKNESSES / CONCERNS",
    "CONCLUSION",
}
_MOBILE_NARRATIVE_TITLE_MAX_LENGTH = 42
_MOBILE_CONCLUSION_TITLES: Dict[str, Tuple[str, ...]] = {
    "en": (
        "Role & System",
        "Development Focus",
        "Usage Recommendation",
        "In Possession",
        "Out of Possession",
    ),
    "tr": (
        "Rol & Sistem",
        "Gelişim Odağı",
        "Kullanım Önerisi",
        "Toplu Oyunda",
        "Topsuz Oyunda",
    ),
}


def _shorten_narrative_title(title: str, lang: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()

    # Turkish report prose must not contain bilingual title annotations. UI
    # terminology translation belongs to the clients, not to model prose.
    if lang == "tr":
        without_parenthetical = re.sub(r"\s*\([^()]*[A-Za-z][^()]*\)\s*$", "", title).strip()
        if without_parenthetical:
            title = without_parenthetical
    if len(title) <= _MOBILE_NARRATIVE_TITLE_MAX_LENGTH:
        return title

    shortened = title[:_MOBILE_NARRATIVE_TITLE_MAX_LENGTH].rstrip()
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0].rstrip()
    return shortened.rstrip("-–—,;/") or title[:_MOBILE_NARRATIVE_TITLE_MAX_LENGTH]


def normalize_mobile_report_format(report_text: str, lang: str) -> str:
    """Enforce the title/body contract consumed by the existing mobile app."""
    if not report_text:
        return report_text

    active_section: Optional[str] = None
    conclusion_index = 0
    output: List[str] = []
    report_lines = report_text.splitlines()
    conclusion_titles = _MOBILE_CONCLUSION_TITLES.get(lang, ())
    for line_index, line in enumerate(report_lines):
        stripped = line.strip()
        if stripped in _NARRATIVE_SECTIONS:
            active_section = stripped
            if stripped == "CONCLUSION":
                conclusion_index = 0
                remaining = []
                for next_line in report_lines[line_index + 1:]:
                    if next_line.strip() in _NARRATIVE_SECTIONS:
                        break
                    if next_line.strip().startswith('- '):
                        remaining.append(next_line)
                original = _MOBILE_CONCLUSION_TITLES.get(lang, ())
                indices = (0, 3, 4) if len(remaining) == 3 else (0, 1, 3, 4) if len(remaining) == 4 else tuple(range(len(original)))
                conclusion_titles = tuple(original[i] for i in indices if i < len(original))
            output.append(line)
            continue
        if stripped and not stripped.startswith("-") and stripped.isupper():
            active_section = None

        if active_section and re.match(r"^\s*-\s+", line):
            item = re.sub(r"^\s*-\s+", "", line).strip()
            match = re.match(r"^([^:：]+)[:：]\s*(.+)$", item)
            if match:
                if active_section == "CONCLUSION" and conclusion_index < len(conclusion_titles):
                    title = conclusion_titles[conclusion_index]
                    conclusion_index += 1
                else:
                    title = _shorten_narrative_title(match.group(1), lang)
                output.append(f"- {title}: {match.group(2).strip()}")
                continue
        output.append(line)
    return "\n".join(output)

ROLE_USAGE_CONSTRAINTS: Dict[str, Dict[str, Any]] = {
    "GK": {
        "allowed": "goalkeeper only",
        "forbidden": "outfield roles such as defender, midfielder, winger, forward, striker",
    },
    "LB": {
        "allowed": "left back / fullback only",
        "forbidden": "center midfield, number 8, winger, striker, center forward, goalkeeper",
    },
    "RB": {
        "allowed": "right back / fullback only",
        "forbidden": "center midfield, number 8, winger, striker, center forward, goalkeeper",
    },
    "CB": {
        "allowed": "center back only",
        "forbidden": "fullback, center midfield, number 8, winger, striker, center forward, goalkeeper",
    },
    "LM": {
        "allowed": "left midfield / wide midfielder only",
        "forbidden": "center back, fullback, defensive midfielder, striker, goalkeeper",
    },
    "RM": {
        "allowed": "right midfield / wide midfielder only",
        "forbidden": "center back, fullback, defensive midfielder, striker, goalkeeper",
    },
    "CDM": {
        "allowed": "defensive midfielder / holding midfielder only",
        "forbidden": "center forward, striker, winger, fullback, center back, goalkeeper",
    },
    "CM": {
        "allowed": "central midfielder / number 8 only",
        "forbidden": "center forward, striker, winger, fullback, center back, goalkeeper",
    },
    "CAM": {
        "allowed": "attacking midfielder / number 10 only",
        "forbidden": "center forward, striker, fullback, center back, goalkeeper",
    },
    "LW": {
        "allowed": "left winger / left wide forward only",
        "forbidden": "center midfield, number 8, defensive midfielder, fullback, center back, goalkeeper",
    },
    "RW": {
        "allowed": "right winger / right wide forward only",
        "forbidden": "center midfield, number 8, defensive midfielder, fullback, center back, goalkeeper",
    },
    "CF": {
        "allowed": "striker / center forward only",
        "forbidden": "center midfield, number 8, attacking midfielder, defensive midfielder, winger, fullback, center back, goalkeeper",
    },
}

NEGATIVE_METRIC_RANGES: Dict[str, Tuple[float, float]] = {
    "Goals Conceded": (0, 2),
    "Penalties Committed": (0, 0.15),
    "Penalties Missed": (0, 0.15),
    "Shots Off Target": (0, 2.5),
    "Big Chances Missed": (0, 1),
    "Aerials Lost": (0, 4),
    "Duels Lost": (0, 6),
    "Fouls": (0, 2),
    "Dispossessed": (0, 5),
    "Dribbled Past": (0, 2),
    "Turn Over": (0, 3),
    "Possession Lost": (0, 20),
    "Offsides": (0, 0.3),
    "Own Goals": (0, 0.2),
    "Error Lead To Goal": (0, 0.25),
    "Error Lead To Shot": (0, 0.4),
    "Yellow Cards": (0, 0.4),
    "Yellow & Red Cards": (0, 1),
    "Red Cards": (0, 0.2),
}

CONCERN_RISK_THRESHOLD = 0.33
WATCH_RISK_THRESHOLD = 0.66


def _role_short(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    upper = raw.upper()
    if upper in ROLE_SHORT_TO_LONG:
        return upper
    return ROLE_LONG_TO_SHORT.get(raw.lower())


def _role_constraint_block(player_card: Dict[str, Any]) -> str:
    raw_roles: List[Any] = []
    roles = player_card.get("roles")
    if isinstance(roles, list):
        raw_roles.extend(roles)
    elif roles:
        raw_roles.append(roles)
    raw_roles.extend(
        value for value in (
            player_card.get("position_name"),
            player_card.get("position"),
            player_card.get("role"),
        )
        if value
    )

    mapped = []
    for role in raw_roles:
        short = _role_short(role)
        if short and short not in mapped:
            mapped.append(short)

    if not mapped:
        return (
            "ROLE_CONSTRAINTS:\n"
            "- No reliable role was provided. Do not invent a new position; keep role recommendations generic and avoid naming a different position.\n"
        )

    primary = mapped[0]
    constraint = ROLE_USAGE_CONSTRAINTS.get(primary, {})
    allowed = constraint.get("allowed", ROLE_SHORT_TO_LONG.get(primary, primary))
    forbidden = constraint.get("forbidden", "any unrelated role family")
    mapped_labels = ", ".join(f"{short} ({ROLE_SHORT_TO_LONG.get(short, short)})" for short in mapped)

    return "\n".join(
        [
            "ROLE_CONSTRAINTS:",
            f"- Source roles mapped from the player data: {mapped_labels}.",
            f"- Primary role for Role & Usage recommendations: {primary} ({ROLE_SHORT_TO_LONG.get(primary, primary)}).",
            f"- Allowed recommendation space: {allowed}.",
            f"- Forbidden recommendation space: {forbidden}.",
            "- In CONCLUSION / Role & Usage, every role, system, in-possession, and out-of-possession recommendation MUST stay inside the allowed recommendation space.",
            "- If metrics suggest a different role family, ignore that temptation and explain how those metrics help the mapped primary role instead.",
        ]
    )


def _metric_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


NEGATIVE_METRIC_BY_KEY = {_metric_key(metric): metric for metric in NEGATIVE_METRIC_RANGES}


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number else None
    raw = str(value).strip().replace("%", "").replace(",", ".")
    if not raw:
        return None
    try:
        number = float(raw)
    except ValueError:
        return None
    return number if number == number else None


def _iter_negative_metric_values(metadata: Dict[str, Any]) -> List[Tuple[str, float]]:
    values: Dict[str, float] = {}
    meta = metadata or {}

    for raw_metric, raw_value in meta.items():
        metric = NEGATIVE_METRIC_BY_KEY.get(_metric_key(raw_metric))
        if not metric:
            continue
        value = _num(raw_value)
        if value is not None:
            values[metric] = value

    for container_key in ("stats", "statistics", "metrics"):
        raw_stats = meta.get(container_key)
        if not isinstance(raw_stats, list):
            continue
        for stat in raw_stats:
            if not isinstance(stat, dict):
                continue
            metric = NEGATIVE_METRIC_BY_KEY.get(
                _metric_key(stat.get("metric") or stat.get("stat") or stat.get("label") or stat.get("name"))
            )
            if not metric:
                continue
            value = _num(stat.get("value") or stat.get("amount") or stat.get("score"))
            if value is not None:
                values[metric] = value

    return sorted(values.items())


def _build_metric_significance_block(metric_docs: List[Dict[str, Any]]) -> str:
    strongest_values: Dict[str, float] = {}
    for doc in metric_docs or []:
        for metric, value in _iter_negative_metric_values(doc.get("metadata") or {}):
            previous = strongest_values.get(metric)
            if previous is None or value > previous:
                strongest_values[metric] = value

    if not strongest_values:
        return "\nMETRIC_SIGNIFICANCE_GUIDE:\nNo normalized risk metrics available."

    concern_lines: List[str] = []
    low_risk_lines: List[str] = []

    for metric, value in sorted(strongest_values.items()):
        min_value, max_value = NEGATIVE_METRIC_RANGES[metric]
        if max_value <= min_value:
            continue
        risk = max(0.0, min(1.0, (value - min_value) / (max_value - min_value)))
        line = f"- {metric}: value={value:g}, risk={risk:.2f}"
        if risk >= CONCERN_RISK_THRESHOLD:
            severity = "problem" if risk >= WATCH_RISK_THRESHOLD else "watch"
            concern_lines.append(f"{line}, concern_level={severity}")
        else:
            low_risk_lines.append(f"{line}, concern_level=low")

    lines = [
        "\nMETRIC_SIGNIFICANCE_GUIDE:",
        "For negative/risk metrics, risk is normalized as (value - min) / (max - min).",
        "Only use CONCERN_CANDIDATES as direct weaknesses. Do not cite LOW_RISK_NEGATIVES as weaknesses.",
        "If a low-risk negative metric is mentioned in PLAYER STATS, keep it factual or positive-neutral; do not frame it as a concern.",
        "CONCERN_CANDIDATES:",
    ]
    lines.extend(concern_lines or ["- None"])
    lines.append("LOW_RISK_NEGATIVES:")
    lines.extend(low_risk_lines or ["- None"])
    return "\n".join(lines)


def fetch_docs_for_favorite(
    db,
    player_identity: Dict[str, Any],
    limit_docs: int = 30,
) -> List[Dict[str, Any]]:
    from report_module.identity import report_sportmonks_id, fetch_report_player
    provider_id = report_sportmonks_id(player_identity)
    if provider_id is not None:
        row = fetch_report_player(db, provider_id)
        return [{"id": row["id"], "content": row.get("content"), "metadata": row.get("metadata")}]

    club_player_id = player_identity.get("club_player_id") or player_identity.get("clubPlayerId")
    if club_player_id is not None:
        row = db.execute(
            text(
                """
                SELECT id, metadata, content
                FROM player_data
                WHERE id = :player_id
                LIMIT 1
                """
            ),
            {"player_id": club_player_id},
        ).mappings().first()
        if row:
            return [{"id": row["id"], "content": row.get("content"), "metadata": row.get("metadata")}]

    name = player_identity.get("name")
    if not name or not str(name).strip():
        return []

    name_raw = str(name).strip()
    name_norm = norm_name(name_raw)
    name_raw_q = f"%{name_raw}%"
    name_norm_q = f"%{name_norm}%"

    nat = player_identity.get("nationality")
    nat_raw = nat.strip() if isinstance(nat, str) else ""
    nat_q = f"%{nat_raw}%" if nat_raw else None

    rows = db.execute(
        text(
            """
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
            """
        ),
        {
            "name_norm_q": name_norm_q,
            "name_raw_q": name_raw_q,
            "nat_q": nat_q,
            "lim": int(limit_docs),
        },
    ).mappings().all()

    if not rows:
        rows = db.execute(
            text(
                """
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
                """
            ),
            {"name_norm_q": name_norm_q, "name_raw_q": name_raw_q, "lim": int(limit_docs)},
        ).mappings().all()
    if not rows:
        return []

    best: Tuple[float, Optional[int]] = (-1.0, None)
    for row in rows:
        score = _score_candidate(row.get("metadata") or {}, player_identity)
        row_id = row.get("id")
        if row_id is not None and score > best[0]:
            best = (score, int(row_id))

    if best[1] is None:
        return [{"id": row["id"], "content": row.get("content"), "metadata": row.get("metadata")} for row in rows[:limit_docs]]

    doc = db.execute(
        text(
            """
            SELECT id, metadata, content
            FROM player_data
            WHERE id = :id
            LIMIT 1
            """
        ),
        {"id": best[1]},
    ).mappings().first()
    if not doc:
        return []

    return [{"id": doc["id"], "content": doc.get("content"), "metadata": doc.get("metadata")}]


def build_player_card_from_docs(metric_docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    card: Dict[str, Any] = {}

    for doc in metric_docs:
        meta = doc.get("metadata") or {}

        fields = {
            "name": _first_non_empty(meta.get("player_name"), meta.get("name"), meta.get("player")),
            "team": _first_non_empty(meta.get("team"), meta.get("team_name"), meta.get("club")),
            "nationality": _first_non_empty(meta.get("nationality"), meta.get("nationality_name"), meta.get("country")),
            "gender": _first_non_empty(meta.get("gender")),
            "age": _first_non_empty(meta.get("age")),
            "height": _first_non_empty(meta.get("height"), meta.get("height_cm")),
            "weight": _first_non_empty(meta.get("weight"), meta.get("weight_kg")),
            "potential": _first_non_empty(meta.get("potential")),
            "form": _first_non_empty(meta.get("form")),
            "position_name": _first_non_empty(meta.get("position_name"), meta.get("position")),
        }
        for key, value in fields.items():
            if key not in card and value is not None:
                card[key] = value

        if "roles" not in card:
            if card.get("position_name"):
                card["roles"] = [str(card["position_name"])]
            else:
                roles_raw = _first_non_empty(meta.get("roles"), meta.get("roles_json"), meta.get("position"), meta.get("position_name"))
                card["roles"] = _normalize_roles(roles_raw)

    if "roles" not in card:
        card["roles"] = [str(card["position_name"])] if card.get("position_name") else []

    return card


def _build_llm_input(player_card: Dict[str, Any], metric_docs: List[Dict[str, Any]]) -> str:
    parts: List[str] = ["PLAYER_CARD_JSON:", str(player_card or {}), "\nMETRIC_DOCUMENTS (newest first):"]
    parts.insert(0, _role_constraint_block(player_card))
    parts.insert(1, _build_metric_significance_block(metric_docs))

    if not metric_docs:
        parts.append("[]")
    else:
        for doc in metric_docs[:30]:
            meta = doc.get("metadata") or {}
            content = (doc.get("content") or "").strip()
            if len(content) > 1200:
                content = content[:1200] + "..."
            parts.append(f"\n- doc_id: {doc.get('id')}")
            parts.append(f"  metadata: {meta}")
            parts.append(f"  content: {content}")

    return "\n".join(parts)


def build_report_foundation(
    db,
    favorite_id: str,
    lang: str = "en",
    version: int = 1,
    player_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    identity = player_identity or {}
    docs = fetch_docs_for_favorite(db, player_identity=identity, limit_docs=30)
    player_card = build_player_card_from_docs(docs)

    for key, value in identity.items():
        if key not in player_card and value is not None:
            player_card[key] = value
    if identity.get("roles"):
        player_card["roles"] = identity["roles"]
    for role_key in ("position_name", "position", "role"):
        if identity.get(role_key):
            player_card[role_key] = identity[role_key]
    for score_key in ("potential", "form"):
        if score_key not in player_card and identity.get(score_key) is not None:
            player_card[score_key] = identity[score_key]

    content_json = {
        "favorite_player_id": favorite_id,
        "language": lang,
        "version": version,
        "player_identity": identity,
        "player_card": player_card,
        "metrics_docs": docs,
        "report_text": "",
        "sections": {
            "data": {"status": "ready"},
            "strengths": {"status": "pending"},
            "weaknesses": {"status": "pending"},
            "role_usage": {"status": "pending"},
        },
    }
    return {"content": "", "content_json": with_phase_distributions(content_json)}


def complete_report_foundation(foundation: Dict[str, Any], lang: str, access_tier: str | None = None) -> Dict[str, Any]:
    """Complete a persisted data foundation with the report's narrative layer."""
    content_json = dict(foundation or {})
    player_card = dict(content_json.get("player_card") or {})
    docs = list(content_json.get("metrics_docs") or [])
    if access_tier is not None:
        current = {**foundation, "generation_mode": "lazy_sections"}
        for section in LAZY_NARRATIVE_SECTIONS:
            result = complete_report_section(current, section, lang, access_tier)
            current = result['content_json']
        return result
    report_text = (report_chain.invoke({"input_text": _build_llm_input(player_card, docs), "lang": lang}) or "").strip()
    report_text = normalize_mobile_report_format(report_text, lang)
    content_json["report_text"] = report_text
    content_json["sections"] = {
        "data": {"status": "ready"},
        "analysis": {"status": "ready"},
        "strengths": {"status": "ready"},
        "weaknesses": {"status": "ready"},
        "role_usage": {"status": "ready"},
    }
    return {"content": report_text, "content_json": with_phase_distributions(content_json)}


def complete_report_section(foundation: Dict[str, Any], section: str, lang: str, access_tier: str = 'paid') -> Dict[str, Any]:
    """Generate one optional narrative section and merge it into a persisted report."""
    if section not in LAZY_NARRATIVE_SECTIONS:
        raise ValueError(f"Unsupported report section: {section}")
    content_json = dict(foundation or {})
    player_card = dict(content_json.get("player_card") or {})
    docs = list(content_json.get("metrics_docs") or [])
    heading, rules = LAZY_NARRATIVE_SECTIONS[section]
    if section == 'role_usage':
        titles = ['Role & System', 'In Possession', 'Out of Possession']
        if access_tier == 'paid':
            titles.insert(1, 'Development Focus')
        expected_bullets = len(titles)
        rules = (f"Provide exactly {expected_bullets} bullets in this order: " + ', '.join(titles)
                 + '. Use the corresponding Turkish titles when lang is tr. '
                 + 'Do not generate Usage Recommendation or any other section.'
                 + (' Do not generate Development Focus.' if access_tier == 'free' else ''))
    else:
        expected_bullets = 2 if access_tier == 'free' else 3
        rules = rules.replace('exactly 5', f'exactly {expected_bullets}')
    raw_section_text = (section_report_chain.invoke({
        "input_text": _build_llm_input(player_card, docs),
        "lang": lang,
        "heading": heading,
        "rules": rules,
    }) or "").strip()
    bullet_lines = [line.strip() for line in raw_section_text.splitlines() if line.strip().startswith("-")]
    if len(bullet_lines) != expected_bullets:
        raise ValueError(f"Narrative section must contain exactly {expected_bullets} bullets: {section}")
    if section == "weaknesses":
        normalized_output = raw_section_text.casefold()
        placeholder_phrases = (
            "no concern",
            "not a concern",
            "not a weakness",
            "no verified",
            "endişe yok",
            "endişe oluşturmamaktadır",
            "zayıflık adayı değildir",
            "doğrulanmış bir endişe",
        )
        if any(phrase in normalized_output for phrase in placeholder_phrases):
            raise ValueError("Weakness section contains no-concern placeholder copy")
    section_text = f"{heading}\n" + "\n".join(bullet_lines)
    section_text = normalize_mobile_report_format(section_text, lang)
    texts = dict(content_json.get("narrative_sections") or {})
    texts[section] = section_text
    content_json["narrative_sections"] = texts
    content_json["report_text"] = "\n\n".join(
        texts[key] for key in ("strengths", "weaknesses", "role_usage") if texts.get(key)
    )
    sections = dict(content_json.get("sections") or {})
    sections.setdefault("data", {"status": "ready"})
    sections[section] = {"status": "ready", "access_tier": access_tier}
    content_json["sections"] = sections
    return {"content": content_json["report_text"], "content_json": with_phase_distributions(content_json)}


def generate_report_content(
    db,
    favorite_id: str,
    lang: str = "en",
    version: int = 1,
    player_identity: Optional[Dict[str, Any]] = None,
    access_tier: str | None = None,
) -> Dict[str, Any]:
    foundation = build_report_foundation(db, favorite_id, lang, version, player_identity)
    return complete_report_foundation(foundation["content_json"], lang, access_tier)
