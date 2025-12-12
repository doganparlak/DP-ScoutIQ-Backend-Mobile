# api_module/utilities.py
from __future__ import annotations
from typing import Optional, Dict, Any, List, Literal
from fastapi import Depends, Header, HTTPException
from fastapi.security import OAuth2PasswordBearer
import os, uuid, hashlib, json, datetime as dt
import random, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
import re
from sqlalchemy import text
from sqlalchemy.orm import Session
from collections.abc import Mapping
# DB session provider
from api_module.database import SessionLocal

PlanLiteral = Literal["Free", "Pro"]

MESSAGES = {
    "weak_pw": {
        "en": "Password must be at least 8 characters and include at least one letter and one number.",
        "tr": "Parola en az 8 karakter olmalı ve en az bir harf ile bir rakam içermelidir.",
    },
    "same_pw": {
        "en": "New password must be different from your current password.",
        "tr": "Yeni parola mevcut parolanızdan farklı olmalıdır.",
    },
}

def pick(lang: str | None, key: str) -> str:
    lang_norm = normalize_lang(lang) or "en"
    return MESSAGES[key].get(lang_norm, MESSAGES[key]["en"])

# --- Email settings (you provided) ---
settings: Dict[str, Any] = {
    "email": {
        "sender_email": os.environ.get("SMTP_SENDER_EMAIL", ""),
        "smtp_server": os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
        "sender_password": os.environ.get("SMTP_APP_PASSWORD", ""),
        "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
    }
}
IMG_TAG = re.compile(r'<img[^>]+src="([^"]+)"[^>]*>', re.IGNORECASE)
HTMLY_RE = re.compile(r'</?(table|thead|tbody|tr|td|th|ul|ol|li|div|p|h[1-6]|span)\b', re.IGNORECASE)
DB_FILE = "supabase://postgres"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
# --- DB helpers ---
def get_db() -> Session:
    return SessionLocal()

def append_chat_message(db: Session, session_token: str, role: str, content: str) -> None:
    db.execute(
        text("""
        INSERT INTO chat_messages (session_token, role, content, created_at)
        VALUES (:token, :role, :content, :ts)
        """),
        {"token": session_token, "role": role, "content": content, "ts": now_iso()}
    )
    db.commit()

def delete_chat_messages(db: Session, session_token: str) -> None:
    db.execute(text("DELETE FROM chat_messages WHERE session_token = :t"), {"t": session_token})
    db.commit()

def load_chat_messages(db: Session, session_token: str) -> List[Dict[str, str]]:
    rows = db.execute(
        text("""
        SELECT role, content
        FROM chat_messages
        WHERE session_token = :t
        ORDER BY id ASC
        """),
        {"t": session_token}
    ).mappings().all()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def session_exists_and_active(db: Session, session_id: str) -> bool:
    row = db.execute(
        text("SELECT 1 FROM sessions WHERE token = :t AND ended_at IS NULL"),
        {"t": session_id}
    ).first()
    return row is not None


# --- utilities ---
def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

def new_salt() -> str:
    return uuid.uuid4().hex

def now_iso() -> str:
     return datetime.now(timezone.utc).isoformat()

def _to_iso_date(val):
    # Always return str or None
    if val is None:
        return None
    # Accept date OR datetime
    try:
        # datetime.date (no time)
        import datetime as _dt
        if isinstance(val, _dt.datetime):
            return val.date().isoformat()
        if isinstance(val, _dt.date):
            return val.isoformat()
        # if it’s already a string, leave it
        if isinstance(val, str):
            return val
    except Exception:
        pass
    return str(val)

def _to_iso_datetime(val):
    if val is None:
        return None
    try:
        import datetime as _dt
        if isinstance(val, _dt.datetime):
            return val.isoformat()
        if isinstance(val, str):
            return val
    except Exception:
        pass
    return str(val)

def user_row_to_dict(row: any) -> dict:
    """
    Normalize a user row into JSON-serializable dict:
      - dob -> ISO date string or None
      - created_at -> ISO datetime string
      - favorite_players -> list
      - subscription* -> subscription info
    """
    if hasattr(row, "_mapping"):
        m = row._mapping
    elif isinstance(row, Mapping):
        m = row
    elif hasattr(row, "__dict__"):
        m = row.__dict__
    else:
        raise TypeError(f"Unsupported row type: {type(row)}")

    get = m.get

    favs = get("favorites_json")
    # Normalize favorites to a list
    if isinstance(favs, str):
        try:
            parsed = json.loads(favs)
        except Exception:
            parsed = []
        fav_list = parsed if isinstance(parsed, list) else []
    elif isinstance(favs, list):
        fav_list = favs
    else:
        fav_list = []

    return {
        "id": get("id"),
        "email": get("email"),
        "dob": _to_iso_date(get("dob")),
        "country": get("country"),
        "plan": get("plan"),
        "favorite_players": fav_list,
        "created_at": _to_iso_datetime(get("created_at")),
        "uiLanguage": get("language"),
        # NEW FIELDS FOR SUBSCRIPTION
        "subscriptionEndAt": _to_iso_datetime(get("subscription_end_at")),
        "subscriptionPlatform": get("subscription_platform"),
        "subscriptionAutoRenew": get("subscription_auto_renew"),
    }

