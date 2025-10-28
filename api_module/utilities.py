# api_module/utilities.py
from __future__ import annotations
from typing import Optional, Dict, Any, List
from fastapi import Depends, Header, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pydantic import EmailStr
import os, sqlite3, uuid, hashlib, hmac, json, datetime as dt
import random, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone

# --- Email settings (you provided) ---
settings: Dict[str, Any] = {
    "email": {
        "sender_email": os.environ.get("SMTP_SENDER_EMAIL", ""),
        "smtp_server": os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
        "sender_password": os.environ.get("SMTP_APP_PASSWORD", ""),
        "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
    }
}

# --- DB paths ---
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "user_db"))
os.makedirs(DB_PATH, exist_ok=True)
DB_FILE = os.path.join(DB_PATH, "app.db")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
# --- DB helpers ---
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
    except Exception:
        pass
    return conn

def init_db() -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      email TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      salt TEXT NOT NULL,
      dob TEXT,
      country TEXT,
      plan TEXT DEFAULT 'Free',
      favorites_json TEXT DEFAULT '[]',
      created_at TEXT NOT NULL,
      language TEXT,
      newsletter INTEGER NOT NULL DEFAULT 0   -- 0 = no, 1 = yes
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
      token TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL,
      language TEXT,
      created_at TEXT NOT NULL,
      ended_at TEXT,
      FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS email_codes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      email TEXT NOT NULL,
      code TEXT NOT NULL,
      purpose TEXT NOT NULL,           -- 'signup' | 'reset'
      created_at TEXT NOT NULL,
      used INTEGER NOT NULL DEFAULT 0
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS favorite_players (
      id TEXT PRIMARY KEY,                -- uuid string
      user_id INTEGER NOT NULL,           -- fk to users.id
      name TEXT NOT NULL,
      nationality TEXT,
      age INTEGER,
      potential INTEGER,                  -- 0..100 (nullable)
      roles_json TEXT NOT NULL,           -- JSON array of LONG role names, e.g. ["Center Back","Right Back"]
      created_at TEXT NOT NULL,           -- ISO timestamp
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_token TEXT NOT NULL,            -- FK to sessions.token
      role TEXT NOT NULL CHECK(role IN ('human','ai','system')),
      content TEXT NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY(session_token) REFERENCES sessions(token) ON DELETE CASCADE
    );
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_token);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_time ON chat_messages(created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_email_codes_email ON email_codes(email);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_email_codes_purpose ON email_codes(purpose);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);")
    conn.commit()
    conn.close()


def append_chat_message(conn, session_token: str, role: str, content: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chat_messages (session_token, role, content, created_at) VALUES (?, ?, ?, ?)",
        (session_token, role, content, now_iso())
    )
    conn.commit()

def delete_chat_messages(conn, session_token: str) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM chat_messages WHERE session_token=?", (session_token,))
    conn.commit()

def load_chat_messages(conn, session_token: str) -> List[Dict[str, str]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content FROM chat_messages WHERE session_token=? ORDER BY id ASC",
        (session_token,)
    )
    rows = cur.fetchall() or []
    return [{"role": r["role"], "content": r["content"]} for r in rows]

def session_exists_and_active(cur, session_id: str) -> bool:
    cur.execute("SELECT 1 FROM sessions WHERE token=? AND ended_at IS NULL", (session_id,))
    return cur.fetchone() is not None


# --- utilities ---
def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

def new_salt() -> str:
    return uuid.uuid4().hex

def now_iso() -> str:
     return datetime.now(timezone.utc).isoformat()

def user_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "dob": row["dob"],
        "country": row["country"],
        "plan": row["plan"],
        "favorite_players": json.loads(row["favorites_json"] or "[]"),
        "created_at": row["created_at"],
        "uiLanguage": row["language"],     # 'en' | 'tr' | None
    }
