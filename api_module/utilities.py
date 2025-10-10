# api_module/utilities.py
from __future__ import annotations
from typing import Optional, Dict, Any
from fastapi import Header, HTTPException
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
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
os.makedirs(DB_PATH, exist_ok=True)
DB_FILE = os.path.join(DB_PATH, "app.db")

# --- DB helpers ---
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
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

# --- password reset helpers ---
def gen_code() -> str:
    return f"{random.randint(0, 999999):06d}"

def create_reset_code(email: str) -> str:
    code = gen_code()
    conn = get_db()
    conn.execute(
        "INSERT INTO password_resets (email, code, created_at, used) VALUES (?, ?, ?, 0)",
        (email, code, now_iso())
    )
    conn.commit()
    conn.close()
    return code

def verify_reset_code(email: str, code: str, expiry_minutes: int = 10) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, created_at, used FROM password_resets
        WHERE email=? AND code=?
        ORDER BY id DESC LIMIT 1
    """, (email, code))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    if int(row["used"]) == 1:
        conn.close()
        return False
    # expiry check
    created = dt.datetime.fromisoformat(row["created_at"])
    if dt.datetime.utcnow() - created > dt.timedelta(minutes=expiry_minutes):
        conn.close()
        return False
    # mark used
    cur.execute("UPDATE password_resets SET used=1 WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    return True

def send_email_code(receiver_email: str, code: str, mail_type: str = "Reset") -> None:
    """
    Adapted from your template to send a 6-digit verification code (to match UI).
    """
    sender_email = settings['email']['sender_email']
    sender_password = settings['email']['sender_password']
    smtp_server = settings['email']['smtp_server']
    smtp_port = settings['email']['smtp_port']

    if mail_type == 'Reset':
        subject = 'Your ScoutIQ password reset code'
        body = (
            "Dear User,\n\n"
            f"Use the following 6-digit code to reset your password:\n\n"
            f"Verification Code: {code}\n\n"
            "This code will expire in 10 minutes. If you did not request this reset, you can ignore this message.\n\n"
            "Best regards,\nScoutIQ Support"
        )
    else:
        subject = 'Your ScoutIQ sign-up verification code'
        body = (
            "Dear User,\n\n"
            f"Use the following 6-digit code to verify your email:\n\n"
            f"Verification Code: {code}\n\n"
            "This code will expire in 10 minutes.\n\n"
            "Best regards,\nScoutIQ Support"
        )

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, receiver_email, msg.as_string())
        print(f"[mail] sent {mail_type} code to {receiver_email}")
    except Exception as e:
        print(f"[mail] failed to send email: {str(e)}")
    finally:
        try:
            server.quit()
        except:
            pass