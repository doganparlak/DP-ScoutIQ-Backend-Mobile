from typing import Dict, Any, Optional, List
from dotenv import load_dotenv
load_dotenv()
from langchain_deepseek import ChatDeepSeek
from langchain_core.prompts import ChatPromptTemplate
from langchain_classic.memory import ConversationBufferMemory
from langchain_classic.chains import ConversationalRetrievalChain
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.retrievers import BaseRetriever
import warnings
import json
warnings.filterwarnings("ignore", category=DeprecationWarning, module="langchain")


from api_module.utilities import (
    get_db,
    get_session_language,
    append_chat_message,
    load_chat_messages
)
from chatbot_module.prompts import (
    system_message,
    meta_parser_system_prompt,
    translate_tr_to_en_system_message,
    translate_en_to_tr_system_message,
    interpretation_system_prompt
)
from chatbot_module.tools import (
    get_seen_players_from_history, 
    filter_players_by_seen,
    strip_meta_stats_text,
    compose_selection_preamble,
    is_turkish,
    is_same_club,
    is_turkish_nationality,
    is_disallowed_turkish_club,
    is_likely_turkish_name,
    request_allows_turkish_entities,
    request_allows_non_senior_squads,
    is_premium_request,
    is_weak_generic_suggestion_request,
    is_premium_allowed_club,
    is_transfer_fallback_club,
    is_generic_alternative_request,
    get_candidate_rejection_reason,
    has_required_discovery_fields,
    player_matches_requested_position,
    summarize_doc_candidate,
    build_pass2_query,
    build_pass3_query,
    collect_recent_human_constraints,
    extract_target_team_from_question,
    rewrite_position_reference_phrases,
    strip_target_team_from_question,
)
from chatbot_module.tools_extensions import (
    parse_player_meta_new,
    build_player_payload_new
)

# === Load Vectorstore ===
from chatbot_module.vectorstore_small import get_retriever
# === QA Chain with RAG & Memory ===



CHAT_LLM = ChatDeepSeek(
    model="deepseek-chat",
    temperature=0.3,
)

PARSER_LLM = ChatDeepSeek(
    model="deepseek-chat",
    temperature=0,   # keep it deterministic for JSON-style parsing
)

TRANSLATE_LLM = ChatDeepSeek(
    model="deepseek-chat",
    temperature=0,
)

SHARED_RETRIEVER = get_retriever(k=12, filter=None)
CANDIDATE_RETRIEVER = get_retriever(k=40, filter=None)
BROAD_CANDIDATE_RETRIEVER = get_retriever(k=60, filter=None)
FILTERED_CONTEXT_DOCS = 10
CHARS_TO_TOKENS = 0.3
DEEPSEEK_INPUT_COST_PER_MTOK = 0.28
DEEPSEEK_OUTPUT_COST_PER_MTOK = 0.42
EMBEDDING_INPUT_COST_PER_MTOK = 0.02
INTERPRETATION_OUTPUT_CHARS = 800


def _estimate_tokens_from_text(*parts: Optional[str]) -> float:
    total_chars = sum(len(part or "") for part in parts)
    return total_chars * CHARS_TO_TOKENS


def _estimate_input_cost_usd(tokens: float) -> float:
    return (tokens / 1_000_000.0) * DEEPSEEK_INPUT_COST_PER_MTOK


def _estimate_output_cost_usd(tokens: float) -> float:
    return (tokens / 1_000_000.0) * DEEPSEEK_OUTPUT_COST_PER_MTOK


def _estimate_embedding_cost_usd(text: Optional[str]) -> float:
    tokens = _estimate_tokens_from_text(text)
    return (tokens / 1_000_000.0) * EMBEDDING_INPUT_COST_PER_MTOK


def _log_cost_step(
    cost_tracker: Dict[str, float],
    step: str,
    amount_usd: float,
    note: str,
) -> None:
    cost_tracker["total_usd"] = cost_tracker.get("total_usd", 0.0) + amount_usd
    print(
        f"[cost] step='{step}', est_usd='{amount_usd:.6f}', running_total_usd='{cost_tracker['total_usd']:.6f}', {note}",
        flush=True,
    )


class StaticDocsRetriever(BaseRetriever):
    docs: List[Document]

    def _get_relevant_documents(self, query: str) -> List[Document]:
        return list(self.docs)

    async def _aget_relevant_documents(self, query: str) -> List[Document]:
        return list(self.docs)

