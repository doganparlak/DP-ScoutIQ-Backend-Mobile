from typing import Dict, Any
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
    plot_stats_bundle,
    build_player_tables,
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
def answer_question(question: str, session_id: str = "default") -> Dict[str, Any]:

    # 0) Get/Create Chain
    qa_chain = get_session_chain(session_id)
    memory: ConversationBufferMemory = qa_chain.memory 

    # 1) Freeze PRIOR history (before any LLM call can mutate memory)
    prior_history = memory.load_memory_variables({})["chat_history"]
    prior_history_frozen = list(prior_history)

    # 2) Compute seen players from PRIOR assistant messages ONLY
    seen_players = get_seen_players_from_history(prior_history_frozen)
    allowed_players_str = ", ".join(seen_players) if seen_players else "None"

    # inject the constraint into the question (parametric, prompt-level)
    augmented_question = (
        "Previously mentioned players in this session "
        "(the only allowed pool for any choose/rank/lineup decisions): "
        f"{allowed_players_str}\n\n"
        f"{question}"
    )

    # 3) LLM Call
    inputs = {
        "question": augmented_question,
        "chat_history": prior_history_frozen,  # safe snapshot
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
        meta = parse_player_meta(meta_parser_chain, raw_text = base_answer)

        # 5) Keep only NEW players for tables/plots (old = reference-only)
        meta_new, stats_new, new_names = filter_players_by_seen(meta, parsed_stats, seen_players)

        # 6) Build assets for NEW players only
        player_to_plot = plot_stats_bundle(stats_new) if new_names else {}
        tables_html = build_player_tables(meta_new, stats_new) if new_names else ""

        # 7) Strip flagged/meta/stats text from the answer body (narrative remains)
        known_names = [p.get("name") for p in (meta.get("players") or []) if p.get("name")]
        cleaned = strip_meta_stats_text(base_answer, known_names=known_names)

        # 8) Compose: Tables (NEW only) → Plots (NEW only) → narrative
        parts = []
        if tables_html:
            parts.append(tables_html)

        if player_to_plot:
            #blocks = ["", "**Plots**", ""]
            blocks = []
            for name in sorted(player_to_plot.keys(), key=lambda s: s.lower()):
                val = player_to_plot[name]
                src = (val.get("src") if isinstance(val, dict) else str(val)) or ""
                width_px = int(val.get("width_px", 1600)) if isinstance(val, dict) else 1600
                if src:
                    #blocks.append(name)
                    blocks.append(f'<img alt="{name} — Statistical Highlights" src="{src}" width="{width_px}" />')
                    #blocks.append("")
            parts.append("\n".join(blocks))

        if cleaned:
            parts.append(cleaned)

        out = "\n\n".join(p for p in parts if p).replace("**", "")
    except:
        # Silently fall back to base_answer if enrichers fail
        out = base_answer

    return {"answer": out}