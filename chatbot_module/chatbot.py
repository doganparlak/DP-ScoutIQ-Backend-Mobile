from typing import Dict, Any, Optional
from dotenv import load_dotenv
from langchain_community.vectorstores import FAISS
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain.prompts import ChatPromptTemplate
from langchain.chains import ConversationalRetrievalChain
from langchain.memory import ConversationBufferMemory
from langchain.chains import LLMChain
from chatbot_module.prompts import (
    system_message,
    stats_parser_system_message,
    meta_parser_system_prompt,
)
from chatbot_module.tools import (
    get_seen_players_from_history, 
    parse_statistical_highlights,
    parse_player_meta,
    filter_players_by_seen,
    build_player_payload,
    strip_meta_stats_text)

# Load environment variables
load_dotenv()

# === Load Vectorstore ===
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

retriever = vectorstore.as_retriever(search_kwargs={"k": 10})

# === QA Chain with RAG & Memory ===
prompt = ChatPromptTemplate.from_messages([
    ("system", system_message),
    ("human",
     "{context}\n\n"
     "Question: {question}"
    )
])

def create_qa_chain():
    llm = ChatOpenAI(model="gpt-4o", temperature=0.3)
    memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)
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
stats_parser_chain = LLMChain(
    llm=ChatOpenAI(model="gpt-4o", temperature=0),
    prompt=stats_parser_prompt
)

# ===== Player Meta Parser =====

meta_parser_prompt = ChatPromptTemplate.from_messages([            
    ("system", meta_parser_system_prompt),
    ("human", "Text:\n\n{raw_text}\n\nReturn only JSON, no backticks.")
])

meta_parser_chain = LLMChain(                                     
    llm=ChatOpenAI(model="gpt-4o", temperature=0),
    prompt=meta_parser_prompt
)

# === Per-session chains (so different users/chats don't share memory) ===
sessions: Dict[str, ConversationalRetrievalChain] = {}

def get_session_chain(session_id: str) -> ConversationalRetrievalChain:
    chain = sessions.get(session_id)
    if chain is None:
        chain = create_qa_chain()
        sessions[session_id] = chain
    return chain

def reset_session(session_id: str) -> None:
    """Clears chat history for a given session."""
    if session_id in sessions:
        # Recreate to reset memory cleanly
        sessions[session_id] = create_qa_chain()

# ===== Q&A Actions =====
def answer_question(question: str, session_id: str = "default", strategy: Optional[str] = None) -> Dict[str, Any]:

    # 0) Get/Create Chain
    qa_chain = get_session_chain(session_id)
    memory: ConversationBufferMemory = qa_chain.memory 

    # 1) Freeze PRIOR history (before any LLM call can mutate memory)
    prior_history = memory.load_memory_variables({})["chat_history"]
    prior_history_frozen = list(prior_history)
    # 2) Compute seen players from PRIOR assistant messages ONLY
    seen_players = get_seen_players_from_history(prior_history_frozen)
    allowed_players_str = ", ".join(seen_players) if seen_players else "None"

    strat = (strategy or "").strip()
    strategy_block = (
        f"User strategy preference (use this to shape the analysis and selections): {strat}\n\n"
        if strat else ""
    )

    augmented_question = (
        strategy_block
        + "Previously mentioned players in this session "
        "(the only allowed pool for any choose/rank/lineup decisions): "
        + (allowed_players_str or "None")
        + "\n\n"
        + question
    )

    # 3) LLM Call
    inputs = {
        "question": augmented_question
        #"chat_history": prior_history_frozen
    }
    try:
        result = qa_chain.invoke(inputs)
        base_answer = (result.get("answer") or "").strip()
    except Exception as e:
        # If LLM/RAG fails, surface a minimal error message
        return {"answer": "Sorry, I couldn’t generate an answer right now.", "answer_raw": str(e)}

    # 4) Parse current answer into meta/stats
    out = base_answer
    try:
        qa_as_report = f"**Statistical Highlights**\n\n{base_answer}\n\n"
        parsed_stats = parse_statistical_highlights(stats_parser_chain, qa_as_report)
        print(base_answer)
        print("===================")
        meta = parse_player_meta(meta_parser_chain, raw_text=base_answer)
        # 5) Keep only NEW players for data payload (same dedupe logic as before)   
        meta_new, stats_new, new_names = filter_players_by_seen(meta, parsed_stats, seen_players)
        # 6) Build structured data for NEW players only (no HTML/PNGs)
        payload = build_player_payload(meta_new, stats_new) if new_names else {"players": []}

        # 7) Strip flagged/meta/stats text from the narrative
        known_names = [p.get("name") for p in (meta.get("players") or []) if p.get("name")]
        cleaned = strip_meta_stats_text(base_answer, known_names=known_names)

        # 8) Compose: narrative only
        out = cleaned

        # 9) Return narrative PLUS structured data
        return {"answer": out, "data": payload}

    except Exception as e:
        return {"answer": "Sorry, I couldn’t generate an answer right now.", "error": str(e)}