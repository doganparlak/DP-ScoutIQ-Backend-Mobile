from typing import Dict, Any, Optional, List
from dotenv import load_dotenv
load_dotenv()
from langchain_openai import ChatOpenAI
from langchain_deepseek import ChatDeepSeek
from langchain_core.prompts import ChatPromptTemplate
from langchain_classic.memory import ConversationBufferMemory
from langchain_classic.chains import ConversationalRetrievalChain
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module="langchain")

from api_module.utilities import (
    get_db,
    get_session_language,
    append_chat_message,
    load_chat_messages
)
from chatbot_module.prompts import (
    system_message,
    stats_parser_system_message,
    meta_parser_system_prompt
)
from chatbot_module.tools import (
    get_seen_players_from_history, 
    parse_statistical_highlights,
    parse_player_meta,
    filter_players_by_seen,
    build_player_payload,
    strip_meta_stats_text,
    compose_selection_preamble,
    inject_language
)

# === Load Vectorstore ===
from chatbot_module.vectorstore import get_retriever



"""
embedding = OpenAIEmbeddings(model = "text-embedding-3-large")


try:
    vectorstore = FAISS.load_local(
        "data_module/faiss_index", embedding, allow_dangerous_deserialization=True
    )
except Exception as e:
    raise RuntimeError(
        "Failed to load FAISS index from ./faiss_index. Make sure it exists and "
        "was built with the same embedding model."
    ) from e
"""
# === QA Chain with RAG & Memory ===


def add_language_strategy_to_prompt(ui_language: Optional[str], strategy: Optional[str]) -> ChatPromptTemplate:
    sys_msg = inject_language(system_message, ui_language)
    if strategy:
        sys_msg += (
            "\n\nCurrent scouting strategy / philosophy (must be followed):\n"
            f"{strategy}\n"
        )
    return ChatPromptTemplate.from_messages([
        ("system", sys_msg),
        ("human",
         "{context}\n\n"
         "Question: {question}"
        )
    ])

def create_qa_chain(session_id: str, strategy: Optional[str] = None) -> ConversationalRetrievalChain:
    # 1) get session language
    db = get_db()
    try:
        lang = get_session_language(db, session_id) or "en"
        history_rows = load_chat_messages(db, session_id)
    finally:
        db.close()

    # 2) hydrate memory from persisted history
    memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)
    msgs: List = []
    for row in history_rows:
        if row["role"] == "human":
            msgs.append(HumanMessage(content=row["content"]))
        elif row["role"] == "ai":
            msgs.append(AIMessage(content=row["content"]))
        # ignore any 'system' rows for chat_history
    memory.chat_memory.messages = msgs

    
    # 2) LLM for chat
    #llm = ChatOpenAI(model="gpt-4o", temperature=0.3)
    llm = ChatDeepSeek(
        model="deepseek-chat",   # non-reasoning mode V3.x
        temperature=0.3,
    )
    
    # 3)retriever
    retriever = get_retriever(k=8, filter=None)

    # 4) Prompt
    prompt = add_language_strategy_to_prompt(lang, strategy)

    return ConversationalRetrievalChain.from_llm(
        llm=llm,
        retriever=retriever,
        memory=memory,
        combine_docs_chain_kwargs={"prompt": prompt}
    )

# ===== Stats Parser =====
stats_parser_prompt = ChatPromptTemplate.from_messages([
    ("system", stats_parser_system_message),
    ("human", "Report:\n\n{report_text}\n\nReturn only JSON, no backticks.")
])

llm_parser= ChatOpenAI(model="gpt-4o", temperature=0)
#llm_parser = ChatDeepSeek(
#    model="deepseek-chat",
#    temperature=0,   # keep it deterministic for JSON-style parsing
#)
stats_parser_chain = stats_parser_prompt | llm_parser | StrOutputParser()

# ===== Player Meta Parser =====

meta_parser_prompt = ChatPromptTemplate.from_messages([            
    ("system", meta_parser_system_prompt),
    ("human", "Text:\n\n{raw_text}\n\nReturn only JSON, no backticks.")
])

