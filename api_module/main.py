# api_module/main.py
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware

# your existing chat utilities
from chatbot_module.chatbot import answer_question, get_session_chain, reset_session
from api_module.response_handler import split_response_parts

# import our refactored pieces
from api_module.utilities import (
    get_db, init_db, hash_pw, new_salt, now_iso,
    user_row_to_dict, require_auth, create_reset_code, send_email_code, verify_reset_code, DB_FILE
)
from api_module.models import (
    SignUpIn, LoginIn, LoginOut, ProfileOut, ProfilePatch,
    PasswordResetRequestIn, VerifyResetIn, ChatIn
)

import hmac, uuid, json

app = FastAPI()

# CORS (lock this down to your frontend origin in prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # e.g., ["http://localhost:19006", "http://localhost:3000"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# init DB on import
init_db()

# (dev) show DB file once on startup for clarity
@app.on_event("startup")
def show_db_path():
    print("SQLite DB:", DB_FILE)

# ---------- endpoints ----------
@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True}

@app.post("/auth/signup")
def signup(payload: SignUpIn):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE email=?", (payload.email,))
    if cur.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Email already registered")

    salt = new_salt()
    pw_hash = hash_pw(payload.password, salt)
    favorites_json = json.dumps(payload.favorite_players or [])
    plan = payload.plan or "Free"

    try:
        cur.execute(
            "INSERT INTO users (email, password_hash, salt, dob, country, plan, favorites_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (payload.email, pw_hash, salt, payload.dob, payload.country, plan, favorites_json, now_iso())
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}

@app.post("/auth/login", response_model=LoginOut)
def login(payload: LoginIn):
    import hmac
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email=?", (payload.email,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid credentials")

    salt = row["salt"]
    if not hmac.compare_digest(hash_pw(payload.password, salt), row["password_hash"]):
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid credentials")

    token = uuid.uuid4().hex
    cur.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                (token, row["id"], now_iso()))
    conn.commit()
    user = user_row_to_dict(row)
    conn.close()
    return {"token": token, "user": user}

@app.get("/me", response_model=ProfileOut)
def me(user_id: int = Depends(require_auth)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return user_row_to_dict(row)

@app.patch("/me", response_model=ProfileOut)
def update_me(patch: ProfilePatch, user_id: int = Depends(require_auth)):
    import json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    dob = patch.dob if patch.dob is not None else row["dob"]
    country = patch.country if patch.country is not None else row["country"]
    plan = patch.plan if patch.plan is not None else row["plan"]
    favs = json.dumps(patch.favorite_players) if patch.favorite_players is not None else row["favorites_json"]

    cur.execute("UPDATE users SET dob=?, country=?, plan=?, favorites_json=? WHERE id=?",
                (dob, country, plan, favs, user_id))
    conn.commit()

    cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
    row2 = cur.fetchone()
    conn.close()
    return user_row_to_dict(row2)

@app.post("/logout")
def logout(user_id: int = Depends(require_auth), authorization: Optional[str] = Header(None)):
    token = authorization.split(" ", 1)[1].strip() if authorization else ""
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/logout_all")
def logout_all(user_id: int = Depends(require_auth)):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# --- password reset ---
@app.post("/auth/request_reset")
def request_reset(body: PasswordResetRequestIn):
    """
    Always returns ok (to avoid email enumeration).
    If user exists -> create code, email code.
    """
    email = body.email
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email=?", (email,))
    row = cur.fetchone()
    conn.close()

    # Create/send code only if user exists
    if row:
        code = create_reset_code(email)
        send_email_code(email, code, mail_type="Reset")

    return {"ok": True}

@app.post("/auth/verify_reset")
def verify_reset(body: VerifyResetIn):
    ok = verify_reset_code(body.email, body.code)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    # If you later add a "Set New Password" screen, return a short-lived token here.
    return {"ok": True}


# --- chat ---
@app.post("/chat")
async def chat(body: ChatIn) -> Dict[str, Any]:
    """
    Returns:
      {
        "response": "<narrative text only>",
        "data": {"players": [...]},
        "response_parts": [...]
      }
    """
    result = answer_question(
        body.message,
        session_id=body.session_id or "default",
        strategy=body.strategy
    )
    answer_text = (result.get("answer") or "").strip()
    payload = result.get("data") or {"players": []}
    return {
        "response": answer_text,
        "data": payload,
        "response_parts": split_response_parts(answer_text),
    }

@app.post("/reset")
async def reset(session_id: str) -> Dict[str, Any]:
    reset_session(session_id)
    return {"ok": True, "session_id": session_id, "reset": True}
