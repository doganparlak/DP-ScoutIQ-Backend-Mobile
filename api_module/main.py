from typing import Optional, Dict, Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from chatbot_module.chatbot import answer_question, get_session_chain, reset_session
from response_handler import split_response_parts

app = FastAPI()
# CORS (lock this down to your frontend origin in prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # e.g., ["http://localhost:19006", "http://localhost:3000"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Query(BaseModel):
    question: str
    strategy: Optional[str] = None
    session_id: str  # required for session tracking

@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True}

@app.post("/chat")
async def chat(query: Query) -> Dict[str, Any]:
    """
    - Uses per-session memory from chatbot.py
    - If `strategy` is provided, we inject it as a user message into that session's memory.
    - Returns HTML answer and split parts for your frontend.
    """

    if query.strategy:
        chain = get_session_chain(query.session_id)
        # Record strategy as a user-side note so it influences retrieval/LLM
        chain.memory.chat_memory.add_user_message(f"Team Strategy: {query.strategy}")

    resp = answer_question(query.question, session_id=query.session_id)
    answer_html = resp.get("answer", "No response was generated.")

    return {
        "response": answer_html,
        "response_parts": split_response_parts(answer_html),
    }

@app.post("/reset")
async def reset(session_id: str) -> Dict[str, Any]:
    """
    Clears the chat history for a given session.
    """
    reset_session(session_id)
    return {"ok": True, "session_id": session_id, "reset": True}

# Run with: uvicorn backend.main:app --reload