def build_filtered_retriever(
    query: str,
    target_team: Optional[str],
    *,
    prefer_fallback_clubs: bool = False,
    require_complete_discovery_fields: bool = False,
    cost_tracker: Optional[Dict[str, float]] = None,
) -> BaseRetriever:
    allow_turkish = request_allows_turkish_entities(query)
    allow_non_senior = request_allows_non_senior_squads(query)
    premium_only = is_premium_request(query)
    success_pass_label: Optional[str] = None

    def _filter_docs(
        raw_docs: List[Document],
        active_query: str,
        pass_label: str,
        *,
        restrict_to_fallback_clubs: bool = False,
    ) -> List[Document]:
        # print(
        #     f"[selection] Filtering candidates ({pass_label}) for query='{active_query}', target_team='{target_team}', "
        #     f"allow_turkish='{allow_turkish}', allow_non_senior='{allow_non_senior}', premium_only='{premium_only}', raw_doc_count='{len(raw_docs)}'",
        #     flush=True,
        # )
        filtered_docs: List[Document] = []
        seen_names = set()
        for idx, doc in enumerate(raw_docs, start=1):
            md = doc.metadata or {}
            player_name = str(md.get("player_name") or md.get("name") or "").strip()
            team_name = str(md.get("team_name") or md.get("team") or md.get("club") or "").strip()
            nationality = str(md.get("nationality_name") or md.get("nationality") or md.get("country") or "").strip()
            position_name = str(md.get("position_name") or md.get("position") or "").strip()
            summary = summarize_doc_candidate(doc)

            rejection_reason = get_candidate_rejection_reason(
                player_name,
                team_name,
                nationality,
                target_team=target_team,
                allow_turkish=allow_turkish,
                allow_non_senior=allow_non_senior,
                premium_only=premium_only,
            )
            if rejection_reason:
                if rejection_reason == "premium club restriction":
                    # print(
                    #     f"[selection] REJECT {pass_label} raw_doc#{idx}: premium club restriction "
                    #     f"(premium_allowed='{is_premium_allowed_club(team_name)}') -> {summary}",
                    #     flush=True,
                    # )
                    pass
                elif rejection_reason == "Turkish exclusion":
                    # print(
                    #     f"[selection] REJECT {pass_label} raw_doc#{idx}: Turkish exclusion "
                    #     f"(club='{is_disallowed_turkish_club(team_name)}', nationality='{is_turkish_nationality(nationality)}', "
                    #     f"name='{is_likely_turkish_name(player_name)}') -> {summary}",
                    #     flush=True,
                    # )
                    pass
                else:
                    # print(f"[selection] REJECT {pass_label} raw_doc#{idx}: {rejection_reason} -> {summary}", flush=True)
                    pass
                continue

            if restrict_to_fallback_clubs and not is_transfer_fallback_club(team_name):
                continue

            if require_complete_discovery_fields and not has_required_discovery_fields(team_name, position_name):
                continue

            position_match_ok, requested_position_groups, player_position_groups = player_matches_requested_position(
                active_query,
                position_name,
                [position_name] if position_name else [],
            )
            if not position_match_ok:
                # print(
                #     f"[selection] REJECT {pass_label} raw_doc#{idx}: position mismatch "
                #     f"(requested='{sorted(requested_position_groups) if requested_position_groups else []}', "
                #     f"player='{sorted(player_position_groups) if player_position_groups else []}') -> {summary}",
                #     flush=True,
                # )
                continue

            dedupe_key = (player_name or doc.page_content[:80]).strip().lower()
            if dedupe_key in seen_names:
                # print(f"[selection] REJECT {pass_label} raw_doc#{idx}: duplicate candidate key='{dedupe_key}' -> {summary}", flush=True)
                continue
            seen_names.add(dedupe_key)
            filtered_docs.append(doc)
            # print(f"[selection] KEEP {pass_label} raw_doc#{idx}: {summary}", flush=True)
            if len(filtered_docs) >= FILTERED_CONTEXT_DOCS:
                break
        return filtered_docs

    raw_docs = CANDIDATE_RETRIEVER.invoke(query or "")
    if cost_tracker is not None:
        _log_cost_step(
            cost_tracker,
            "retrieval_pass1",
            _estimate_embedding_cost_usd(query),
            f"query_chars='{len(query or '')}', retriever_k='40'",
        )
    filtered_docs = _filter_docs(
        raw_docs,
        query,
        "pass1",
        restrict_to_fallback_clubs=prefer_fallback_clubs,
    ) if raw_docs else []
    if filtered_docs:
        success_pass_label = "pass1"
    alt_query = query
    pass3_query = query

    if not filtered_docs and target_team:
        alt_query = strip_target_team_from_question(query, target_team)
        if alt_query != query:
            pass2_query = build_pass2_query(
                query,
                alt_query,
                target_team,
                allow_turkish=allow_turkish,
                allow_non_senior=allow_non_senior,
                premium_only=premium_only,
            )
            # print(
            #     f"[selection] No survivors on pass1. Retrying candidate retrieval with pass2 query='{pass2_query}'.",
            #     flush=True,
            # )
            alt_docs = CANDIDATE_RETRIEVER.invoke(pass2_query)
            if cost_tracker is not None:
                _log_cost_step(
                    cost_tracker,
                    "retrieval_pass2",
                    _estimate_embedding_cost_usd(pass2_query),
                    f"query_chars='{len(pass2_query or '')}', retriever_k='40'",
                )
            filtered_docs = _filter_docs(alt_docs, pass2_query, "pass2")
            if filtered_docs:
                success_pass_label = "pass2"

        if not filtered_docs:
            pass3_query = build_pass3_query(
                query,
                alt_query,
                target_team,
                allow_turkish=allow_turkish,
                allow_non_senior=allow_non_senior,
            )
            fallback_docs = CANDIDATE_RETRIEVER.invoke(pass3_query)
            if cost_tracker is not None:
                _log_cost_step(
                    cost_tracker,
                    "retrieval_pass3",
                    _estimate_embedding_cost_usd(pass3_query),
                    f"query_chars='{len(pass3_query or '')}', retriever_k='40'",
                )
            filtered_docs = _filter_docs(
                fallback_docs,
                pass3_query,
                "pass3",
                restrict_to_fallback_clubs=True,
            )
            if filtered_docs:
                success_pass_label = "pass3"

    if not filtered_docs:
        broad_query = pass3_query if target_team else query
        broad_docs = BROAD_CANDIDATE_RETRIEVER.invoke(broad_query)
        if cost_tracker is not None:
            _log_cost_step(
                cost_tracker,
                "retrieval_pass4",
                _estimate_embedding_cost_usd(broad_query),
                f"query_chars='{len(broad_query or '')}', retriever_k='60'",
            )
        filtered_docs = _filter_docs(
            broad_docs,
            broad_query,
            "pass4",
            restrict_to_fallback_clubs=bool(target_team) or prefer_fallback_clubs,
        )
        if filtered_docs:
            success_pass_label = "pass4"

    if filtered_docs:
        # print(f"[selection] succeeded_on='{success_pass_label}'", flush=True)
        # print(
        #     f"[selection] Filtered retrieval candidates: kept {len(filtered_docs)} docs after filtering.",
        #     flush=True,
        # )
        for idx, doc in enumerate(filtered_docs, start=1):
            # print(
            #     f"[selection] FINAL_FILTERED_POOL#{idx}: {summarize_doc_candidate(doc)}",
            #     flush=True,
            # )
            pass
        return StaticDocsRetriever(docs=filtered_docs)

    # print(
    #     f"[selection] Filtered retrieval produced no survivors for query='{query}'. Returning empty filtered retriever.",
    #     flush=True,
    # )
    return StaticDocsRetriever(docs=[])

