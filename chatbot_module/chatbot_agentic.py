from typing import Any, Dict, List, Optional
import json
import os
import random
import re
import warnings

from dotenv import load_dotenv
from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

load_dotenv()
warnings.filterwarnings("ignore", category=DeprecationWarning, module="langchain")

from api_module.utilities import (
    append_chat_message,
    get_db,
)
from chatbot_module.chatbot import (
    BROAD_CANDIDATE_RETRIEVER,
    CANDIDATE_RETRIEVER,
    CHAT_LLM,
    SHARED_RETRIEVER,
    answer_question as legacy_answer_question,
    get_session_state,
    output_tr_translate_chain,
    translate_to_english_if_needed,
)
from chatbot_module.prompts_agentic import (
    AGENTIC_COMPARISON_PROMPT,
    AGENTIC_CONSTRAINT_PROMPT,
    AGENTIC_CONTROLLER_PROMPT,
    AGENTIC_FOLLOWUP_PROMPT,
    AGENTIC_IDENTITY_RESOLVER_PROMPT,
    AGENTIC_NAMED_COMPARISON_PROMPT,
    AGENTIC_NARRATIVE_PROMPT,
    AGENTIC_SCORING_PROMPT,
    AGENTIC_SELECTOR_PROMPT,
)
from chatbot_module.prompts import (
    translate_en_to_tr_system_message,
    translate_tr_to_en_system_message,
)
from chatbot_module.tools import (
    collect_recent_human_constraints,
    get_seen_players_from_history,
    is_generic_alternative_request,
    is_turkish,
)
from chatbot_module.tools_agentic import (
    apply_ai_scores_to_candidate,
    build_agentic_context,
    build_filtered_retriever_agentic,
    build_payload_from_candidate,
    doc_to_candidate,
    extract_json_object,
    fetch_direct_player_candidates_by_name,
    fetch_direct_player_candidate_by_name,
    format_candidates_for_selector,
    is_greeting_or_offtopic,
    clean_constraints,
    candidate_constraint_rejection,
    constraint_relaxation_label,
    infer_league_from_text,
    infer_nationality_from_text,
    infer_excluded_constraints_from_text,
    infer_position_from_text,
    infer_preferred_stats_from_text,
    _quality_debug,
    short_offtopic_response,
    validate_candidate,
)
from potential_form_module.form import reveal_player_form
from potential_form_module.potential import reveal_player_potential


controller_prompt = ChatPromptTemplate.from_messages([
    ("system", AGENTIC_CONTROLLER_PROMPT),
    ("human",
     "Original question:\n{original_question}\n\n"
     "Translated English question:\n{translated_question}\n\n"
     "Strategy:\n{strategy}\n\n"
     "Seen players:\n{seen_players}\n\n"
     "Recent chat memory:\n{recent_memory}\n\n"
     "Return JSON only.")
])
controller_chain = controller_prompt | CHAT_LLM | StrOutputParser()


constraint_prompt = ChatPromptTemplate.from_messages([
    ("system", AGENTIC_CONSTRAINT_PROMPT),
    ("human",
     "Original question:\n{original_question}\n\n"
     "Translated English question:\n{translated_question}\n\n"
     "Strategy:\n{strategy}\n\n"
     "Recent carried constraints:\n{recent_constraints}\n\n"
     "Return JSON only.")
])
constraint_chain = constraint_prompt | CHAT_LLM | StrOutputParser()


selector_prompt = ChatPromptTemplate.from_messages([
    ("system", AGENTIC_SELECTOR_PROMPT),
    ("human",
     "User request:\n{question}\n\n"
     "Strategy:\n{strategy}\n\n"
     "Target team, if any:\n{target_team}\n\n"
     "Premium request:\n{premium_only}\n\n"
     "Extracted constraints:\n{constraints_json}\n\n"
     "Seen players:\n{seen_players}\n\n"
     "RAG candidate list:\n{candidate_list}\n\n"
     "Return JSON only.")
])
selector_chain = selector_prompt | CHAT_LLM | StrOutputParser()


identity_resolver_prompt = ChatPromptTemplate.from_messages([
    ("system", AGENTIC_IDENTITY_RESOLVER_PROMPT),
    ("human",
     "User typed name:\n{question}\n\n"
     "Candidate list:\n{candidate_list}\n\n"
     "Return JSON only.")
])
identity_resolver_chain = identity_resolver_prompt | CHAT_LLM | StrOutputParser()


scoring_prompt = ChatPromptTemplate.from_messages([
    ("system", AGENTIC_SCORING_PROMPT),
    ("human",
     "User request:\n{question}\n\n"
     "Strategy:\n{strategy}\n\n"
     "Candidate profile and stats:\n{candidate_json}\n\n"
     "Return JSON only.")
])
scoring_chain = scoring_prompt | CHAT_LLM | StrOutputParser()


comparison_prompt = ChatPromptTemplate.from_messages([
    ("system", AGENTIC_COMPARISON_PROMPT),
    ("human",
     "Question:\n{question}\n\n"
     "Strategy:\n{strategy}\n\n"
     "Seen players:\n{seen_players}\n\n"
     "Relevant memory:\n{memory}\n\n"
     "Write exactly 3 sentences.")
])
comparison_chain = comparison_prompt | CHAT_LLM | StrOutputParser()


named_comparison_prompt = ChatPromptTemplate.from_messages([
    ("system", AGENTIC_NAMED_COMPARISON_PROMPT),
    ("human",
     "Question:\n{question}\n\n"
     "Strategy:\n{strategy}\n\n"
     "Player A:\n{player_a_json}\n\n"
     "Player B:\n{player_b_json}\n\n"
     "Write exactly 3 sentences.")
])
named_comparison_chain = named_comparison_prompt | CHAT_LLM | StrOutputParser()


narrative_prompt = ChatPromptTemplate.from_messages([
    ("system", AGENTIC_NARRATIVE_PROMPT),
    ("human",
     "Question:\n{question}\n\n"
     "Team strategy / philosophy (may be empty):\n{strategy}\n\n"
     "Player profile:\n{profile_json}\n\n"
     "Stats (metric/value pairs):\n{stats_json}\n\n"
     "Write exactly 3 sentences.")
])
narrative_chain = narrative_prompt | CHAT_LLM | StrOutputParser()


followup_prompt = ChatPromptTemplate.from_messages([
    ("system", AGENTIC_FOLLOWUP_PROMPT),
    ("human",
     "Question:\n{question}\n\n"
     "Strategy:\n{strategy}\n\n"
     "Seen players:\n{seen_players}\n\n"
     "Relevant memory:\n{memory}\n\n"
     "Write exactly 3 sentences.")
])
followup_chain = followup_prompt | CHAT_LLM | StrOutputParser()


DEEPSEEK_INPUT_PRICE_PER_M = float(os.getenv("DEEPSEEK_INPUT_PRICE_PER_M", "0.14"))
DEEPSEEK_OUTPUT_PRICE_PER_M = float(os.getenv("DEEPSEEK_OUTPUT_PRICE_PER_M", "0.28"))
AGENTIC_FLOW_LOG = os.getenv("AGENTIC_FLOW_LOG", "1").lower() in {"1", "true", "yes", "on"}
PRO_LOOKUP_FLOW_LOG = os.getenv("PRO_LOOKUP_FLOW_LOG", "1").lower() in {"1", "true", "yes", "on"}