# ----- deletion helpers -----
def get_user_email_by_id(db: Session, user_id: int) -> Optional[str]:
    row = db.execute(
        text("SELECT email FROM users WHERE id = :id"),
        {"id": user_id}
    ).mappings().first()
    return row["email"] if row else None

def delete_user_everywhere(db: Session, user_id: int) -> None:
    email = get_user_email_by_id(db, user_id)
    if not email:
        return

    # Gather tokens (defensive)
    tokens = [r["token"] for r in db.execute(
        text("SELECT token FROM sessions WHERE user_id = :uid"), {"uid": user_id}
    ).mappings().all()]

    # 1) Delete sessions (FK ON DELETE CASCADE should handle chat_messages)
    db.execute(text("DELETE FROM sessions WHERE user_id = :uid"), {"uid": user_id})

    # 1a) Defensive cleanup if cascade was not present previously
    if tokens:
        for t in tokens:
            db.execute(text("DELETE FROM chat_messages WHERE session_token = :t"), {"t": t})

    # 2) Delete email codes
    db.execute(text("DELETE FROM email_codes WHERE email = :e"), {"e": email})

    # 3) Delete user (cascades to favorite_players)
    db.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})

    db.commit()

# --- auth dependency ---
def require_auth(authorization: Optional[str] = Header(None)) -> int:
    """
    Returns the authenticated user_id or raises 401.
    Expects Authorization: Bearer <token>
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing/invalid Authorization")
    token = authorization.split(" ", 1)[1].strip()

    db = get_db()
    try:
        row = db.execute(
            text("SELECT user_id FROM sessions WHERE token = :t"),
            {"t": token}
        ).mappings().first()
    finally:
        db.close()

    if not row:
        raise HTTPException(status_code=401, detail="Invalid token")
    return int(row["user_id"])

# --- Logout helpers ---
def get_bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing/invalid Authorization")
    return authorization.split(" ", 1)[1].strip()

def revoke_session(db: Session, token: str) -> None:
    db.execute(text("DELETE FROM sessions WHERE token = :t"), {"t": token})
    db.commit()


# ---------- email-code helpers ----------
def _gen_code() -> str:
    return f"{random.randint(0, 999999):06d}"


def create_email_code(email: str, purpose: str) -> str:
    code = _gen_code()
    db = get_db()
    try:
        db.execute(
            text("""
            INSERT INTO email_codes (email, code, purpose, created_at, used)
            VALUES (:e, :c, :p, CAST(:ts AS timestamptz), :used)
            """),
            {
                "e": email,
                "c": code,
                "p": purpose,
                "ts": now_iso(),
                "used": False,          # << boolean, not 0
            }
        )
        db.commit()
    finally:
        db.close()
    return code

def verify_email_code(email: str, code: str, purpose: str, expiry_minutes: int = 10) -> bool:
    db = get_db()
    try:
        row = db.execute(
            text("""
            SELECT id, created_at, used
            FROM email_codes
            WHERE email = :e AND code = :c AND purpose = :p
            ORDER BY id DESC
            LIMIT 1
            """),
            {"e": email, "c": code, "p": purpose}
        ).mappings().first()
        if not row:
            return False

        # used is a boolean in Postgres
        if bool(row["used"]):
            return False

        # created_at parse (unchanged)
        try:
            created = dt.datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        except Exception:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)

        now = dt.datetime.now(dt.timezone.utc)
        if now - created > dt.timedelta(minutes=expiry_minutes):
            return False

        # mark as used with a boolean
        db.execute(text("UPDATE email_codes SET used = TRUE WHERE id = :id"), {"id": row["id"]})
        db.commit()
        return True
    finally:
        db.close()

def send_reachout_email(user_email: str, note: str) -> None:
    """
    Sends the user's message to *your* support inbox.
    We use the configured sender address as BOTH from/to (send to yourself),
    and put the user's email in the subject.
    """
    se = settings['email']['sender_email']
    spw = settings['email']['sender_password']
    host = settings['email']['smtp_server']
    port = settings['email']['smtp_port']

    subject = f"[ScoutWise Reach Out] From: {user_email}"
    timestamp = datetime.now(timezone.utc).isoformat()

    body = (
        "User message received via Help Center.\n\n"
        f"From: {user_email}\n"
        f"At:   {timestamp}\n\n"
        "Message:\n"
        f"{note}\n"
    )

    msg = MIMEMultipart()
    msg["From"] = se
    msg["To"] = se                 # send to yourself
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        server = smtplib.SMTP(host, port)
        server.starttls()
        server.login(se, spw)
        server.sendmail(se, se, msg.as_string())
        #print(f"[mail] reachout from {user_email} sent to {se}")
    except Exception as e:
        pass
    finally:
        try: server.quit()
        except: pass

def send_email_code(receiver_email: str, code: str, mail_type: str) -> None:
    se = settings['email']['sender_email']
    spw = settings['email']['sender_password']
    host = settings['email']['smtp_server']
    port = settings['email']['smtp_port']

    if mail_type == 'reset':
        subject = 'Your ScoutWise password reset code'
        body = (
            "Dear User,\n\n"
            f"Use this 6-digit code to reset your password:\n\n"
            f"{code}\n\n"
            "The code expires in 10 minutes.\n\n"
            "Best,\nScoutWise Support"
        )
    else:
        subject = 'Your ScoutWise sign-up verification code'
        body = (
            "Dear User,\n\n"
            f"Use this 6-digit code to verify your email:\n\n"
            f"{code}\n\n"
            "The code expires in 10 minutes.\n\n"
            "Best,\nScoutWise Support"
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
        #print(f"[mail] sent {mail_type} code to {receiver_email}")
    except Exception as e:
        pass
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
    "LDM": "Left Defensive Midfield",
    "LCM": "Left Center Midfield",
    "LAM": "Left Attacking Midfield",
    "CM": "Center Midfield",
    "CAM": 'Center Attacking Midfield',
    "CDM": "Center Defensive Midfield",
    "RCM": "Right Center Midfield",
    "RM": 'Right Midfield',
    "RDM": "Right Defensive Midfield",
    "RAM": 'Right Attacking Midfield',
    "CF": "Center Forward",
    "RCF": "Right Center Forward",
    "LCF": "Left Center Forward",
    "LW": "Left Wing",
    "RW": "Right Wing",
}

ROLE_LONG_TO_SHORT = {v: k for k, v in ROLE_SHORT_TO_LONG.items()}
# Add extra accepted long-form variants
ROLE_LONG_TO_SHORT.update({
    "Goalkeeper": "GK",
    "Goal Keeper": "GK",
    "Centre Back": "CB",
    "Attacking Midfield": "CAM",
    "Defensive Midfield": "CDM",
    "Centre Forward": "CF",
    "Attacker": "CF",
})

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

def get_user_language(db: Session, user_id: int) -> Optional[str]:
    row = db.execute(text("SELECT language FROM users WHERE id = :id"), {"id": user_id}).mappings().first()
    return (row["language"] or None) if row else None

def set_session_language(db: Session, token: str, lang: Optional[str]) -> None:
    db.execute(text("UPDATE sessions SET language = :l WHERE token = :t"), {"l": lang, "t": token})
    db.commit()

def get_session_language(db: Session, token: str) -> Optional[str]:
    row = db.execute(
        text("SELECT language FROM sessions WHERE token = :t AND ended_at IS NULL"),
        {"t": token}
    ).mappings().first()
    return row["language"] if row and row["language"] else None

def mark_session_started(db: Session, token: str, user_id: int, lang: Optional[str]) -> None:
    # Postgres doesn't have INSERT OR REPLACE; emulate with upsert:
    db.execute(
        text("""
        INSERT INTO sessions (token, user_id, language, created_at, ended_at)
        VALUES (:t, :uid, :l, COALESCE(
            (SELECT created_at FROM sessions WHERE token = :t),
            :now
        ), NULL)
        ON CONFLICT (token) DO UPDATE
        SET user_id = EXCLUDED.user_id,
            language = EXCLUDED.language,
            ended_at = NULL
        """),
        {"t": token, "uid": user_id, "l": lang, "now": now_iso()}
    )
    db.commit()

def mark_session_ended(db: Session, token: str) -> None:
    db.execute(text("UPDATE sessions SET ended_at = :now WHERE token = :t"),
               {"now": now_iso(), "t": token})
    db.commit()

# === SPLIT RESPONSE PARTS TOOL ===

def split_response_parts(html: str):
    """
    Split assistant HTML into parts: text, image, and html (tables/divs).
    Images are isolated; chunks that have HTML tags (e.g., <table>) are marked as 'html'
    so the frontend renders them directly (no streaming).
    """
    parts = []
    pos = 0
    html = html or ""

    for m in IMG_TAG.finditer(html):
        start, end = m.start(), m.end()
        if start > pos:
            chunk = html[pos:start].strip()
            if chunk:
                if HTMLY_RE.search(chunk):
                    parts.append({"type": "html", "html": chunk})   # NEW
                else:
                    parts.append({"type": "text", "html": chunk})
        src = m.group(1)
        parts.append({"type": "image", "src": src})
        pos = end

    if pos < len(html):
        tail = html[pos:].strip()
        if tail:
            if HTMLY_RE.search(tail):
                parts.append({"type": "html", "html": tail})        # NEW
            else:
                parts.append({"type": "text", "html": tail})

    return parts

# === USER PLAN CHECK ====
def is_user_pro(db: Session, user_id: int) -> bool:
    row = db.execute(text("""
        SELECT subscription_end_at
        FROM users
        WHERE id = :uid
        LIMIT 1
    """), {"uid": user_id}).mappings().first()

    if not row:
        return False

    # Pro if subscription_end_at exists and is in the future
    return row["subscription_end_at"] is not None and row["subscription_end_at"] > db.execute(text("NOW()")).scalar()