def add_language_strategy_to_prompt(
    ui_language: Optional[str],
    strategy: Optional[str],
    preamble_text: Optional[str] = None
) -> ChatPromptTemplate:
    sys_msg = system_message

    if strategy:
        sys_msg += "\n\nCurrent scouting strategy / philosophy (must be followed):\n" + strategy + "\n"

    if preamble_text:
        sys_msg += "\n\nSession selection rules / intent hints (must be followed):\n" + preamble_text + "\n"

    # IMPORTANT: no {preamble} variable anymore
    return ChatPromptTemplate.from_messages([
        ("system", sys_msg),
        ("human",
         "{context}\n\n"
         "Question: {question}"
        )
    ])

def get_session_state(session_id: str) -> tuple[str, list]:
    db = get_db()
    try:
        lang = get_session_language(db, session_id) or "en"
        history_rows = load_chat_messages(db, session_id)
        return lang, history_rows
    finally:
        db.close()


def translate_to_english_if_needed(text: Optional[str], lang: str) -> str:
    """If text is Turkish, translate to English; if already English, return unchanged.

    Always logs before/after and prints an approximate DeepSeek cost.
    """
    original = text or ""
    if not is_turkish(lang):  # <--- prevent translation unless TR
        return original
    try:
        translated = translate_chain.invoke({"text": original}).strip()
        return translated or original
    except Exception as e:
        return original