def _estimate_tokens(text: Any) -> int:
    if text is None:
        return 0
    raw = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return max(1, int(len(raw) / 4)) if raw else 0


def _new_trace() -> Dict[str, Any]:
    return {
        "agents": [],
        "tools": [],
        "flow": [],
        "context": {},
        "retrieval": {},
        "retrieval_debug": [],
        "selector": {},
        "selected": {},
        "fetched_options": [],
        "selector_options": [],
        "input_tokens": 0,
        "output_tokens": 0,
    }


def _trace_step(trace: Dict[str, Any], kind: str, name: str) -> None:
    trace["flow"].append(f"{kind}:{name}")
    bucket = "agents" if kind == "agent" else "tools"
    if name not in trace[bucket]:
        trace[bucket].append(name)


def _pro_lookup_log(event: str, payload: Dict[str, Any]) -> None:
    if not PRO_LOOKUP_FLOW_LOG:
        return
    try:
        body = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        body = str(payload)
    print(f"[pro_lookup_flow] event={event} {body}", flush=True)


def _compact_lookup_candidates(candidates: List[Dict[str, Any]], limit: int = 6) -> List[Dict[str, Any]]:
    return [
        {
            "index": candidate.get("index"),
            "name": candidate.get("name"),
            "team": candidate.get("team"),
            "league": candidate.get("league_name"),
            "nationality": candidate.get("nationality"),
            "position": candidate.get("position_name"),
            "match_count": candidate.get("match_count"),
            "stats_count": len(candidate.get("stats") or []),
        }
        for candidate in (candidates or [])[:limit]
    ]


def _trace_llm_cost(trace: Dict[str, Any], input_text: Any, output_text: Any) -> None:
    trace["input_tokens"] += _estimate_tokens(input_text)
    trace["output_tokens"] += _estimate_tokens(output_text)


def _trace_cost_usd(trace: Dict[str, Any]) -> float:
    return (
        (trace["input_tokens"] / 1_000_000) * DEEPSEEK_INPUT_PRICE_PER_M
        + (trace["output_tokens"] / 1_000_000) * DEEPSEEK_OUTPUT_PRICE_PER_M
    )


def _trace_translation_cost(
    trace: Optional[Dict[str, Any]],
    *,
    source_text: str,
    translated_text: str,
    direction: str,
) -> None:
    if trace is None:
        return
    system_prompt = (
        translate_tr_to_en_system_message
        if direction == "to_english"
        else translate_en_to_tr_system_message
    )
    _trace_llm_cost(trace, system_prompt + (source_text or ""), translated_text or "")


def _candidate_option_log(candidates: List[Dict[str, Any]], limit: int = 8) -> List[str]:
    options: List[str] = []
    for candidate in (candidates or [])[:limit]:
        options.append(
            f"{candidate.get('index')}:{candidate.get('name')}|"
            f"{candidate.get('league_name') or 'n/a'}|"
            f"{candidate.get('team') or 'n/a'}"
        )
    return options


def _log_trace(trace: Dict[str, Any], *, session_id: str, outcome: str) -> None:
    if not AGENTIC_FLOW_LOG:
        return
    flow_parts: List[str] = []
    for step in trace["flow"]:
        if flow_parts and flow_parts[-1].startswith(step + " x"):
            count = int(flow_parts[-1].rsplit(" x", 1)[1]) + 1
            flow_parts[-1] = f"{step} x{count}"
        elif flow_parts and flow_parts[-1] == step:
            flow_parts[-1] = f"{step} x2"
        else:
            flow_parts.append(step)

    context = trace.get("context") or {}
    retrieval = trace.get("retrieval") or {}
    retrieval_debug = trace.get("retrieval_debug") or []
    selector = trace.get("selector") or {}
    selected = trace.get("selected") or {}
    constraints = context.get("constraints") or {}
    fetched_options = trace.get("fetched_options") or []
    selector_options = trace.get("selector_options") or []
    reject_parts: List[str] = []
    for item in retrieval_debug[:4]:
        top_rejections = ",".join(f"{name}:{count}" for name, count in (item.get("top_rejections") or [])[:3])
        reject_parts.append(
            f"{item.get('pass')} raw={item.get('raw_count')} accepted={item.get('accepted_count')} "
            f"returned={item.get('returned_count')} rejects={top_rejections or 'none'}"
        )
    reject_text = " | ".join(reject_parts) if reject_parts else "none"
    print(
        "[agentic_flow] "
        f"session={session_id} outcome={outcome} "
        f"intent={context.get('intent', 'unknown')} "
        f"quality={context.get('quality_discovery_mode', False)} "
        f"generic_high_quality={context.get('initial_strong_club_default', False)} "
        f"premium={context.get('premium_only', False)} "
        f"target={context.get('target_team') or 'none'} "
        f"all_leagues={context.get('allow_all_selection_leagues', False)} "
        f"relaxation={context.get('constraint_relaxation', 'strict')} "
        f"fetched={retrieval.get('fetched_count', retrieval.get('candidate_count', 'n/a'))} "
        f"sent_to_selector={retrieval.get('sent_to_selector_count', 'n/a')} "
        f"selector_index={selector.get('selected_index', 'n/a')} "
        f"selection_mode={trace.get('selection_mode', 'n/a')} "
        f"selected={selected.get('name') or 'none'} "
        f"team={selected.get('team') or 'n/a'} "
        f"rating={selected.get('rating', 'n/a')} "
        f"potential={selected.get('potential', 'n/a')} "
        f"form={selected.get('form', 'n/a')} "
        f"retrieval_debug=[{reject_text}] "
        f"flow={' -> '.join(flow_parts) or 'none'} "
        f"total_search_cost_usd={_trace_cost_usd(trace):.6f} ",
        flush=True,
    )
    if constraints:
        print("[agentic_flow:constraints]", flush=True)
        for key in sorted(constraints):
            print(f"  {key}: {json.dumps(constraints.get(key), ensure_ascii=False, default=str)}", flush=True)
    if fetched_options:
        print("[agentic_flow:fetched_players]", flush=True)
        for option in fetched_options:
            print(f"  {option}", flush=True)
    if selector_options:
        print("[agentic_flow:selector_players]", flush=True)
        selected_prefix = f"{selector.get('selected_index')}:"
        for option in selector_options:
            marker = " <- selected" if selected_prefix != "None:" and option.startswith(selected_prefix) else ""
            print(f"  {option}{marker}", flush=True)

def _recent_memory_text(history_rows: list, limit: int = 8) -> str:
    rows = history_rows[-limit:] if history_rows else []
    parts: List[str] = []
    for row in rows:
        role = row.get("role", "unknown")
        content = (row.get("content") or "").strip()
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def _persist_turn(session_id: str, human_text: str, ai_text: str, payload: Optional[Dict[str, Any]] = None) -> None:
    stored_payload = payload if payload is not None else {"players": []}
    stored_ai_content = (
        "[[PAYLOAD_JSON]]\n"
        + json.dumps(stored_payload, ensure_ascii=False)
        + "\n[[/PAYLOAD_JSON]]"
        + "\n\n"
        + (ai_text or "")
    )
    db = get_db()
    try:
        append_chat_message(db, session_id, "human", human_text)
        append_chat_message(db, session_id, "ai", stored_ai_content)
    finally:
        db.close()


