from typing import Any, Dict, List, Optional
import json
import os
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
    AGENTIC_CONTROLLER_PROMPT,
    AGENTIC_FOLLOWUP_PROMPT,
    AGENTIC_IDENTITY_RESOLVER_PROMPT,
    AGENTIC_NAMED_COMPARISON_PROMPT,
    AGENTIC_NARRATIVE_PROMPT,
    AGENTIC_SCORING_PROMPT,
    AGENTIC_SELECTOR_PROMPT,
)
from chatbot_module.tools import get_seen_players_from_history, is_turkish
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
    _is_transfer_fallback_club_strict,
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


selector_prompt = ChatPromptTemplate.from_messages([
    ("system", AGENTIC_SELECTOR_PROMPT),
    ("human",
     "User request:\n{question}\n\n"
     "Strategy:\n{strategy}\n\n"
     "Target team, if any:\n{target_team}\n\n"
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
        "input_tokens": 0,
        "output_tokens": 0,
    }


def _trace_step(trace: Dict[str, Any], kind: str, name: str) -> None:
    trace["flow"].append(f"{kind}:{name}")
    bucket = "agents" if kind == "agent" else "tools"
    if name not in trace[bucket]:
        trace[bucket].append(name)


def _trace_llm_cost(trace: Dict[str, Any], input_text: Any, output_text: Any) -> None:
    trace["input_tokens"] += _estimate_tokens(input_text)
    trace["output_tokens"] += _estimate_tokens(output_text)


def _trace_cost_usd(trace: Dict[str, Any]) -> float:
    return (
        (trace["input_tokens"] / 1_000_000) * DEEPSEEK_INPUT_PRICE_PER_M
        + (trace["output_tokens"] / 1_000_000) * DEEPSEEK_OUTPUT_PRICE_PER_M
    )


def _log_trace(trace: Dict[str, Any], *, session_id: str, outcome: str) -> None:
    flow_parts: List[str] = []
    for step in trace["flow"]:
        if flow_parts and flow_parts[-1].startswith(step + " x"):
            count = int(flow_parts[-1].rsplit(" x", 1)[1]) + 1
            flow_parts[-1] = f"{step} x{count}"
        elif flow_parts and flow_parts[-1] == step:
            flow_parts[-1] = f"{step} x2"
        else:
            flow_parts.append(step)
    """
    print(
        "[chatbot_agentic] "
        f"session={session_id} outcome={outcome} "
        f"flow={' -> '.join(flow_parts) or 'none'} "
        f"agents={','.join(trace['agents']) or 'none'} "
        f"tools={','.join(trace['tools']) or 'none'} "
        f"est_tokens_in={trace['input_tokens']} est_tokens_out={trace['output_tokens']} "
        f"est_cost_usd={_trace_cost_usd(trace):.6f} "
        f"pricing=input:${DEEPSEEK_INPUT_PRICE_PER_M}/1M,output:${DEEPSEEK_OUTPUT_PRICE_PER_M}/1M",
        flush=True,
    )
    """

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


def _translate_output_if_needed(text: str, lang: str) -> str:
    if not is_turkish(lang):
        return text
    try:
        translated = output_tr_translate_chain.invoke({"text": text}).strip()
        return translated or text
    except Exception:
        return text


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
                "reason": data.get("reason"),
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
    answer = _translate_output_if_needed(raw, lang)
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

    valid_scored: List[Dict[str, Any]] = []
    score_rejections: Dict[str, int] = {}
    scored_samples: List[Dict[str, Any]] = []
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
        else:
            score_rejections[rejection] = score_rejections.get(rejection, 0) + 1
    if not valid_scored:
        if getattr(ctx, "quality_discovery_mode", False):
            _quality_debug("scoring_validation", {
                "scored_count": len(scored_samples),
                "valid_count": 0,
                "top_rejections": sorted(score_rejections.items(), key=lambda item: item[1], reverse=True)[:5],
                "sample_scored": scored_samples,
            })
        return None

    def quality_key(candidate: Dict[str, Any]) -> tuple:
        return (
            candidate.get("potential") or 0,
            candidate.get("form") or 0,
            len(candidate.get("stats") or []),
            candidate.get("match_count") or 0,
            candidate.get("rating") or 0,
            1 if selected_index is not None and candidate.get("index") == selected_index else 0,
        )

    selection_mode = "max_quality"
    ranked_for_log = sorted(valid_scored, key=quality_key, reverse=True)
    if (
        getattr(ctx, "quality_discovery_mode", False)
        and getattr(ctx, "target_team", None)
        and not getattr(ctx, "initial_strong_club_default", False)
    ):
        selection_mode = "realistic_target_varied"
        non_strong_source = [
            candidate
            for candidate in valid_scored
            if not _is_transfer_fallback_club_strict(candidate.get("team"))
        ] or valid_scored
        max_stats = max(len(candidate.get("stats") or []) for candidate in non_strong_source)
        coverage_band = [
            candidate
            for candidate in non_strong_source
            if len(candidate.get("stats") or []) >= max_stats - 1
        ]
        ranked = sorted(
            coverage_band,
            key=lambda candidate: (
                candidate.get("potential") or 0,
                candidate.get("form") or 0,
                candidate.get("rating") or 0,
                candidate.get("match_count") or 0,
            ),
        )
        if len(ranked) <= 2:
            selected = ranked[len(ranked) // 2]
        else:
            lower = max(0, len(ranked) // 3)
            upper = max(lower + 1, (2 * len(ranked)) // 3)
            realistic_band = ranked[lower:upper] or ranked
            target_key = sum(ord(ch) for ch in str(getattr(ctx, "target_team", "") or "").lower())
            selected = realistic_band[target_key % len(realistic_band)]
        ranked_for_log = ranked
    else:
        selected = max(valid_scored, key=quality_key)
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
        answer = _translate_output_if_needed(raw, lang)
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
    answer = _translate_output_if_needed(raw, lang)
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
        _trace_llm_cost(trace, original_question, translated_raw)

    if is_greeting_or_offtopic(translated_raw):
        answer = short_offtopic_response(lang)
        _persist_turn(session_id, translated_raw, answer, {"players": []})
        _trace_step(trace, "tool", "persist_memory")
        _log_trace(trace, session_id=session_id, outcome="greeting_or_offtopic")
        return {"answer": answer, "data": {"players": []}}

    planner_data = _controller_decision(
        original_question=original_question,
        translated_question=translated_raw,
        strategy=strategy,
        seen_players=seen_players,
        history_rows=history_rows,
        trace=trace,
    )
    _trace_step(trace, "tool", "build_context")
    ctx = build_agentic_context(
        original_question=original_question,
        translated_question=translated_raw,
        lang=lang,
        history_rows=history_rows,
        seen_players=seen_players,
        strategy=strategy,
        planner_data=planner_data,
    )
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
            direct_candidate = _resolve_direct_identity_with_ai(
                question=ctx.effective_query,
                candidates=direct_candidates,
                trace=trace,
            )
            if not direct_candidate and direct_candidates:
                direct_candidate = direct_candidates[0]
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
            if direct_candidate:
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
            _trace_step(trace, "tool", "filtered_retriever")
            _, candidate_docs = build_filtered_retriever_agentic(
                ctx,
                CANDIDATE_RETRIEVER,
                BROAD_CANDIDATE_RETRIEVER,
            )
            candidates = []

        if not candidate_docs and not candidates:
            if ctx.quality_discovery_mode:
                answer = "I could not find a player who satisfies the quality thresholds for that request."
                answer = _translate_output_if_needed(answer, lang)
                _persist_turn(session_id, ctx.translated_question, answer, {"players": []})
                _trace_step(trace, "tool", "persist_memory")
                _log_trace(trace, session_id=session_id, outcome="no_quality_candidates")
                return {"answer": answer, "data": {"players": []}}
            _trace_step(trace, "tool", "legacy_fallback")
            _log_trace(trace, session_id=session_id, outcome="fallback_no_candidates")
            return legacy_answer_question(original_question, session_id=session_id, strategy=strategy)

        if not candidates:
            _trace_step(trace, "tool", "candidate_builder")
            candidates = [doc_to_candidate(doc, idx) for idx, doc in enumerate(candidate_docs, start=1)]
        else:
            _trace_step(trace, "tool", "candidate_builder")
        candidate_list = format_candidates_for_selector(candidates)
        selector_payload = {
            "question": ctx.effective_query,
            "strategy": strategy or "",
            "target_team": ctx.target_team or "",
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

        selected = _choose_scored_candidate(
            selected_index=selected_index,
            candidates=candidates,
            ctx=ctx,
            strategy=strategy,
            trace=trace,
        )
        if not selected:
            if ctx.quality_discovery_mode:
                answer = "I could not find a player who satisfies the quality thresholds for that request."
                answer = _translate_output_if_needed(answer, lang)
                _persist_turn(session_id, ctx.translated_question, answer, {"players": []})
                _trace_step(trace, "tool", "persist_memory")
                _log_trace(trace, session_id=session_id, outcome="no_valid_quality_candidate")
                return {"answer": answer, "data": {"players": []}}
            _trace_step(trace, "tool", "legacy_fallback")
            _log_trace(trace, session_id=session_id, outcome="fallback_no_valid_scored_candidate")
            return legacy_answer_question(original_question, session_id=session_id, strategy=strategy)

        _trace_step(trace, "tool", "payload_builder")
        payload, new_names = build_payload_from_candidate(selected, seen_players)

        if not new_names and not ctx.direct_player_lookup:
            if ctx.quality_discovery_mode:
                answer = "I could not find a different player who satisfies the quality thresholds for that request."
                answer = _translate_output_if_needed(answer, lang)
                _persist_turn(session_id, ctx.translated_question, answer, {"players": []})
                _trace_step(trace, "tool", "persist_memory")
                _log_trace(trace, session_id=session_id, outcome="no_new_quality_candidate")
                return {"answer": answer, "data": {"players": []}}
            _trace_step(trace, "tool", "legacy_fallback")
            _log_trace(trace, session_id=session_id, outcome="fallback_duplicate_candidate")
            return legacy_answer_question(original_question, session_id=session_id, strategy=strategy)

        p0 = (payload.get("players") or [None])[0] or {}
        profile_meta = p0.get("meta") or {}
        stats = p0.get("stats") or selected.get("stats") or []
        profile_json = json.dumps({
            "name": p0.get("name") or selected.get("name"),
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
        answer = _translate_output_if_needed(memory_out, lang)
        if is_turkish(lang):
            _trace_step(trace, "agent", "translate_to_user_language")
            _trace_llm_cost(trace, memory_out, answer)
        _persist_turn(session_id, ctx.translated_question, memory_out, payload)
        _trace_step(trace, "tool", "persist_memory")
        _log_trace(trace, session_id=session_id, outcome="agentic_success")
        return {"answer": answer, "data": payload}

    except Exception as exc:
        _trace_step(trace, "tool", "legacy_fallback")
        _log_trace(trace, session_id=session_id, outcome=f"fallback_exception:{type(exc).__name__}:{str(exc)[:120]}")
        fallback = legacy_answer_question(original_question, session_id=session_id, strategy=strategy)
        if "error" not in fallback:
            fallback["agentic_fallback_reason"] = str(exc)
        return fallback