def create_qa_chain(
    lang: str,
    history_rows: list,
    strategy: Optional[str] = None,
    preamble_text: Optional[str] = None,
    retriever: Optional[BaseRetriever] = None,
) -> ConversationalRetrievalChain:

    # hydrate memory from persisted history
    memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)
    msgs: List = []
    for row in history_rows:
        if row["role"] == "human":
            msgs.append(HumanMessage(content=row["content"]))
        elif row["role"] == "ai":
            msgs.append(AIMessage(content=row["content"]))
    memory.chat_memory.messages = msgs

    # build prompt with baked-in preamble_text (no extra input keys)
    prompt = add_language_strategy_to_prompt(lang, strategy, preamble_text=preamble_text)

    chain = ConversationalRetrievalChain.from_llm(
        llm=CHAT_LLM,
        retriever=retriever or SHARED_RETRIEVER,
        memory=memory,
        combine_docs_chain_kwargs={"prompt": prompt}
    )
    return chain

# ===== Player Meta Parser =====

meta_parser_prompt = ChatPromptTemplate.from_messages([            
    ("system", meta_parser_system_prompt),
    ("human", "Text:\n\n{raw_text}\n\nReturn only JSON, no backticks.")
])
meta_parser_chain = meta_parser_prompt | PARSER_LLM | StrOutputParser()

# ===== Translate (TR -> EN, or passthrough EN) =====
translate_prompt = ChatPromptTemplate.from_messages([
    ("system", translate_tr_to_en_system_message),
    ("human", "{text}"),
])
translate_chain = translate_prompt | TRANSLATE_LLM | StrOutputParser()

output_tr_translate_prompt = ChatPromptTemplate.from_messages([
    ("system", translate_en_to_tr_system_message),
    ("human", "{text}"),
])
output_tr_translate_chain = output_tr_translate_prompt | TRANSLATE_LLM | StrOutputParser()

# ==== Interpratation =====
interpretation_prompt = ChatPromptTemplate.from_messages([
    ("system", interpretation_system_prompt),
    ("human",
     "Question:\n{question}\n\n"
     "Team strategy / philosophy (may be empty):\n{strategy}\n\n"
     "Player profile:\n{profile_json}\n\n"
     "Stats (metric/value pairs):\n{stats_json}\n\n"
     "Write exactly 3 sentences."
    )
])

interpretation_chain = interpretation_prompt | CHAT_LLM | StrOutputParser()