def _extract_payload_json(content: str) -> Dict[str, Any]:
    text = content or ""
    match = re.search(r"\[\[PAYLOAD_JSON\]\]\s*(\{[\s\S]*?\})\s*\[\[/PAYLOAD_JSON\]\]", text)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except Exception:
        return {}


def _last_agentic_constraints(history_rows: list) -> Dict[str, Any]:
    for row in reversed(history_rows or []):
        if row.get("role") != "ai":
            continue
        payload = _extract_payload_json(row.get("content") or "")
        constraints = payload.get("agentic_constraints")
        if isinstance(constraints, dict):
            return clean_constraints(constraints)
    return {}


def _constraint_update_resets(text: str) -> bool:
    lowered = (text or "").lower()
    return bool(re.search(r"\b(start over|reset|forget previous|ignore previous|new search|completely different)\b", lowered))


def _constraint_update_removes(text: str) -> bool:
    lowered = (text or "").lower()
    return bool(re.search(r"\b(remove|without|no longer|not anymore|instead of|rather than|any nationality|any league|any team|any age|any height|any weight)\b", lowered))


def _mentions_gender(text: str) -> bool:
    return bool(re.search(r"\b(male|female|woman|women|girl|girls|man|men|boy|boys|unknown gender)\b", (text or "").lower()))


def _infer_removed_stats(text: str) -> List[str]:
    lowered = (text or "").lower()
    spans: List[str] = []
    for pattern in [
        r"\binstead of\s+(.+?)(?:\s+i want|\s+with|\s+use|\s+give|\s+find|$)",
        r"\brather than\s+(.+?)(?:\s+i want|\s+with|\s+use|\s+give|\s+find|$)",
        r"\bwithout\s+(.+)$",
        r"\bremove\s+(.+)$",
        r"\bno longer\s+(.+)$",
    ]:
        match = re.search(pattern, lowered)
        if match:
            spans.append(match.group(1))
    stats: List[str] = []
    for span in spans:
        for metric in infer_preferred_stats_from_text(span):
            if metric not in stats:
                stats.append(metric)
    return stats


def _merge_turn_constraints(previous: Dict[str, Any], current: Dict[str, Any], question_text: str) -> Dict[str, Any]:
    previous = clean_constraints(previous)
    current = clean_constraints(current)
    if not previous or _constraint_update_resets(question_text):
        return current

    merged = dict(previous)
    text = question_text or ""
    lowered = text.lower()

    removable_keys = {
        "nationality": [r"\bany nationality\b", r"\bno nationality\b", r"\bremove nationality\b", r"\bwithout nationality\b"],
        "league": [r"\bany league\b", r"\bno league\b", r"\bremove league\b", r"\bwithout league\b"],
        "team": [r"\bany team\b", r"\bno team\b", r"\bremove team\b", r"\bwithout team\b"],
        "age_min": [r"\bany age\b", r"\bremove age\b", r"\bwithout age\b"],
        "age_max": [r"\bany age\b", r"\bremove age\b", r"\bwithout age\b"],
        "height_min": [r"\bany height\b", r"\bremove height\b", r"\bwithout height\b"],
        "height_max": [r"\bany height\b", r"\bremove height\b", r"\bwithout height\b"],
        "weight_min": [r"\bany weight\b", r"\bremove weight\b", r"\bwithout weight\b"],
        "weight_max": [r"\bany weight\b", r"\bremove weight\b", r"\bwithout weight\b"],
    }
    cleared_keys = set()
    for key, patterns in removable_keys.items():
        if any(re.search(pattern, lowered) for pattern in patterns):
            merged[key] = None
            cleared_keys.add(key)

    for key in [
        "gender", "position", "nationality", "league", "team",
        "age_min", "age_max", "height_min", "height_max", "weight_min", "weight_max",
    ]:
        if key == "gender" and not _mentions_gender(text):
            continue
        if key in cleared_keys:
            continue
        value = current.get(key)
        if value is not None:
            merged[key] = value

    for key in ["excluded_nationalities", "excluded_positions", "excluded_leagues", "excluded_teams"]:
        values: List[str] = []
        for value in [*(previous.get(key) or []), *(current.get(key) or [])]:
            text = str(value or "").strip()
            if text and text not in values:
                values.append(text)
        merged[key] = values[:5]

    removed_stats = set(_infer_removed_stats(text))
    preferred_stats: List[str] = []
    for metric in previous.get("preferred_stats") or []:
        if metric not in removed_stats and metric not in preferred_stats:
            preferred_stats.append(metric)
    for metric in current.get("preferred_stats") or []:
        if metric not in removed_stats and metric not in preferred_stats:
            preferred_stats.append(metric)
        if len(preferred_stats) >= 4:
            break
    merged["preferred_stats"] = preferred_stats

    current_requirements = current.get("stat_requirements") or []
    if current_requirements or _constraint_update_removes(text):
        merged["stat_requirements"] = current_requirements

    merged["notes"] = current.get("notes") or previous.get("notes") or ""
    return clean_constraints(merged)


def _translate_output_if_needed(text: str, lang: str, trace: Optional[Dict[str, Any]] = None) -> str:
    if not is_turkish(lang):
        return text
    try:
        translated = output_tr_translate_chain.invoke({"text": text}).strip()
        if trace is not None:
            _trace_step(trace, "agent", "translate_to_user_language")
            _trace_translation_cost(
                trace,
                source_text=text,
                translated_text=translated or text,
                direction="to_user_language",
            )
        return translated or text
    except Exception:
        return text


def _no_candidate_response(ctx, session_id: str, trace: Dict[str, Any]) -> Dict[str, Any]:
    message = (
        "I could not find a player matching those exact filters in the current database. "
        "Try broadening the club, nationality, position, or league constraint."
    )
    answer = _translate_output_if_needed(message, ctx.lang, trace)
    payload = {"players": []}
    _persist_turn(session_id, ctx.translated_question, message, payload)
    _trace_step(trace, "tool", "persist_memory")
    return {"answer": answer, "data": payload}


