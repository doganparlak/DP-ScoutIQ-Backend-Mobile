# api_module/utilities.py
from __future__ import annotations
from typing import Optional, Dict, Any
from fastapi import Depends, Header, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pydantic import EmailStr
import os, sqlite3, uuid, hashlib, hmac, json, datetime as dt
import random, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --- Email settings (you provided) ---
settings: Dict[str, Any] = {
    "email": {
        "sender_email": "dogan.parlak.1404@gmail.com",
        "smtp_server": "smtp.gmail.com",
        "sender_password": "kouq fqhn rsgh smrf",  # Gmail App Password (ok for local dev)
        "smtp_port": 587,
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
      created_at TEXT NOT NULL
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
      token TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL,
      created_at TEXT NOT NULL,
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
    cur.execute("CREATE INDEX IF NOT EXISTS idx_email_codes_email ON email_codes(email);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_email_codes_purpose ON email_codes(purpose);")
    conn.commit()
    conn.close()

# --- utilities ---
def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

def new_salt() -> str:
    return uuid.uuid4().hex

def now_iso() -> str:
    return dt.datetime.utcnow().isoformat()

def user_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "dob": row["dob"],
        "country": row["country"],
        "plan": row["plan"],
        "favorite_players": json.loads(row["favorites_json"] or "[]"),
        "created_at": row["created_at"],
    }
# ----- deletion helpers -----
def get_user_email_by_id(conn: sqlite3.Connection, user_id: int) -> Optional[str]:
    cur = conn.cursor()
    cur.execute("SELECT email FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    return row["email"] if row else None

def delete_user_everywhere(conn: sqlite3.Connection, user_id: int) -> None:
    """
    Hard-delete the user and related artifacts from SQLite:
      - sessions (by user_id)
      - email_codes (by email)
      - users (by id)
    """
    email = get_user_email_by_id(conn, user_id)
    if not email:
        # Nothing to delete
        return
    cur = conn.cursor()
    try:
        # Delete auth sessions first
        cur.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        # Delete email codes for this user
        cur.execute("DELETE FROM email_codes WHERE email=?", (email,))
        # Finally delete the user record
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
    created = dt.datetime.fromisoformat(row["created_at"])
    if dt.datetime.utcnow() - created > dt.timedelta(minutes=expiry_minutes):
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