# ===== Q&A Actions =====
def answer_question(
    question: str, 
    session_id: str = "default", 
    strategy: Optional[str] = None
) -> Dict[str, Any]:

    # A) get lang + history first
    lang, history_rows = get_session_state(session_id)
    cost_tracker: Dict[str, float] = {"total_usd": 0.0}
    # B) build a temporary memory for “seen players” from history_rows
    ai_msgs: List[AIMessage] = []
    for row in history_rows:
        if row["role"] == "ai":
            ai_msgs.append(AIMessage(content=row["content"]))

    # 2) Compute seen players from PRIOR assistant messages ONLY
    seen_players = get_seen_players_from_history(ai_msgs)
    seen_list_lower = { (n or "").lower().strip() for n in seen_players }
    # 3) Build selection preamble (semantic, no keyword parsing)
    preamble = compose_selection_preamble(seen_players, strategy)
    # 4) Translate user question to English if needed (TR -> EN, EN passthrough)
    original_question = question or ""
    translated_question_raw = translate_to_english_if_needed(original_question, lang)
    translated_question = rewrite_position_reference_phrases(translated_question_raw)
    if is_turkish(lang):
        translation_input_tokens = _estimate_tokens_from_text(translate_tr_to_en_system_message, original_question)
        translation_output_tokens = _estimate_tokens_from_text(translated_question_raw)
        _log_cost_step(
            cost_tracker,
            "translate_tr_to_en",
            _estimate_input_cost_usd(translation_input_tokens) + _estimate_output_cost_usd(translation_output_tokens),
            f"input_chars='{len(original_question)}', output_chars='{len(translated_question_raw)}'",
        )
    generic_alternative_request = is_generic_alternative_request(translated_question)
    # print(
    #     f"[answer] session='{session_id}', lang='{lang}', original_question='{question}', translated_question='{translated_question}'",
    #     flush=True,
    # )
    target_team = extract_target_team_from_question(translated_question)
    if not target_team and generic_alternative_request:
        for row in reversed(history_rows):
            if row.get("role") != "human":
                continue
            target_team = extract_target_team_from_question(row.get("content") or "")
            if target_team:
                break
    recent_constraint_messages: list[str] = []
    if generic_alternative_request:
        recent_constraint_messages = collect_recent_human_constraints(
            history_rows,
            is_generic_alternative_fn=is_generic_alternative_request,
            limit=3,
        )
        # print(
        #     f"[answer] carried_constraints={recent_constraint_messages}",
        #     flush=True,
        # )
    target_team_nudge = ""
    if target_team:
        target_team_nudge = (
            f'Target team rule: the user is scouting for "{target_team}". '
            f'NEVER suggest a player from "{target_team}" or any variant of the same club. '
            'The player must already belong to a different club. '
            'Apply the normal Turkish exclusion and squad-level exclusion rules as well. '
            'Before answering, silently verify that this is a real transfer target from a different club.\n\n'
        )
    # 5) Intent hint — ONLY entity resolution (seen name), no keyword lists
    q_lower = (question or "").lower()
    tq_lower = (translated_question or "").lower()
    mentions_seen_by_name = any(n and (n in q_lower or n in tq_lower) for n in seen_list_lower)
    # Let the LLM infer intent semantically using the preamble rules.
    if mentions_seen_by_name:
        intent_nudge = (
            "Intent: the user referenced a previously seen player by name. "
            "Do NOT print any PLAYER_PROFILE blocks. "
            "Refer back to earlier blocks and provide narrative only.\n\n"
        )
    else:
        intent_nudge = (
            "Intent: the user may be asking for a different option or for collective reasoning about previously discussed players. "
            "Infer intention semantically (not by keywords) using the selection rules above.\n\n"
        )
    if generic_alternative_request and recent_constraint_messages:
        previous_substantive_human = recent_constraint_messages[-1]
        constraints_block = " | ".join(recent_constraint_messages)
        intent_nudge += (
            "Follow-up alternative request: keep the same scouting criteria from the recent substantive user requests. "
            f"Recent user constraints: \"{constraints_block}\". "
            f"The latest substantive request was: \"{previous_substantive_human}\". "
            "Return a different unseen player that fits those same criteria.\n\n"
        )
    initial_high_quality_default = (
        not seen_players
        and not mentions_seen_by_name
        and not generic_alternative_request
        and is_weak_generic_suggestion_request(translated_question)
    )
    initial_fallback_club_target_default = (
        not seen_players
        and not mentions_seen_by_name
        and not generic_alternative_request
        and bool(target_team)
        and is_transfer_fallback_club(target_team)
    )
    initial_strong_club_default = initial_high_quality_default or initial_fallback_club_target_default
    if initial_high_quality_default:
        intent_nudge += (
            "Initial broad suggestion rule: because this is the first weak generic suggestion request in the session, "
            "prefer a player whose current club belongs to the approved strong-club fallback set before considering broader options.\n\n"
        )
    if initial_fallback_club_target_default:
        intent_nudge += (
            f'Initial target-club rule: because the scouting club "{target_team}" belongs to the approved strong-club fallback set, '
            "the first suggested player should also come from that same strong-club fallback set before considering broader options.\n\n"
        )
    discovery_mode = not mentions_seen_by_name
    if discovery_mode:
        intent_nudge += (
            "Discovery rule: when suggesting a player, choose only candidates whose current team and playing position are both clearly available; "
            "if either field is missing or unknown, discard that candidate and choose another one.\n\n"
        )
    preamble_text = preamble + target_team_nudge + intent_nudge
    retrieval_query = translated_question
    if generic_alternative_request and recent_constraint_messages:
        constraints_block = "\n".join(f"- {msg}" for msg in recent_constraint_messages)
        retrieval_query = (
            "Carry over the same scouting constraints from these recent user requests:\n"
            f"{constraints_block}\n"
            "Follow-up request: suggest another different player with the same criteria."
        )
    runtime_retriever = build_filtered_retriever(
        retrieval_query,
        target_team,
        prefer_fallback_clubs=initial_strong_club_default,
        require_complete_discovery_fields=discovery_mode,
        cost_tracker=cost_tracker,
    )
    # print(
    #     f"[answer] target_team='{target_team}', mentions_seen_by_name='{mentions_seen_by_name}', "
    #     f"generic_alternative_request='{generic_alternative_request}', retrieval_query='{retrieval_query}', "
    #     f"retriever='{type(runtime_retriever).__name__}'",
    #     flush=True,
    # )
    qa_chain = create_qa_chain(
        lang=lang,
        history_rows=history_rows,
        strategy=strategy,
        preamble_text=preamble_text,
        retriever=runtime_retriever,
    )
    # 6) LLM Call
    inputs = {
        "question": retrieval_query,
    }
    history_chars = sum(len((row.get("content") or "")) for row in history_rows)
    context_chars = sum(len(doc.page_content or "") for doc in getattr(runtime_retriever, "docs", []))
    try:
        result = qa_chain.invoke(inputs)
        base_answer = (result.get("answer") or "").strip()
        qa_input_tokens = _estimate_tokens_from_text(
            system_message,
            strategy or "",
            preamble_text,
            retrieval_query,
            " ".join(row.get("content") or "" for row in history_rows),
            "".join(doc.page_content or "" for doc in getattr(runtime_retriever, "docs", [])),
        )
        qa_output_tokens = _estimate_tokens_from_text(base_answer)
        _log_cost_step(
            cost_tracker,
            "recommendation_attempt1",
            _estimate_input_cost_usd(qa_input_tokens) + _estimate_output_cost_usd(qa_output_tokens),
            f"history_chars='{history_chars}', context_chars='{context_chars}', output_chars='{len(base_answer)}'",
        )
        # print(f"[answer] initial_llm_answer={base_answer}", flush=True)
    except Exception as e:
        # print(e, flush=True)
        db = get_db()
        try:
            append_chat_message(db, session_id, "human", translated_question)
            append_chat_message(db, session_id, "ai", "Sorry, I couldn’t generate an answer right now.")
        finally:
            db.close()
        return {"answer": "Sorry, I couldn’t generate an answer right now.", "answer_raw": str(e)}

    # 6) Parse current answer into meta/stats
    out = base_answer
    try:
        retry_preamble = preamble_text
        meta = {"players": []}
        meta_new = {"players": []}
        new_names = set()
        payload = {"players": []}

        for attempt_idx in range(1, 3):
            # print(f"[answer] selection_attempt='{attempt_idx}'", flush=True)
            meta = parse_player_meta_new(meta_parser_chain, raw_text=base_answer)
            players = meta.get("players") or []
            # print(f"[answer] parsed_players={players}", flush=True)

            # Hard same-club safeguard: if the model suggests a player already at the
            # destination club, retry with an explicit exclusion preamble.
            if target_team and players:
                suggested_name = (players[0] or {}).get("name")
                suggested_team = (players[0] or {}).get("team")
                # print(suggested_team, flush=True)
                if is_same_club(target_team, suggested_team):
                    # print(f"Same-club suggestion detected: target_team='{target_team}' vs suggested_team='{suggested_team}'. Retrying with exclusion preamble.", flush=True)
                    retry_preamble = (
                        retry_preamble
                        + f'\nInvalid previous choice: "{suggested_team}" is the same club as "{target_team}". '
                          'HARD EXCLUSION RULE: Never suggest a player from the target team or any naming variant, youth side, reserve side, academy side, B team, or affiliate squad of that club. '
                          'You must discard this candidate internally and replace them with a different player before answering. '
                          'Before you output the final player, verify that the player already belongs to a different club and would need to transfer to join the target team. '
                        + (f'Do not suggest "{suggested_name}" again.\n' if suggested_name else "\n")
                    )
                    retry_chain = create_qa_chain(
                        lang=lang,
                        history_rows=history_rows,
                        strategy=strategy,
                        preamble_text=retry_preamble,
                        retriever=runtime_retriever,
                    )
                    retry_result = retry_chain.invoke(inputs)
                    base_answer = (retry_result.get("answer") or "").strip()
                    retry_input_tokens = _estimate_tokens_from_text(
                        system_message,
                        strategy or "",
                        retry_preamble,
                        retrieval_query,
                        " ".join(row.get("content") or "" for row in history_rows),
                        "".join(doc.page_content or "" for doc in getattr(runtime_retriever, "docs", [])),
                    )
                    retry_output_tokens = _estimate_tokens_from_text(base_answer)
                    _log_cost_step(
                        cost_tracker,
                        f"recommendation_retry_attempt{attempt_idx + 1}",
                        _estimate_input_cost_usd(retry_input_tokens) + _estimate_output_cost_usd(retry_output_tokens),
                        f"reason='same target club', output_chars='{len(base_answer)}'",
                    )
                    out = base_answer
                    continue

            # Keep only NEW players for data payload (so cards/plots are printed once per player)
            meta_new, new_names = filter_players_by_seen(meta, seen_players)
            # Build structured data for NEW players only (no HTML/PNGs)
            payload = build_player_payload_new(meta_new) if new_names else {"players": []}
            # print(f"[answer] new_names={sorted(new_names) if new_names else []}", flush=True)

            if generic_alternative_request and players and not new_names:
                duplicate_name = (players[0] or {}).get("name")
                # print(
                #     f"[answer] duplicate alternative suggestion detected for seen player='{duplicate_name}'. Retrying.",
                #     flush=True,
                # )
                retry_preamble = (
                    retry_preamble
                    + f'\nInvalid previous choice: "{duplicate_name or "This player"}" was already shown earlier in this chat. '
                      'For an alternative request, you must return a different unseen player who keeps the same scouting criteria. '
                    + (f'Do not suggest "{duplicate_name}" again.\n' if duplicate_name else "\n")
                )
                retry_chain = create_qa_chain(
                    lang=lang,
                    history_rows=history_rows,
                    strategy=strategy,
                    preamble_text=retry_preamble,
                    retriever=runtime_retriever,
                )
                retry_result = retry_chain.invoke(inputs)
                base_answer = (retry_result.get("answer") or "").strip()
                retry_input_tokens = _estimate_tokens_from_text(
                    system_message,
                    strategy or "",
                    retry_preamble,
                    retrieval_query,
                    " ".join(row.get("content") or "" for row in history_rows),
                    "".join(doc.page_content or "" for doc in getattr(runtime_retriever, "docs", [])),
                )
                retry_output_tokens = _estimate_tokens_from_text(base_answer)
                _log_cost_step(
                    cost_tracker,
                    f"recommendation_retry_attempt{attempt_idx + 1}",
                    _estimate_input_cost_usd(retry_input_tokens) + _estimate_output_cost_usd(retry_output_tokens),
                    f"reason='duplicate alternative suggestion', output_chars='{len(base_answer)}'",
                )
                # print(f"[answer] retry_llm_answer={base_answer}", flush=True)
                out = base_answer
                continue

            resolved_player = (payload.get("players") or [None])[0] or {}
            resolved_meta = resolved_player.get("meta") or {}
            resolved_name = resolved_player.get("name")
            resolved_team = resolved_meta.get("team")
            resolved_nationality = resolved_meta.get("nationality")
            resolved_position_name = resolved_meta.get("position_name")
            resolved_roles = resolved_meta.get("roles") or []
            turkish_name_flag = is_likely_turkish_name(resolved_name)
            # print(
            #     f"[answer] resolved_player name='{resolved_name}', team='{resolved_team}', "
            #     f"nationality='{resolved_nationality}', position_name='{resolved_position_name}', "
            #     f"roles='{resolved_roles}', turkish_name_flag='{turkish_name_flag}'",
            #     flush=True,
            # )
            position_match_ok, requested_position_groups, player_position_groups = player_matches_requested_position(
                retrieval_query,
                resolved_position_name,
                resolved_roles,
            )

            resolved_rejection_reason = (
                get_candidate_rejection_reason(
                    resolved_name,
                    resolved_team,
                    resolved_nationality,
                    target_team=target_team,
                    allow_turkish=False,
                    allow_non_senior=False,
                    premium_only=is_premium_request(translated_question),
                )
                if new_names else None
            )
            if new_names and not position_match_ok:
                resolved_rejection_reason = "position mismatch"
            if new_names and initial_strong_club_default and not is_transfer_fallback_club(resolved_team):
                resolved_rejection_reason = "initial strong-club restriction"
            if new_names and discovery_mode and not has_required_discovery_fields(resolved_team, resolved_position_name):
                resolved_rejection_reason = "missing discovery fields"

            if resolved_rejection_reason:
                # print(
                #     f"{resolved_rejection_reason} safeguard triggered: "
                #     f"name='{resolved_name}', team='{resolved_team}', nationality='{resolved_nationality}', "
                #     f"position_name='{resolved_position_name}', roles='{resolved_roles}', "
                #     f"requested_position_groups='{sorted(requested_position_groups) if requested_position_groups else []}', "
                #     f"player_position_groups='{sorted(player_position_groups) if player_position_groups else []}', "
                #     f"turkish_name_flag='{turkish_name_flag}'. Retrying.",
                #     flush=True,
                # )
                retry_preamble = (
                    retry_preamble
                    + f'\nInvalid previous choice: "{resolved_name or "This player"}" resolves to team "{resolved_team}" '
                      f'with nationality "{resolved_nationality}". '
                      'HARD EXCLUSION RULE: This candidate is completely invalid. '
                      'You must discard this candidate internally and replace them with a different player before answering. '
                      'The replacement player must satisfy all active hard filters from the system prompt and selection rules. '
                      'If the candidate fails the same-club rule, Turkish exclusion rule, squad-level rule, or requested-position rule, discard that player and choose another one before answering. '
                    + (
                        f'The user requested position groups {sorted(requested_position_groups)}; '
                          f'the candidate only matches {sorted(player_position_groups)}. '
                        if resolved_rejection_reason == "position mismatch" and requested_position_groups is not None else ""
                    )
                    + (f'Do not suggest "{resolved_name}" again.\n' if resolved_name else "\n")
                )
                retry_chain = create_qa_chain(
                    lang=lang,
                    history_rows=history_rows,
                    strategy=strategy,
                    preamble_text=retry_preamble,
                    retriever=runtime_retriever,
                )
                retry_result = retry_chain.invoke(inputs)
                base_answer = (retry_result.get("answer") or "").strip()
                retry_input_tokens = _estimate_tokens_from_text(
                    system_message,
                    strategy or "",
                    retry_preamble,
                    retrieval_query,
                    " ".join(row.get("content") or "" for row in history_rows),
                    "".join(doc.page_content or "" for doc in getattr(runtime_retriever, "docs", [])),
                )
                retry_output_tokens = _estimate_tokens_from_text(base_answer)
                _log_cost_step(
                    cost_tracker,
                    f"recommendation_retry_attempt{attempt_idx + 1}",
                    _estimate_input_cost_usd(retry_input_tokens) + _estimate_output_cost_usd(retry_output_tokens),
                    f"reason='{resolved_rejection_reason}', output_chars='{len(base_answer)}'",
                )
                # print(f"[answer] retry_llm_answer={base_answer}", flush=True)
                out = base_answer
                continue

            # print(
            #     f"[answer] accepted_player name='{resolved_name}', team='{resolved_team}', nationality='{resolved_nationality}'",
            #     flush=True,
            # )
            break

        # If QA stage was narrative-only (seen player by name), keep old behavior:
        if not new_names:
            known_names = [p.get("name") for p in (meta.get("players") or []) if p.get("name")]
            out = strip_meta_stats_text(base_answer, known_names=known_names)
        else:
            # QA stage is block-only -> generate narrative from payload + meta
            p0 = (payload.get("players") or [None])[0] or {}
            profile_meta = p0.get("meta") or {}
            stats = p0.get("stats") or []
            # Build compact inputs for the interpretation LLM
            profile_json = json.dumps({
                "name": p0.get("name"),
                **profile_meta
            }, ensure_ascii=False)

            stats_json = json.dumps(stats, ensure_ascii=False)

            out = interpretation_chain.invoke({
                "question": translated_question,
                "strategy": strategy or "",
                "profile_json": profile_json,
                "stats_json": stats_json,
            }).strip()
            interpretation_input_tokens = _estimate_tokens_from_text(
                interpretation_system_prompt,
                translated_question,
                strategy or "",
                profile_json,
                stats_json,
            )
            interpretation_output_tokens = _estimate_tokens_from_text("x" * INTERPRETATION_OUTPUT_CHARS)
            _log_cost_step(
                cost_tracker,
                "interpretation",
                _estimate_input_cost_usd(interpretation_input_tokens) + _estimate_output_cost_usd(interpretation_output_tokens),
                f"assumed_output_chars='{INTERPRETATION_OUTPUT_CHARS}'",
            )
        memory_out = out
        if is_turkish(lang):
            try:
                translated_out = output_tr_translate_chain.invoke({"text": memory_out}).strip()
                if translated_out:
                    out = translated_out
                tr_output_input_tokens = _estimate_tokens_from_text(translate_en_to_tr_system_message, memory_out)
                tr_output_output_tokens = _estimate_tokens_from_text(translated_out if translated_out else memory_out)
                _log_cost_step(
                    cost_tracker,
                    "translate_en_to_tr",
                    _estimate_input_cost_usd(tr_output_input_tokens) + _estimate_output_cost_usd(tr_output_output_tokens),
                    f"input_chars='{len(memory_out)}', output_chars='{len(translated_out if translated_out else memory_out)}'",
                )
            except Exception as e:
                pass
        
        stored_ai_content = "[[PAYLOAD_JSON]]\n" + json.dumps(payload, ensure_ascii=False) + "\n[[/PAYLOAD_JSON]]" + "\n\n" + memory_out
        db = get_db()
        try:
            append_chat_message(db, session_id, "human", translated_question)
            append_chat_message(db, session_id, "ai", stored_ai_content)
        finally:
            db.close()

        return {"answer": out, "data": payload}


    except Exception as e:
        # Persist raw base answer if parsing failed (optional)
        db = get_db()
        try:
            append_chat_message(db, session_id, "human", translated_question)
            append_chat_message(db, session_id, "ai", base_answer)
        finally:
            db.close()
        return {"answer": "Sorry, I couldn’t generate an answer right now.", "error": str(e)}