def _controller_decision(
    *,
    original_question: str,
    translated_question: str,
    strategy: Optional[str],
    seen_players: set[str],
    history_rows: list,
    trace: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        payload = {
            "original_question": original_question,
            "translated_question": translated_question,
            "strategy": strategy or "",
            "seen_players": ", ".join(sorted(seen_players)) if seen_players else "None",
            "recent_memory": _recent_memory_text(history_rows),
        }
        if trace is not None:
            _trace_step(trace, "agent", "controller")
        raw = controller_chain.invoke(payload)
        if trace is not None:
            _trace_llm_cost(trace, AGENTIC_CONTROLLER_PROMPT + json.dumps(payload, ensure_ascii=False), raw)
        return extract_json_object(raw)
    except Exception:
        return {}


def _constraint_decision(
    *,
    original_question: str,
    translated_question: str,
    strategy: Optional[str],
    recent_constraints: List[str],
    trace: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        payload = {
            "original_question": original_question,
            "translated_question": translated_question,
            "strategy": strategy or "",
            "recent_constraints": "\n".join(f"- {item}" for item in recent_constraints or []) or "None",
        }
        if trace is not None:
            _trace_step(trace, "agent", "constraint_extractor")
        raw = constraint_chain.invoke(payload)
        if trace is not None:
            _trace_llm_cost(trace, AGENTIC_CONSTRAINT_PROMPT + json.dumps(payload, ensure_ascii=False), raw)
        return clean_constraints(extract_json_object(raw))
    except Exception:
        return {}


def _score_candidate_with_ai(
    candidate: Dict[str, Any],
    *,
    question: str,
    strategy: Optional[str],
    trace: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    player_id = candidate.get("id")
    if player_id is not None:
        if trace is not None:
            _trace_step(trace, "agent", "scoring")
        db = get_db()
        try:
            potential_result = reveal_player_potential(db, player_id)
            form_result = reveal_player_form(db, player_id)
            return apply_ai_scores_to_candidate(candidate, {
                "potential": potential_result.get("potential"),
                "form": form_result.get("form"),
            })
        except Exception:
            pass
        finally:
            db.close()

    compact_candidate = {
        "name": candidate.get("name"),
        "age": candidate.get("age"),
        "height": candidate.get("height"),
        "weight": candidate.get("weight"),
        "nationality": candidate.get("nationality"),
        "team": candidate.get("team"),
        "league_name": candidate.get("league_name"),
        "position_name": candidate.get("position_name"),
        "match_count": candidate.get("match_count"),
        "rating": candidate.get("rating"),
        "stats": candidate.get("stats") or [],
    }
    payload = {
        "question": question,
        "strategy": strategy or "",
        "candidate_json": json.dumps(compact_candidate, ensure_ascii=False),
    }
    if trace is not None:
        _trace_step(trace, "agent", "scoring")
    raw = scoring_chain.invoke(payload)
    if trace is not None:
        _trace_llm_cost(trace, AGENTIC_SCORING_PROMPT + json.dumps(payload, ensure_ascii=False), raw)
    return apply_ai_scores_to_candidate(candidate, extract_json_object(raw))


def _resolve_direct_identity_with_ai(
    *,
    question: str,
    candidates: List[Dict[str, Any]],
    trace: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    candidate_list = format_candidates_for_selector(candidates, max_stats=6)
    payload = {
        "question": question,
        "candidate_list": candidate_list,
    }
    if trace is not None:
        _trace_step(trace, "agent", "identity_resolver")
    raw = identity_resolver_chain.invoke(payload)
    if trace is not None:
        _trace_llm_cost(trace, AGENTIC_IDENTITY_RESOLVER_PROMPT + json.dumps(payload, ensure_ascii=False), raw)
    data = extract_json_object(raw)
    try:
        selected_index = int(data.get("selected_index"))
    except Exception:
        selected_index = None
    indexed = {int(c["index"]): c for c in candidates if c.get("index") is not None}
    selected = indexed.get(selected_index) if selected_index is not None else None
    """
    if selected:
        print(
            "[chatbot_agentic_lookup] event=identity_resolver_output "
            + json.dumps({
                "selected_index": selected_index,
                "selected_name": selected.get("name"),
                "selected_team": selected.get("team"),
            }, ensure_ascii=False),
            flush=True,
        )
        
    """
    return selected


def _resolve_named_player_for_comparison(
    *,
    name: str,
    trace: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if trace is not None:
        _trace_step(trace, "tool", "comparison_candidate_lookup")
    candidates = fetch_direct_player_candidates_by_name(name)
    selected = _resolve_direct_identity_with_ai(
        question=name,
        candidates=candidates,
        trace=trace,
    )
    if selected:
        return selected
    if candidates:
        return candidates[0]
    if trace is not None:
        _trace_step(trace, "tool", "direct_db_lookup")
    return fetch_direct_player_candidate_by_name(name)


def _compact_comparison_player(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": candidate.get("name"),
        "age": candidate.get("age"),
        "height": candidate.get("height"),
        "weight": candidate.get("weight"),
        "nationality": candidate.get("nationality"),
        "team": candidate.get("team"),
        "league_name": candidate.get("league_name"),
        "position_name": candidate.get("position_name"),
        "match_count": candidate.get("match_count"),
        "rating": candidate.get("rating"),
        "potential": candidate.get("potential"),
        "form": candidate.get("form"),
        "stats": candidate.get("stats") or [],
    }


def _answer_named_comparison(
    *,
    question: str,
    translated_question: str,
    session_id: str,
    lang: str,
    strategy: Optional[str],
    player_names: List[str],
    trace: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if len(player_names) < 2:
        return None

    resolved: List[Dict[str, Any]] = []
    for name in player_names[:2]:
        candidate = _resolve_named_player_for_comparison(name=name, trace=trace)
        if not candidate:
            return None
        try:
            candidate = _score_candidate_with_ai(
                candidate,
                question=translated_question,
                strategy=strategy,
                trace=trace,
            )
        except Exception:
            pass
        resolved.append(candidate)

    payload = {
        "question": translated_question,
        "strategy": strategy or "",
        "player_a_json": json.dumps(_compact_comparison_player(resolved[0]), ensure_ascii=False),
        "player_b_json": json.dumps(_compact_comparison_player(resolved[1]), ensure_ascii=False),
    }
    if trace is not None:
        _trace_step(trace, "agent", "named_comparison")
    raw = named_comparison_chain.invoke(payload).strip()
    if trace is not None:
        _trace_llm_cost(trace, AGENTIC_NAMED_COMPARISON_PROMPT + json.dumps(payload, ensure_ascii=False), raw)
    answer = _translate_output_if_needed(raw, lang, trace)
    _persist_turn(session_id, translated_question, raw, {"players": []})
    if trace is not None:
        _trace_step(trace, "tool", "persist_memory")
        _log_trace(trace, session_id=session_id, outcome="named_comparison")
    return {"answer": answer, "data": {"players": []}}


def _choose_scored_candidate(
    *,
    selected_index: Optional[int],
    candidates: List[Dict[str, Any]],
    ctx,
    strategy: Optional[str],
    trace: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    ordered: List[Dict[str, Any]] = []
    indexed = {int(c["index"]): c for c in candidates if c.get("index") is not None}
    if selected_index in indexed:
        ordered.append(indexed[selected_index])
    ordered.extend(
        candidate
        for candidate in sorted(
            candidates,
            key=lambda c: (
                len(c.get("stats") or []),
                c.get("match_count") or 0,
                c.get("rating") or 0,
            ) if getattr(ctx, "quality_discovery_mode", False) else (
                c.get("rating") or 0,
                c.get("match_count") or 0,
                len(c.get("stats") or []),
            ),
            reverse=True,
        )
        if candidate.get("index") != selected_index
    )

    quality_mode = bool(getattr(ctx, "quality_discovery_mode", False) or getattr(ctx, "premium_only", False))
    valid_scored: List[Dict[str, Any]] = []
    scored_candidates: List[Dict[str, Any]] = []
    score_rejections: Dict[str, int] = {}
    scored_samples: List[Dict[str, Any]] = []

    def quality_key(candidate: Dict[str, Any]) -> tuple:
        return (
            candidate.get("potential") or 0,
            candidate.get("form") or 0,
            len(candidate.get("stats") or []),
            candidate.get("match_count") or 0,
            candidate.get("rating") or 0,
            1 if selected_index is not None and candidate.get("index") == selected_index else 0,
        )

    for candidate in ordered:
        try:
            scored = _score_candidate_with_ai(
                candidate,
                question=ctx.effective_query,
                strategy=strategy,
                trace=trace,
            )
        except Exception:
            score_rejections["scoring_exception"] = score_rejections.get("scoring_exception", 0) + 1
            continue
        scored_candidates.append(scored)
        rejection = validate_candidate(scored, ctx)
        sample = {
            "name": scored.get("name"),
            "team": scored.get("team"),
            "league": scored.get("league_name"),
            "age": scored.get("age"),
            "match_count": scored.get("match_count"),
            "stats_count": len(scored.get("stats") or []),
            "potential": scored.get("potential"),
            "form": scored.get("form"),
            "rejection": rejection,
        }
        if len(scored_samples) < 8:
            scored_samples.append(sample)
        if not rejection:
            valid_scored.append(scored)
            if not quality_mode:
                break
        else:
            score_rejections[rejection] = score_rejections.get(rejection, 0) + 1
    if not valid_scored:
        if scored_candidates:
            selected = max(scored_candidates, key=quality_key)
            if trace is not None:
                trace["selection_mode"] = "relaxed_best_available_after_validation"
            _quality_debug("scoring_validation", {
                "scored_count": len(scored_samples),
                "valid_count": 0,
                "selection_mode": "relaxed_best_available_after_validation",
                "selected": {
                    "name": selected.get("name"),
                    "team": selected.get("team"),
                    "league": selected.get("league_name"),
                    "rating": selected.get("rating"),
                    "potential": selected.get("potential"),
                    "form": selected.get("form"),
                    "stats_count": len(selected.get("stats") or []),
                },
                "top_rejections": sorted(score_rejections.items(), key=lambda item: item[1], reverse=True)[:5],
                "sample_scored": scored_samples,
            })
            return selected
        if getattr(ctx, "quality_discovery_mode", False) and scored_candidates:
            selected = max(scored_candidates, key=quality_key)
            if trace is not None:
                trace["selection_mode"] = "quality_relaxed_best_available"
            _quality_debug("scoring_validation", {
                "scored_count": len(scored_samples),
                "valid_count": 0,
                "selection_mode": "quality_relaxed_best_available",
                "selected": {
                    "name": selected.get("name"),
                    "team": selected.get("team"),
                    "league": selected.get("league_name"),
                    "rating": selected.get("rating"),
                    "potential": selected.get("potential"),
                    "form": selected.get("form"),
                    "stats_count": len(selected.get("stats") or []),
                },
                "top_rejections": sorted(score_rejections.items(), key=lambda item: item[1], reverse=True)[:5],
                "sample_scored": scored_samples,
            })
            return selected
        if getattr(ctx, "quality_discovery_mode", False) and ordered:
            selected = max(ordered, key=quality_key)
            if trace is not None:
                trace["selection_mode"] = "quality_relaxed_unscored_best_available"
            _quality_debug("scoring_validation", {
                "scored_count": len(scored_samples),
                "valid_count": 0,
                "selection_mode": "quality_relaxed_unscored_best_available",
                "selected": {
                    "name": selected.get("name"),
                    "team": selected.get("team"),
                    "league": selected.get("league_name"),
                    "rating": selected.get("rating"),
                    "stats_count": len(selected.get("stats") or []),
                },
                "top_rejections": sorted(score_rejections.items(), key=lambda item: item[1], reverse=True)[:5],
                "sample_scored": scored_samples,
            })
            return selected
        if getattr(ctx, "quality_discovery_mode", False):
            _quality_debug("scoring_validation", {
                "scored_count": len(scored_samples),
                "valid_count": 0,
                "top_rejections": sorted(score_rejections.items(), key=lambda item: item[1], reverse=True)[:5],
                "sample_scored": scored_samples,
            })
        return None

    selection_mode = "max_quality" if quality_mode else "selector_first_valid"
    ranked_for_log = sorted(valid_scored, key=quality_key, reverse=True) if quality_mode else valid_scored
    selected = max(valid_scored, key=quality_key) if quality_mode else valid_scored[0]
    if trace is not None:
        trace["selection_mode"] = selection_mode
    if getattr(ctx, "quality_discovery_mode", False):
        ordered_shortlist = []
        for idx, candidate in enumerate(ranked_for_log[:8], start=1):
            ordered_shortlist.append({
                "rank": idx,
                "selected": candidate is selected,
                "name": candidate.get("name"),
                "team": candidate.get("team"),
                "league": candidate.get("league_name"),
                "rating": candidate.get("rating"),
                "potential": candidate.get("potential"),
                "form": candidate.get("form"),
                "stats_count": len(candidate.get("stats") or []),
            })
        _quality_debug("scoring_validation", {
            "scored_count": len(valid_scored) + sum(score_rejections.values()),
            "valid_count": len(valid_scored),
            "selection_mode": selection_mode,
            "top_rejections": sorted(score_rejections.items(), key=lambda item: item[1], reverse=True)[:5],
            "ordered_shortlist": ordered_shortlist,
        })
    return selected


def _answer_seen_or_comparison(
    *,
    question: str,
    translated_question: str,
    session_id: str,
    lang: str,
    history_rows: list,
    seen_players: set[str],
    strategy: Optional[str],
    comparison: bool,
    trace: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if comparison:
        payload = {
            "question": translated_question,
            "strategy": strategy or "",
            "seen_players": ", ".join(sorted(seen_players)),
            "memory": _recent_memory_text(history_rows, limit=12),
        }
        if trace is not None:
            _trace_step(trace, "agent", "comparison")
        raw = comparison_chain.invoke(payload).strip()
        if trace is not None:
            _trace_llm_cost(trace, AGENTIC_COMPARISON_PROMPT + json.dumps(payload, ensure_ascii=False), raw)
        answer = _translate_output_if_needed(raw, lang, trace)
        _persist_turn(session_id, translated_question, raw, {"players": []})
        if trace is not None:
            _trace_step(trace, "tool", "persist_memory")
            _log_trace(trace, session_id=session_id, outcome="comparison")
        return {"answer": answer, "data": {"players": []}}

    payload = {
        "question": translated_question,
        "strategy": strategy or "",
        "seen_players": ", ".join(sorted(seen_players)),
        "memory": _recent_memory_text(history_rows, limit=12),
    }
    if trace is not None:
        _trace_step(trace, "agent", "seen_player_followup")
    raw = followup_chain.invoke(payload).strip()
    if trace is not None:
        _trace_llm_cost(trace, AGENTIC_FOLLOWUP_PROMPT + json.dumps(payload, ensure_ascii=False), raw)
    answer = _translate_output_if_needed(raw, lang, trace)
    _persist_turn(session_id, translated_question, raw, {"players": []})
    if trace is not None:
        _trace_step(trace, "tool", "persist_memory")
        _log_trace(trace, session_id=session_id, outcome="seen_player_followup")
    return {"answer": answer, "data": {"players": []}}


def answer_question(
    question: str,
    session_id: str = "default",
    strategy: Optional[str] = None,
) -> Dict[str, Any]:
    trace = _new_trace()
    lang, history_rows = get_session_state(session_id)
    _trace_step(trace, "tool", "load_memory")
    ai_msgs: List[AIMessage] = [
        AIMessage(content=row["content"])
        for row in history_rows
        if row.get("role") == "ai"
    ]
    seen_players = get_seen_players_from_history(ai_msgs)
    _trace_step(trace, "tool", "seen_players")

    original_question = question or ""
    translated_raw = translate_to_english_if_needed(original_question, lang)
    if is_turkish(lang):
        _trace_step(trace, "agent", "translate_to_english")
        _trace_translation_cost(
            trace,
            source_text=original_question,
            translated_text=translated_raw,
            direction="to_english",
        )

    if is_greeting_or_offtopic(translated_raw):
        answer = short_offtopic_response(lang)
        _persist_turn(session_id, translated_raw, answer, {"players": []})
        _trace_step(trace, "tool", "persist_memory")
        _log_trace(trace, session_id=session_id, outcome="greeting_or_offtopic")
        return {"answer": answer, "data": {"players": []}}

    _pro_lookup_log("input", {
        "session": session_id,
        "lang": lang,
        "original": original_question,
        "translated": translated_raw,
        "strategy_chars": len(strategy or ""),
        "seen_players_count": len(seen_players or []),
    })

    planner_data = _controller_decision(
        original_question=original_question,
        translated_question=translated_raw,
        strategy=strategy,
        seen_players=seen_players,
        history_rows=history_rows,
        trace=trace,
    )
    planner_intent = planner_data.get("intent")
    _pro_lookup_log("controller", {
        "intent": planner_intent,
        "effective_query": planner_data.get("effective_query"),
        "target_team": planner_data.get("target_team"),
        "comparison_players": planner_data.get("comparison_players"),
        "raw_keys": sorted(planner_data.keys()),
    })
    continuation_request = planner_intent == "alternative_recommendation" or is_generic_alternative_request(
        planner_data.get("effective_query") or translated_raw,
    )
    previous_constraints = _last_agentic_constraints(history_rows) if continuation_request else {}
    carried_constraints = (
        collect_recent_human_constraints(
            history_rows,
            is_generic_alternative_fn=is_generic_alternative_request,
            limit=3,
        )
        if continuation_request
        else []
    )
    constraints = _constraint_decision(
        original_question=original_question,
        translated_question=planner_data.get("effective_query") or translated_raw,
        strategy=strategy,
        recent_constraints=carried_constraints,
        trace=trace,
    )
    inferred_exclusions = infer_excluded_constraints_from_text(
        original_question,
        translated_raw,
        planner_data.get("effective_query") or "",
    )
    for key, values in inferred_exclusions.items():
        if values:
            constraints[key] = [*(constraints.get(key) or []), *values]
    constraints = clean_constraints(constraints)

    inferred_nationality = infer_nationality_from_text(
        original_question,
        translated_raw,
        planner_data.get("effective_query") or "",
        strategy or "",
    )
    if (
        inferred_nationality
        and not constraints.get("nationality")
        and inferred_nationality not in (constraints.get("excluded_nationalities") or [])
    ):
        constraints["nationality"] = inferred_nationality
    inferred_position = infer_position_from_text(
        original_question,
        translated_raw,
        planner_data.get("effective_query") or "",
        strategy or "",
    )
    if (
        inferred_position
        and not constraints.get("position")
        and inferred_position not in (constraints.get("excluded_positions") or [])
    ):
        constraints["position"] = inferred_position
    inferred_league = infer_league_from_text(
        original_question,
        translated_raw,
        planner_data.get("effective_query") or "",
        strategy or "",
    )
    if inferred_league and not constraints.get("league") and inferred_league not in (constraints.get("excluded_leagues") or []):
        constraints["league"] = inferred_league
    inferred_stats = infer_preferred_stats_from_text(
        original_question,
        translated_raw,
        planner_data.get("effective_query") or "",
        strategy or "",
        "\n".join(carried_constraints),
    )
    if inferred_stats:
        merged_stats = []
        for metric in [*(constraints.get("preferred_stats") or []), *inferred_stats]:
            if metric not in merged_stats:
                merged_stats.append(metric)
            if len(merged_stats) >= 4:
                break
        constraints["preferred_stats"] = merged_stats
    constraints = _merge_turn_constraints(previous_constraints, constraints, original_question)
    _pro_lookup_log("constraints", {
        "continuation_request": continuation_request,
        "carried_count": len(carried_constraints or []),
        "previous_constraint_keys": sorted(previous_constraints.keys()),
        "constraints": constraints,
    })
    _trace_step(trace, "tool", "build_context")
    ctx = build_agentic_context(
        original_question=original_question,
        translated_question=translated_raw,
        lang=lang,
        history_rows=history_rows,
        seen_players=seen_players,
        strategy=strategy,
        planner_data=planner_data,
        constraints=constraints,
    )
    trace["context"] = {
        "intent": ctx.intent,
        "effective_query": ctx.effective_query,
        "target_team": ctx.target_team,
        "direct_player_lookup": ctx.direct_player_lookup,
        "quality_discovery_mode": ctx.quality_discovery_mode,
        "initial_strong_club_default": ctx.initial_strong_club_default,
        "premium_only": ctx.premium_only,
        "allow_turkish": ctx.allow_turkish,
        "allow_non_senior": ctx.allow_non_senior,
        "allow_all_selection_leagues": ctx.allow_all_selection_leagues,
        "constraints": ctx.constraints,
        "constraint_relaxation": constraint_relaxation_label(ctx.constraint_relaxation_level),
    }
    _pro_lookup_log("context", {
        "intent": ctx.intent,
        "direct_player_lookup": ctx.direct_player_lookup,
        "effective_query": ctx.effective_query,
        "translated_question": ctx.translated_question,
        "target_team": ctx.target_team,
        "discovery_mode": ctx.discovery_mode,
        "quality_discovery_mode": ctx.quality_discovery_mode,
        "constraints": ctx.constraints,
    })
    if getattr(ctx, "quality_discovery_mode", False):
        _quality_debug("context", {
            "original_question": original_question,
            "translated_question": ctx.translated_question,
            "effective_query": ctx.effective_query,
            "intent": ctx.intent,
            "target_team": ctx.target_team,
            "quality_discovery_mode": ctx.quality_discovery_mode,
            "initial_strong_club_default": ctx.initial_strong_club_default,
            "premium_only": ctx.premium_only,
            "allow_turkish": ctx.allow_turkish,
            "allow_non_senior": ctx.allow_non_senior,
        })

    if ctx.intent in {"greeting_or_offtopic", "clarification"}:
        answer = short_offtopic_response(lang)
        _persist_turn(session_id, ctx.translated_question, answer, {"players": []})
        _trace_step(trace, "tool", "persist_memory")
        _log_trace(trace, session_id=session_id, outcome=ctx.intent)
        return {"answer": answer, "data": {"players": []}}

    if ctx.intent == "comparison":
        named_comparison = _answer_named_comparison(
            question=original_question,
            translated_question=ctx.translated_question,
            session_id=session_id,
            lang=lang,
            strategy=strategy,
            player_names=ctx.comparison_players,
            trace=trace,
        )
        if named_comparison:
            return named_comparison
        return _answer_seen_or_comparison(
            question=original_question,
            translated_question=ctx.translated_question,
            session_id=session_id,
            lang=lang,
            history_rows=history_rows,
            seen_players=seen_players,
            strategy=strategy,
            comparison=True,
            trace=trace,
        )

    if ctx.intent == "seen_player_followup":
        return _answer_seen_or_comparison(
            question=original_question,
            translated_question=ctx.translated_question,
            session_id=session_id,
            lang=lang,
            history_rows=history_rows,
            seen_players=seen_players,
            strategy=strategy,
            comparison=False,
            trace=trace,
        )

    try:
        if ctx.direct_player_lookup:
            _trace_step(trace, "tool", "direct_candidate_lookup")
            _pro_lookup_log("direct_lookup_start", {
                "query_used_for_db": ctx.effective_query,
                "original": original_question,
                "translated": ctx.translated_question,
                "constraints": ctx.constraints,
            })
            """
            print(
                "[chatbot_agentic_lookup] event=direct_lookup_agent_input "
                + json.dumps({
                    "session_id": session_id,
                    "original_question": original_question,
                    "translated_question": ctx.translated_question,
                    "effective_query": ctx.effective_query,
                    "intent": ctx.intent,
                    "direct_player_lookup": ctx.direct_player_lookup,
                }, ensure_ascii=False),
                flush=True,
            )
            """
            direct_candidates = fetch_direct_player_candidates_by_name(ctx.effective_query)
            _pro_lookup_log("direct_candidates", {
                "count": len(direct_candidates or []),
                "top": _compact_lookup_candidates(direct_candidates),
            })
            trace["retrieval"] = {
                "source": "direct_candidate_lookup",
                "fetched_count": len(direct_candidates or []),
            }
            trace["fetched_options"] = _candidate_option_log(direct_candidates)
            direct_candidate = _resolve_direct_identity_with_ai(
                question=ctx.effective_query,
                candidates=direct_candidates,
                trace=trace,
            )
            _pro_lookup_log("identity_resolver_selected", {
                "selected": {
                    "name": direct_candidate.get("name"),
                    "team": direct_candidate.get("team"),
                    "league": direct_candidate.get("league_name"),
                    "nationality": direct_candidate.get("nationality"),
                    "position": direct_candidate.get("position_name"),
                    "match_count": direct_candidate.get("match_count"),
                } if direct_candidate else None,
            })
            if not direct_candidate and direct_candidates:
                direct_candidate = direct_candidates[0]
                _pro_lookup_log("identity_resolver_fallback", {
                    "reason": "no_selected_index_from_resolver",
                    "fallback": {
                        "name": direct_candidate.get("name"),
                        "team": direct_candidate.get("team"),
                        "league": direct_candidate.get("league_name"),
                        "nationality": direct_candidate.get("nationality"),
                    },
                })
                """
                print(
                    "[chatbot_agentic_lookup] event=identity_resolver_fallback_to_top_candidate "
                    + json.dumps({
                        "candidate": {
                            "name": direct_candidate.get("name"),
                            "team": direct_candidate.get("team"),
                            "league_name": direct_candidate.get("league_name"),
                            "nationality": direct_candidate.get("nationality"),
                            "position_name": direct_candidate.get("position_name"),
                            "match_count": direct_candidate.get("match_count"),
                        }
                    }, ensure_ascii=False),
                    flush=True,
                )
                """
            if not direct_candidate and not direct_candidates:
                _trace_step(trace, "tool", "direct_db_lookup")
                direct_candidate = fetch_direct_player_candidate_by_name(ctx.effective_query)
                _pro_lookup_log("direct_db_fallback", {
                    "query_used_for_db": ctx.effective_query,
                    "selected": {
                        "name": direct_candidate.get("name"),
                        "team": direct_candidate.get("team"),
                        "league": direct_candidate.get("league_name"),
                        "nationality": direct_candidate.get("nationality"),
                    } if direct_candidate else None,
                })
                trace["retrieval"] = {
                    "source": "direct_db_lookup",
                    "fetched_count": 1 if direct_candidate else 0,
                }
                trace["fetched_options"] = _candidate_option_log([direct_candidate] if direct_candidate else [])
            if direct_candidate:
                _pro_lookup_log("direct_lookup_final", {
                    "selected": {
                        "name": direct_candidate.get("name"),
                        "team": direct_candidate.get("team"),
                        "league": direct_candidate.get("league_name"),
                        "nationality": direct_candidate.get("nationality"),
                        "position": direct_candidate.get("position_name"),
                        "rating": direct_candidate.get("rating"),
                        "stats_count": len(direct_candidate.get("stats") or []),
                    },
                })
                """
                print(
                    "[chatbot_agentic_lookup] event=direct_lookup_agent_output "
                    + json.dumps({
                        "source": "db",
                        "candidate": {
                            "name": direct_candidate.get("name"),
                            "team": direct_candidate.get("team"),
                            "league_name": direct_candidate.get("league_name"),
                            "nationality": direct_candidate.get("nationality"),
                            "position_name": direct_candidate.get("position_name"),
                            "match_count": direct_candidate.get("match_count"),
                            "stats_count": len(direct_candidate.get("stats") or []),
                        },
                    }, ensure_ascii=False),
                    flush=True,
                )
                """
                candidates = [direct_candidate]
                candidate_docs = []
            else:
                """
                print(
                    "[chatbot_agentic_lookup] event=direct_lookup_agent_output "
                    + json.dumps({"source": "db", "candidate": None, "fallback": "shared_retriever"}, ensure_ascii=False),
                    flush=True,
                )
                """
                _trace_step(trace, "tool", "shared_retriever")
                raw_docs = SHARED_RETRIEVER.invoke(ctx.effective_query)
                candidate_docs = list(raw_docs or [])[:12]
                trace["retrieval"] = {
                    "source": "shared_retriever",
                    "fetched_count": len(candidate_docs or []),
                }
                """
                print(
                    "[chatbot_agentic_lookup] event=shared_retriever_after "
                    + json.dumps({
                        "query": ctx.effective_query,
                        "doc_count": len(candidate_docs),
                        "sample_docs": [
                            {
                                "player_name": (doc.metadata or {}).get("player_name") or (doc.metadata or {}).get("name"),
                                "team_name": (doc.metadata or {}).get("team_name") or (doc.metadata or {}).get("team"),
                                "nationality_name": (doc.metadata or {}).get("nationality_name") or (doc.metadata or {}).get("nationality"),
                                "position_name": (doc.metadata or {}).get("position_name") or (doc.metadata or {}).get("position"),
                            }
                            for doc in candidate_docs[:10]
                        ],
                    }, ensure_ascii=False),
                    flush=True,
                )
                """
                candidates = []
        else:
            _pro_lookup_log("filtered_retriever_start", {
                "reason": "not_direct_player_lookup",
                "intent": ctx.intent,
                "effective_query": ctx.effective_query,
                "constraints": ctx.constraints,
            })
            _trace_step(trace, "tool", "filtered_retriever")
            _, candidate_docs = build_filtered_retriever_agentic(
                ctx,
                CANDIDATE_RETRIEVER,
                BROAD_CANDIDATE_RETRIEVER,
            )
            trace["retrieval"] = {
                "source": "filtered_retriever",
                "fetched_count": len(candidate_docs or []),
            }
            trace["retrieval_debug"] = list(getattr(ctx, "retrieval_debug", []) or [])
            trace["context"]["constraint_relaxation"] = constraint_relaxation_label(ctx.constraint_relaxation_level)
            _pro_lookup_log("filtered_retriever_result", {
                "doc_count": len(candidate_docs or []),
                "relaxation": constraint_relaxation_label(ctx.constraint_relaxation_level),
                "debug": trace["retrieval_debug"][:3],
                "top_docs": [
                    {
                        "player_name": (doc.metadata or {}).get("player_name") or (doc.metadata or {}).get("name"),
                        "team": (doc.metadata or {}).get("team_name") or (doc.metadata or {}).get("team"),
                        "league": (doc.metadata or {}).get("league_name") or (doc.metadata or {}).get("league"),
                        "nationality": (doc.metadata or {}).get("nationality_name") or (doc.metadata or {}).get("nationality"),
                        "position": (doc.metadata or {}).get("position_name") or (doc.metadata or {}).get("position"),
                    }
                    for doc in list(candidate_docs or [])[:6]
                ],
            })
            candidates = []

        if not candidate_docs and not candidates:
            _log_trace(
                trace,
                session_id=session_id,
                outcome="no_candidates_quality_relaxed" if ctx.quality_discovery_mode else "no_candidates",
            )
            return _no_candidate_response(ctx, session_id, trace)

        if not candidates:
            _trace_step(trace, "tool", "candidate_builder")
            candidates = [doc_to_candidate(doc, idx) for idx, doc in enumerate(candidate_docs, start=1)]
        else:
            _trace_step(trace, "tool", "candidate_builder")
        if not trace.get("fetched_options"):
            trace["fetched_options"] = _candidate_option_log(candidates)
        if not ctx.premium_only and not ctx.direct_player_lookup:
            random.SystemRandom().shuffle(candidates)
            for idx, candidate in enumerate(candidates, start=1):
                candidate["index"] = idx
        trace["selector_options"] = _candidate_option_log(candidates)
        trace["retrieval"]["sent_to_selector_count"] = len(candidates or [])
        candidate_list = format_candidates_for_selector(candidates)
        selector_payload = {
            "question": ctx.effective_query,
            "strategy": strategy or "",
            "target_team": "" if ctx.direct_player_lookup else (ctx.target_team or ""),
            "premium_only": "yes" if ctx.premium_only else "no",
            "constraints_json": json.dumps({
                "constraints": ctx.constraints,
                "relaxation": constraint_relaxation_label(ctx.constraint_relaxation_level),
            }, ensure_ascii=False),
            "seen_players": ", ".join(sorted(seen_players)) if seen_players else "None",
            "candidate_list": candidate_list,
        }
        _trace_step(trace, "agent", "selector")
        selector_raw = selector_chain.invoke(selector_payload)
        _trace_llm_cost(trace, AGENTIC_SELECTOR_PROMPT + json.dumps(selector_payload, ensure_ascii=False), selector_raw)
        selector_data = extract_json_object(selector_raw)
        selected_index = selector_data.get("selected_index")
        try:
            selected_index = int(selected_index) if selected_index is not None else None
        except Exception:
            selected_index = None
        trace["selector"] = {
            "selected_index": selected_index,
            "player_name": selector_data.get("player_name"),
            "confidence": selector_data.get("confidence"),
            "risk_flags": selector_data.get("risk_flags") or [],
        }

        selected = _choose_scored_candidate(
            selected_index=selected_index,
            candidates=candidates,
            ctx=ctx,
            strategy=strategy,
            trace=trace,
        )
        if not selected:
            _trace_step(trace, "tool", "legacy_fallback")
            _log_trace(
                trace,
                session_id=session_id,
                outcome=(
                    "fallback_no_valid_scored_candidate_quality_relaxed"
                    if ctx.quality_discovery_mode
                    else "fallback_no_valid_scored_candidate"
                ),
            )
            return legacy_answer_question(original_question, session_id=session_id, strategy=strategy)

        _trace_step(trace, "tool", "payload_builder")
        trace["selected"] = {
            "name": selected.get("name"),
            "team": selected.get("team"),
            "rating": selected.get("rating"),
            "potential": selected.get("potential"),
            "form": selected.get("form"),
            "match_count": selected.get("match_count"),
            "stats_count": len(selected.get("stats") or []),
        }
        payload, new_names = build_payload_from_candidate(selected, seen_players)
        payload["agentic_constraints"] = ctx.constraints

        if not new_names and not ctx.direct_player_lookup:
            if ctx.quality_discovery_mode:
                trace["selection_mode"] = trace.get("selection_mode") or "quality_duplicate_allowed"
                trace["selected"]["duplicate_allowed"] = True
            else:
                _trace_step(trace, "tool", "legacy_fallback")
                _log_trace(trace, session_id=session_id, outcome="fallback_duplicate_candidate")
                return legacy_answer_question(original_question, session_id=session_id, strategy=strategy)

        p0 = (payload.get("players") or [None])[0] or {}
        profile_meta = p0.get("meta") or {}
        stats = p0.get("stats") or selected.get("stats") or []
        profile_json = json.dumps({
            "name": p0.get("name") or selected.get("name"),
            "target_team_fit_context": ctx.target_team or None,
            "fit_question": bool(ctx.direct_player_lookup and ctx.target_team),
            **profile_meta,
        }, ensure_ascii=False)
        stats_json = json.dumps(stats, ensure_ascii=False)
        narrative_payload = {
            "question": ctx.translated_question,
            "strategy": strategy or "",
            "profile_json": profile_json,
            "stats_json": stats_json,
        }
        _trace_step(trace, "agent", "final_narrative")
        memory_out = narrative_chain.invoke(narrative_payload).strip()
        _trace_llm_cost(trace, AGENTIC_NARRATIVE_PROMPT + json.dumps(narrative_payload, ensure_ascii=False), memory_out)
        answer = _translate_output_if_needed(memory_out, lang, trace)
        _persist_turn(session_id, ctx.translated_question, memory_out, payload)
        _trace_step(trace, "tool", "persist_memory")
        _log_trace(trace, session_id=session_id, outcome="agentic_success")
        return {"answer": answer, "data": payload}

    except Exception as exc:
        _log_trace(trace, session_id=session_id, outcome=f"agentic_exception:{type(exc).__name__}:{str(exc)[:120]}")
        raise
