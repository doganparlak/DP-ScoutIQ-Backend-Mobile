from typing import Dict, Any, Optional, List
from dotenv import load_dotenv
load_dotenv()
from langchain_deepseek import ChatDeepSeek
from langchain_core.prompts import ChatPromptTemplate
from langchain_classic.memory import ConversationBufferMemory
from langchain_classic.chains import ConversationalRetrievalChain
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
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
    inject_language,
    is_turkish
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

SHARED_RETRIEVER = get_retriever(k=6, filter=None)

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
    preamble_text: Optional[str] = None
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
        retriever=SHARED_RETRIEVER,
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
    # B) build a temporary memory for “seen players” from history_rows
    ai_msgs: List[AIMessage] = []
    for row in history_rows:
        if row["role"] == "ai":
            ai_msgs.append(AIMessage(content=row["content"]))

    # 2) Compute seen players from PRIOR assistant messages ONLY
    seen_players = get_seen_players_from_history(ai_msgs)
    seen_list_lower = { (n or "").lower().strip() for n in seen_players }
    print("SEEN PLAYERS FETCHED")
    print(seen_list_lower)
    # 3) Build selection preamble (semantic, no keyword parsing)
    preamble = compose_selection_preamble(seen_players, strategy)
    # 4) Translate user question to English if needed (TR -> EN, EN passthrough)
    translated_question = translate_to_english_if_needed(question or "", lang)
    # 5) Intent hint — ONLY entity resolution (seen name), no keyword lists
    q_lower = (question or "").lower()
    mentions_seen_by_name = any(n and n in q_lower for n in seen_list_lower)
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
    preamble_text = preamble + intent_nudge
    qa_chain = create_qa_chain(
        lang=lang,
        history_rows=history_rows,
        strategy=strategy,
        preamble_text=preamble_text
    )
    retrieval_query = translated_question    
    # 6) LLM Call
    inputs = {
        "question": retrieval_query,
    }
    db = get_db()
    try:
        result = qa_chain.invoke(inputs)
        base_answer = (result.get("answer") or "").strip()
        append_chat_message(db, session_id, "human", question or "")
        append_chat_message(db, session_id, "ai", base_answer)
    except Exception as e:
        print(e)
        append_chat_message(db, session_id, "human", question or "")
        append_chat_message(db, session_id, "ai", "Sorry, I couldn’t generate an answer right now.")
        return {"answer": "Sorry, I couldn’t generate an answer right now.", "answer_raw": str(e)}
    finally:
        db.close()

    # 6) Parse current answer into meta/stats
    out = base_answer
    print(out)
    try:
        meta = parse_player_meta_new(meta_parser_chain, raw_text=base_answer)
        print("META")
        print(meta)
        # Keep only NEW players for data payload (so cards/plots are printed once per player)
        meta_new, new_names = filter_players_by_seen(meta, seen_players)
        print("META NEW")
        print(meta_new)
        # Build structured data for NEW players only (no HTML/PNGs)
        payload = build_player_payload_new(meta_new) if new_names else {"players": []}
        print("PAYLOAD")
        print(payload)
        # If QA stage was narrative-only (seen player by name), keep old behavior:
        if not new_names:
            known_names = [p.get("name") for p in (meta.get("players") or []) if p.get("name")]
            out = strip_meta_stats_text(base_answer, known_names=known_names)
        else:
            # QA stage is block-only -> generate narrative from payload + meta
            p0 = (payload.get("players") or [None])[0] or {}
            print(p0)
            profile_meta = p0.get("meta") or {}
            print(profile_meta)
            stats = p0.get("stats") or []
            print(stats)
            # Build compact inputs for the interpretation LLM
            profile_json = json.dumps({
                "name": p0.get("name"),
                **profile_meta
            }, ensure_ascii=False)

            stats_json = json.dumps(stats, ensure_ascii=False)

            out = interpretation_chain.invoke({
                "question": translated_question,
                "profile_json": profile_json,
                "stats_json": stats_json,
            }).strip()
            print("OUT")
            print(out)
        if is_turkish(lang):
            try:
                translated_out = output_tr_translate_chain.invoke({"text": out}).strip()
                if translated_out:
                    out = translated_out
            except Exception as e:
                pass

        return {"answer": out, "data": payload}


    except Exception as e:
        # Persist raw base answer if parsing failed (optional)
        db = get_db()
        try:
            append_chat_message(db, session_id, "human", question or "")
            append_chat_message(db, session_id, "ai", base_answer)
        finally:
            db.close()
        return {"answer": "Sorry, I couldn’t generate an answer right now.", "error": str(e)}
