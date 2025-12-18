from typing import Dict, Any, Optional, List
from dotenv import load_dotenv
load_dotenv()

import warnings
import json
warnings.filterwarnings("ignore", category=DeprecationWarning, module="langchain")

from langchain_deepseek import ChatDeepSeek
from langchain_core.prompts import ChatPromptTemplate
from langchain_classic.memory import ConversationBufferMemory
from langchain_classic.chains import ConversationalRetrievalChain
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser

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
    compose_selection_preamble,
    inject_language,
    is_turkish,
    build_recent_context,
)

from chatbot_module.tools_extensions import (
    parse_player_meta_new,
    build_player_payload_new
)

from chatbot_module.vectorstore_small import get_retriever


# ===== Models =====

CHAT_LLM = ChatDeepSeek(
    model="deepseek-chat",
    temperature=0.3,
)

PARSER_LLM = ChatDeepSeek(
    model="deepseek-chat",
    temperature=0,
)

TRANSLATE_LLM = ChatDeepSeek(
    model="deepseek-chat",
    temperature=0,
)

SHARED_RETRIEVER = get_retriever(k=6, filter=None)


# ===== Prompt builders =====

def add_language_strategy_to_prompt(
    ui_language: Optional[str],
    strategy: Optional[str],
    preamble_text: Optional[str] = None
) -> ChatPromptTemplate:
    sys_msg = inject_language(system_message, "en")

    if strategy:
        sys_msg += "\n\nCurrent scouting strategy / philosophy (must be followed):\n" + strategy + "\n"

    if preamble_text:
        sys_msg += "\n\nSession selection rules / intent hints (must be followed):\n" + preamble_text + "\n"

    return ChatPromptTemplate.from_messages([
        ("system", sys_msg),
        ("human", "{context}\n\nQuestion: {question}")
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
    original = text or ""
    if not is_turkish(lang):
        return original
    try:
        translated = translate_chain.invoke({"text": original}).strip()
        return translated or original
    except Exception:
        return original


def create_qa_chain(
    lang: str,
    history_rows: list,
    strategy: Optional[str] = None,
    preamble_text: Optional[str] = None
) -> ConversationalRetrievalChain:

    memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)
    msgs: List = []
    for row in history_rows:
        if row.get("role") == "human":
            msgs.append(HumanMessage(content=row.get("content") or ""))
        elif row.get("role") == "ai":
            msgs.append(AIMessage(content=row.get("content") or ""))
    memory.chat_memory.messages = msgs

    prompt = add_language_strategy_to_prompt(lang, strategy, preamble_text=preamble_text)

    chain = ConversationalRetrievalChain.from_llm(
        llm=CHAT_LLM,
        retriever=SHARED_RETRIEVER,
        memory=memory,
        combine_docs_chain_kwargs={"prompt": prompt}
    )
    return chain


# ===== Chains =====

meta_parser_prompt = ChatPromptTemplate.from_messages([
    ("system", meta_parser_system_prompt),
    ("human", "Text:\n\n{raw_text}\n\nReturn only JSON, no backticks.")
])
meta_parser_chain = meta_parser_prompt | PARSER_LLM | StrOutputParser()

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

interpretation_prompt = ChatPromptTemplate.from_messages([
    ("system", interpretation_system_prompt),
    ("human",
     "Question:\n{question}\n\n"
     "Known players so far (names only):\n{known_names_json}\n\n"
     "Recent chat context:\n{recent_context}\n\n"
     "If profile_json and stats_json are provided, interpret that player.\n"
     "If they are empty, select exactly ONE player from known_names_json that best matches the question/context and interpret them.\n\n"
     "Player profile JSON:\n{profile_json}\n\n"
     "Stats JSON (metric/value pairs):\n{stats_json}\n\n"
     "Write exactly 3 sentences."
    )
])
interpretation_chain = interpretation_prompt | CHAT_LLM | StrOutputParser()


# ===== Q&A =====

def answer_question(
    question: str,
    session_id: str = "default",
    strategy: Optional[str] = None
) -> Dict[str, Any]:

    # --- session state ---
    lang, history_rows = get_session_state(session_id)

    # --- seen players from prior assistant messages only ---
    ai_msgs: List[AIMessage] = []
    for row in history_rows:
        if row.get("role") == "ai":
            ai_msgs.append(AIMessage(content=row.get("content") or ""))

    seen_players = get_seen_players_from_history(ai_msgs)
    seen_list_lower = {(n or "").lower().strip() for n in seen_players}

    print("SEEN PLAYERS FETCHED")
    print(seen_list_lower)

    # --- translate question to EN if TR session ---
    translated_question = translate_to_english_if_needed(question or "", lang)

    # --- detect explicit mention of any seen player name ---
    q_lower = (question or "").lower()
    mentions_seen_by_name = any(n and n in q_lower for n in seen_list_lower)

    # Always persist the HUMAN message (even if we skip QA)
    db = get_db()
    try:
        append_chat_message(db, session_id, "human", question or "")
    finally:
        db.close()

    # --- if user references a seen player name, skip QA and interpret from context+names ---
    if mentions_seen_by_name:
        print("SEEN PLAYER")
        known_names = sorted([n for n in seen_players if n])
        print(known_names)
        recent_context = build_recent_context(history_rows)
        print(recent_context)
        out = interpretation_chain.invoke({
            "question": translated_question,
            "known_names_json": json.dumps(known_names, ensure_ascii=False),
            "recent_context": recent_context,
            "profile_json": "",
            "stats_json": "",
        }).strip()
        print(out)
        if is_turkish(lang):
            try:
                tr = output_tr_translate_chain.invoke({"text": out}).strip()
                if tr:
                    out = tr
            except Exception:
                pass

        # Persist AI narrative
        db = get_db()
        try:
            append_chat_message(db, session_id, "ai", out)
        finally:
            db.close()

        return {"answer": out, "data": {"players": []}}

    # --- otherwise run QA to produce profile block (new player flow) ---
    preamble = compose_selection_preamble(seen_players, strategy)
    intent_nudge = (
        "Intent: the user may be asking for a different option or for collective reasoning about previously discussed players. "
        "Infer intention semantically (not by keywords) using the selection rules above.\n\n"
    )
    preamble_text = preamble + intent_nudge

    qa_chain = create_qa_chain(
        lang=lang,
        history_rows=history_rows,
        strategy=strategy,
        preamble_text=preamble_text
    )

    base_answer = ""
    try:
        result = qa_chain.invoke({"question": translated_question})
        base_answer = (result.get("answer") or "").strip()
        print(base_answer)
    except Exception as e:
        # Persist AI failure message
        db = get_db()
        try:
            append_chat_message(db, session_id, "ai", "Sorry, I couldn’t generate an answer right now.")
        finally:
            db.close()
        return {"answer": "Sorry, I couldn’t generate an answer right now.", "answer_raw": str(e)}

    # Persist raw QA output (block-only typically)
    db = get_db()
    try:
        append_chat_message(db, session_id, "ai", base_answer)
    finally:
        db.close()

    # --- parse meta from QA answer, build payload for NEW players only ---
    try:
        meta = parse_player_meta_new(meta_parser_chain, raw_text=base_answer)
        print(meta)
        meta_new, new_names = filter_players_by_seen(meta, seen_players)
        print(meta_new)
        payload = build_player_payload_new(meta_new) if new_names else {"players": []}

        # If somehow no new players were produced, fallback to context-based interpretation
        if not new_names or not (payload.get("players") or []):
            print("fallback")
            known_names = sorted([n for n in seen_players if n])
            recent_context = build_recent_context(history_rows)

            out = interpretation_chain.invoke({
                "question": translated_question,
                "known_names_json": json.dumps(known_names, ensure_ascii=False),
                "recent_context": recent_context,
                "profile_json": "",
                "stats_json": "",
            }).strip()
        else:
            p0 = (payload.get("players") or [None])[0] or {}
            print(p0)
            profile_meta = p0.get("meta") or {}
            print(profile_meta)
            stats = p0.get("stats") or []
            print(stats)
            profile_json = json.dumps(
                {"name": p0.get("name"), **profile_meta},
                ensure_ascii=False
            )
            stats_json = json.dumps(stats, ensure_ascii=False)

            known_names = sorted([n for n in seen_players if n])
            recent_context = build_recent_context(history_rows)
            out = interpretation_chain.invoke({
                "question": translated_question,
                "known_names_json": json.dumps(known_names, ensure_ascii=False),
                "recent_context": recent_context,
                "profile_json": profile_json,
                "stats_json": stats_json,
            }).strip()
            print(out)
        # Optional: if QA answer had extra stuff, you can still strip it (usually unnecessary now)
        # known_names_in_answer = [p.get("name") for p in (meta.get("players") or []) if p.get("name")]
        # base_clean = strip_meta_stats_text(base_answer, known_names=known_names_in_answer)

        if is_turkish(lang):
            try:
                tr = output_tr_translate_chain.invoke({"text": out}).strip()
                if tr:
                    out = tr
            except Exception:
                pass

        # Persist final interpreted narrative as an AI message (so next turns can reference it)
        db = get_db()
        try:
            append_chat_message(db, session_id, "ai", out)
        finally:
            db.close()

        return {"answer": out, "data": payload}

    except Exception as e:
        # Persist AI failure message
        db = get_db()
        try:
            append_chat_message(db, session_id, "ai", "Sorry, I couldn’t generate an answer right now.")
        finally:
            db.close()
        return {"answer": "Sorry, I couldn’t generate an answer right now.", "error": str(e)}