meta_parser_chain = meta_parser_prompt | llm_parser | StrOutputParser()
# ===== Q&A Actions =====
def answer_question(
    question: str, 
    session_id: str = "default", 
    strategy: Optional[str] = None
) -> Dict[str, Any]:

    # 0) Get/Create Chain
    qa_chain = create_qa_chain(session_id, strategy=strategy)
    memory: ConversationBufferMemory = qa_chain.memory

    # 1) Freeze PRIOR history (before any LLM call can mutate memory)
    prior_history = memory.load_memory_variables({})["chat_history"]
    prior_history_frozen = list(prior_history)

    # 2) Compute seen players from PRIOR assistant messages ONLY
    seen_players = get_seen_players_from_history(prior_history_frozen)
    seen_list_lower = { (n or "").lower().strip() for n in seen_players }
    #print(seen_players)
    # 3) Build selection preamble (semantic, no keyword parsing)
    preamble = compose_selection_preamble(seen_players, strategy)

    # 4) Intent hint — ONLY entity resolution (seen name), no keyword lists
    q_lower = (question or "").lower()
    mentions_seen_by_name = any(n and n in q_lower for n in seen_list_lower)

    # We do not enumerate any “another/alternative” words.
    # Let the LLM infer intent semantically using the preamble rules.
    if mentions_seen_by_name:
        intent_nudge = (
            "Intent: the user referenced a previously seen player by name. "
            "Do NOT print any PLAYER_PROFILE/PLAYER_STATS blocks. "
            "Refer back to earlier blocks and provide narrative only.\n\n"
        )
    else:
        intent_nudge = (
            "Intent: the user may be asking for a different option or for collective reasoning about previously discussed players. "
            "Infer intention semantically (not by keywords) using the selection rules above.\n\n"
        )
    no_nationality_bias = (
        "Nationality constraint: none unless the user explicitly specifies one.\n"
        "Do NOT infer or prefer a player's nationality from the interface or query language.\n"
        "When nationality is unspecified, treat it as 'unspecified' and select solely on role fit/history and performance.\n\n"
    )

    augmented_question = preamble + no_nationality_bias + intent_nudge + "Question: " + (question or "")
    # 5) LLM Call
    inputs = {"question": augmented_question}
    try:
        result = qa_chain.invoke(inputs)
        base_answer = (result.get("answer") or "").strip()

        db = get_db()
        try:
            append_chat_message(db, session_id, "human", question or "")
            append_chat_message(db, session_id, "ai", base_answer)
        finally:
            db.close()
    except Exception as e:
        # Persist the user's message even if model fails
        print(e)
        db = get_db()
        try:
            append_chat_message(db, session_id, "human", question or "")
            append_chat_message(db, session_id, "ai", "Sorry, I couldn’t generate an answer right now.")
        finally:
            db.close()
        return {"answer": "Sorry, I couldn’t generate an answer right now.", "answer_raw": str(e)}

    # 6) Parse current answer into meta/stats
    out = base_answer
    try:
        qa_as_report = f"**Statistical Highlights**\n\n{base_answer}\n\n"
        print(base_answer)
        parsed_stats = parse_statistical_highlights(stats_parser_chain, qa_as_report)
        meta = parse_player_meta(meta_parser_chain, raw_text=base_answer)
        print(meta)
        # Keep only NEW players for data payload (so cards/plots are printed once per player)
        meta_new, stats_new, new_names = filter_players_by_seen(meta, parsed_stats, seen_players)

        # Build structured data for NEW players only (no HTML/PNGs)
        payload = build_player_payload(meta_new, stats_new) if new_names else {"players": []}

        # Strip flagged/meta/stats text from the narrative; keep only analysis
        known_names = [p.get("name") for p in (meta.get("players") or []) if p.get("name")]
        cleaned = strip_meta_stats_text(base_answer, known_names=known_names)
        #print(payload)
        out = cleaned

        return {"answer": out, "data": payload}

    except Exception as e:
        print(e)
        # Persist raw base answer if parsing failed (optional)
        db = get_db()
        try:
            append_chat_message(db, session_id, "human", question or "")
            append_chat_message(db, session_id, "ai", base_answer)
        finally:
            db.close()
        return {"answer": "Sorry, I couldn’t generate an answer right now.", "error": str(e)}
