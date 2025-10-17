# api_module/main.py
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Depends, Header, status, Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv() 

# your existing chat utilities
from chatbot_module.chatbot import answer_question, reset_session
from api_module.response_handler import split_response_parts

# import our refactored pieces
from api_module.utilities import (
    get_db, init_db, hash_pw, new_salt, now_iso, get_user_email_by_id, delete_user_everywhere, get_bearer_token, revoke_session,
    user_row_to_dict, require_auth, create_email_code, verify_email_code, send_email_code, DB_FILE
)
from api_module.models import (
    SignUpIn, LoginIn, LoginOut, ProfileOut, ProfilePatch, SetNewPasswordIn,
    PasswordResetRequestIn, VerifyResetIn, VerifySignupIn, SignupCodeRequestIn, ChatIn
)

import hmac, uuid, json, re, os

PASSWORD_RE = re.compile(r'^(?=.*[A-Za-z])(?=.*\d).{8,}$')

# (dev) show DB file once on startup for clarity
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    init_db()  # idempotent table creation
    if os.environ.get("ENV", "dev") != "prod":
        print("SQLite DB:", DB_FILE)
    yield

app = FastAPI(lifespan=lifespan)

# CORS (lock this down to your frontend origin in prod)
origins_env = os.environ.get("CORS_ORIGINS")
origins = [o.strip() for o in origins_env.split(",")] if origins_env else ["http://localhost:19006"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # e.g., ["http://localhost:19006", "http://localhost:3000"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



# ---------- endpoints ----------
@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True}

@app.post("/auth/signup")
def signup(payload: SignUpIn):
    # 1) server-side password validation
    if not PASSWORD_RE.match(payload.password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters and include at least one letter and one number.",
        )
    
    conn = get_db()
    cur = conn.cursor()

    # 2) check if email exists
    cur.execute("SELECT 1 FROM users WHERE email=?", (payload.email,))
    if cur.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Email already registered")

    # 3) create user
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

@app.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(authorization: str | None = Header(None)):
    """
    Log out only the *current* session by deleting the bearer token from `sessions`.
    Always returns 204 (idempotent).
    """
    try:
        token = get_bearer_token(authorization)
    except HTTPException:
        # If there's no/invalid header, still respond 204 to be idempotent
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    conn = get_db()
    try:
        revoke_session(conn, token)  # deletes the row if it exists
    finally:
        try:
            conn.close()
        except:
            pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)

@app.post("/auth/set_new_password")
def set_new_password(body: SetNewPasswordIn):
    # 1) Strength check (same as signup)
    if not PASSWORD_RE.match(body.new_password):
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters and include at least one letter and one number."
        )

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, password_hash, salt FROM users WHERE email=?", (body.email,))
    row = cur.fetchone()
    if not row:
        conn.close()
        # Don’t reveal if email exists
        return {"ok": True}

    user_id = int(row["id"])
    old_hash = row["password_hash"]
    old_salt = row["salt"]

    # 2) Block reuse: hash new password with the OLD salt and compare
    new_with_old_salt = hash_pw(body.new_password, old_salt)
    if hmac.compare_digest(new_with_old_salt, old_hash):
        conn.close()
        raise HTTPException(status_code=400, detail="New password must be different from your current password.")

    # 3) Save new password with a FRESH salt, then revoke all sessions
    fresh_salt = new_salt()
    fresh_hash = hash_pw(body.new_password, fresh_salt)

    cur.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?", (fresh_hash, fresh_salt, user_id))
    cur.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()
    return {"ok": True}

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

@app.post("/logout_all")
def logout_all(user_id: int = Depends(require_auth)):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# --- email codes: reset ---
@app.post("/auth/request_reset")
def request_reset(body: PasswordResetRequestIn):
    # always ok; only send if user exists
    email = body.email
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email=?", (email,))
    row = cur.fetchone(); conn.close()
    if row:
        code = create_email_code(email, purpose="reset")
        send_email_code(email, code, mail_type="reset")
    return {"ok": True}

@app.post("/auth/verify_reset")
def verify_reset(body: VerifyResetIn):
    ok = verify_email_code(body.email, body.code, purpose="reset")
    if not ok: raise HTTPException(status_code=400, detail="Invalid or expired code")
    return {"ok": True}

# --- email codes: signup ---
@app.post("/auth/request_signup_code")
def request_signup_code(body: SignupCodeRequestIn):
    # Only send if a user record exists (you create on /auth/signup first)
    email = body.email
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email=?", (email,))
    row = cur.fetchone(); conn.close()
    if row:
        code = create_email_code(email, purpose="signup")
        send_email_code(email, code, mail_type="signup")
    return {"ok": True}

@app.post("/auth/verify_signup_code")
def verify_signup_code(body: VerifySignupIn):
    ok = verify_email_code(body.email, body.code, purpose="signup")
    if not ok: raise HTTPException(status_code=400, detail="Invalid or expired code")
    return {"ok": True}

@app.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_me(user_id: int = Depends(require_auth)):
    conn = get_db()
    try:
        email = get_user_email_by_id(conn, user_id)
        if not email:
            raise HTTPException(status_code=404, detail="User not found")
        delete_user_everywhere(conn, user_id)
    except HTTPException:
        raise
    except Exception:
        # Any DB error becomes 500
        raise HTTPException(status_code=500, detail="Could not delete account")
    finally:
        try:
            conn.close()
        except:
            pass
    # 204 No Content
    return Response(status_code=status.HTTP_204_NO_CONTENT)

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
