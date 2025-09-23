from typing import Optional, Dict, Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from chatbot_module.chatbot import answer_question, get_session_chain, reset_session
from api_module.response_handler import split_response_parts

app = FastAPI()
# CORS (lock this down to your frontend origin in prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # e.g., ["http://localhost:19006", "http://localhost:3000"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatIn(BaseModel):
    message: str
    session_id: Optional[str] = "default"
    strategy: Optional[str] = None

class Query(BaseModel):
    question: str
    strategy: Optional[str] = None
    session_id: str  # required for session tracking

@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True}

@app.post("/chat")
async def chat(body: ChatIn) -> Dict[str, Any]:
    """
    Returns:
      {
        "response": "<narrative text only>",
        "data": {"players": [...]},           # <-- NEW: structured data, no visuals
        "response_parts": [...]               # optional: split narrative for streaming
      }
    """
    result = answer_question(body.message, 
                             session_id=body.session_id or "default",
                             strategy=body.strategy)

    # Back-compat & shape normalization
    answer_text = (result.get("answer") or "").strip()
    payload = result.get("data") or {"players": []}
    
    print(payload)
    return {
        "response": answer_text,
        "data": payload,                              # <-- NEW
        "response_parts": split_response_parts(answer_text),
    }

@app.post("/reset")
async def reset(session_id: str) -> Dict[str, Any]:
    reset_session(session_id)
    return {"ok": True, "session_id": session_id, "reset": True}

# Run with: uvicorn api_module.main:app --reload --port 8000         