# ----- deletion helpers -----
def get_user_email_by_id(conn: sqlite3.Connection, user_id: int) -> Optional[str]:
    cur = conn.cursor()
    cur.execute("SELECT email FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    return row["email"] if row else None

def delete_user_everywhere(conn: sqlite3.Connection, user_id: int) -> None:
    """
    Hard-delete a user and all related data:

      - sessions (by user_id)  -> cascades to chat_messages
      - chat_messages (defensive fallback if cascade isn't active)
      - email_codes (by email)
      - users (by id)          -> cascades to favorite_players

    Requires: sessions.token is FK for chat_messages.session_token (ON DELETE CASCADE)
              favorite_players.user_id FK to users(id) (ON DELETE CASCADE)
    """
    email = get_user_email_by_id(conn, user_id)
    if not email:
        return

    cur = conn.cursor()
    try:
        # Ensure cascades are honored on this connection
        cur.execute("PRAGMA foreign_keys = ON;")

        # Collect session tokens up-front (for defensive cleanup if needed)
        cur.execute("SELECT token FROM sessions WHERE user_id=?", (user_id,))
        tokens = [row["token"] for row in cur.fetchall() or []]

        # 1) Delete auth/chat sessions (should cascade to chat_messages)
        cur.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

        # 1a) Defensive fallback: if FK cascade wasn't active previously,
        #     explicitly remove chat messages for those session tokens.
        if tokens:
            # Use executemany to avoid huge IN clauses; small lists can use IN (...)
            cur.executemany(
                "DELETE FROM chat_messages WHERE session_token=?",
                [(t,) for t in tokens],
            )

        # 2) Delete email verification/reset codes tied to this user
        cur.execute("DELETE FROM email_codes WHERE email=?", (email,))

        # 3) Delete the user (cascades to favorite_players)
        cur.execute("DELETE FROM users WHERE id=?", (user_id,))

        conn.commit()
    except Exception:
        conn.rollback()
        raise

# --- auth dependency ---
def require_auth(authorization: Optional[str] = Header(None)) -> int:
    """
    Returns the authenticated user_id or raises 401.
    Expects Authorization: Bearer <token>
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing/invalid Authorization")
    token = authorization.split(" ", 1)[1].strip()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM sessions WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid token")
    return int(row["user_id"])

# --- Logout helpers ---
def get_bearer_token(authorization: Optional[str]) -> str:
    """
    Extract the bearer token string or raise 401 for missing/invalid headers.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing/invalid Authorization")
    return authorization.split(" ", 1)[1].strip()

def revoke_session(conn: sqlite3.Connection, token: str) -> None:
    """
    Delete a single session row for this token. Idempotent (no error if not found).
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()

# ---------- email-code helpers ----------
def _gen_code() -> str:
    return f"{random.randint(0, 999999):06d}"

def create_email_code(email: str, purpose: str) -> str:
    code = _gen_code()
    conn = get_db()
    conn.execute(
        "INSERT INTO email_codes (email, code, purpose, created_at, used) VALUES (?, ?, ?, ?, 0)",
        (email, code, purpose, now_iso()),
    )
    conn.commit()
    conn.close()
    return code

def verify_email_code(email: str, code: str, purpose: str, expiry_minutes: int = 10) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, created_at, used FROM email_codes
        WHERE email=? AND code=? AND purpose=?
        ORDER BY id DESC LIMIT 1
    """, (email, code, purpose))
    row = cur.fetchone()
    if not row:
        conn.close(); return False
    if int(row["used"]) == 1:
        conn.close(); return False
    
    # Parse created_at robustly
    try:
        created = dt.datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    except Exception:
        # Fallback if stored in a non-ISO format (unlikely)
        conn.close(); return False
     
    # Normalize to aware UTC if the row happened to be stored naive in older data
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)

    now = dt.datetime.now(dt.timezone.utc)  # aware UTC
    if now - created >  dt.timedelta(minutes=expiry_minutes):
        conn.close(); return False
    
    cur.execute("UPDATE email_codes SET used=1 WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    return True

def send_email_code(receiver_email: str, code: str, mail_type: str) -> None:
    se = settings['email']['sender_email']
    spw = settings['email']['sender_password']
    host = settings['email']['smtp_server']
    port = settings['email']['smtp_port']

    if mail_type == 'reset':
        subject = 'Your ScoutIQ password reset code'
        body = (
            "Dear User,\n\n"
            f"Use this 6-digit code to reset your password:\n\n"
            f"{code}\n\n"
            "The code expires in 10 minutes.\n\n"
            "Best,\nScoutIQ Support"
        )
    else:  # 'signup'
        subject = 'Your ScoutIQ sign-up verification code'
        body = (
            "Dear User,\n\n"
            f"Use this 6-digit code to verify your email:\n\n"
            f"{code}\n\n"
            "The code expires in 10 minutes.\n\n"
            "Best,\nScoutIQ Support"
        )

    msg = MIMEMultipart()
    msg['From'] = se
    msg['To'] = receiver_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP(host, port)
        server.starttls()
        server.login(se, spw)
        server.sendmail(se, receiver_email, msg.as_string())
        print(f"[mail] sent {mail_type} code to {receiver_email}")
    except Exception as e:
        print(f"[mail] failed: {e}")
    finally:
        try: server.quit()
        except: pass

# ---------- ROLE Utilities ----------
ROLE_SHORT_TO_LONG = {
    "GK": "Goal Keeper",
    "LWB": "Left Wing Back",
    "LB": "Left Back",
    "LCB": "Left Center Back",
    "CB": "Center Back",
    "RCB": "Right Center Back",
    "RB": "Right Back",
    "RWB": "Right Wing Back",
    "LM": 'Left Midfield',
    "LCM": "Left Center Midfield",
    "CM": "Center Midfield",
    "CAM": 'Center Attacking Midfield',
    "CDM": "Center Defensive Midfield",
    "RCM": "Right Center Midfield",
    "RM": 'Right Midfield',
    "CF": "Center Forward",
    "RCF": "Right Center Forward",
    "LCF": "Left Center Forward",
    "LW": "Left Wing",
    "RW": "Right Wing",
}

ROLE_LONG_TO_SHORT = {v: k for k, v in ROLE_SHORT_TO_LONG.items()}

def to_long_roles(maybe_short_or_long_list):
    out = []
    for r in maybe_short_or_long_list or []:
        if r in ROLE_SHORT_TO_LONG:
            out.append(ROLE_SHORT_TO_LONG[r])
        elif r in ROLE_LONG_TO_SHORT:
            out.append(r)
        else:
            out.append(r)
    return out

#---------- Language Utilities ----------

def normalize_lang(value: Optional[str]) -> Optional[str]:
    """
    Accepts 'en'/'tr', 'EN'/'TR', 'English', 'Türkçe', or full BCP-47 like 'en-US'/'tr-TR'.
    Returns 'en' or 'tr' or None.
    """
    if not value:
        return None
    v = value.strip().lower()
    if v in {"en", "tr"}:
        return v
    if v.startswith("en"):
        return "en"
    if v.startswith("tr"):
        return "tr"
    if "english" in v:
        return "en"
    if "türkçe" in v or "turkish" in v:
        return "tr"
    return None

def get_user_language(conn: sqlite3.Connection, user_id: int) -> Optional[str]:
    cur = conn.cursor()
    cur.execute("SELECT language FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    return (row["language"] or None) if row else None

def set_session_language(conn, token: str, lang: Optional[str]) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE sessions SET language=? WHERE token=?", (lang, token))
    if cur.rowcount == 0:
        # Optional: create if not present (depends on your login flow)
        cur.execute(
            "INSERT INTO sessions(token, user_id, language, created_at) VALUES (?, ?, ?, ?)",
            (token, -1, lang, now_iso())  # -1 only if you truly don't know user_id here
        )
    conn.commit()

def get_session_language(conn, token: str) -> Optional[str]:
    cur = conn.cursor()
    cur.execute("SELECT language FROM sessions WHERE token=? AND ended_at IS NULL", (token,))
    row = cur.fetchone()
    return row["language"] if row and row["language"] else None

def mark_session_started(conn, token: str, user_id: int, lang: Optional[str]) -> None:
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO sessions(token, user_id, language, created_at, ended_at)
        VALUES (?, ?, ?, COALESCE((SELECT created_at FROM sessions WHERE token=?), ?), NULL)
    """, (token, user_id, lang, token, now_iso()))
    conn.commit()

def mark_session_ended(conn, token: str) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE sessions SET ended_at=? WHERE token=?", (now_iso(), token))
    conn.commit()
