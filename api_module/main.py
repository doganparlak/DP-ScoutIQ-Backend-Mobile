# api_module/main.py
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, Depends, Header, status, Response
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv() 

from sqlalchemy.orm import Session
from sqlalchemy import text

from chatbot_module.chatbot import answer_question
# import our refactored pieces
from api_module.utilities import (
    hash_pw, new_salt, now_iso, get_user_email_by_id, delete_user_everywhere, get_bearer_token, revoke_session,
    user_row_to_dict, require_auth, create_email_code, verify_email_code, send_email_code, to_long_roles, normalize_lang, get_user_language,
    session_exists_and_active, delete_chat_messages, split_response_parts, pick, send_reachout_email,
)
from api_module.payment_utilities import(
     verify_ios_subscription, verify_android_subscription, run_subscription_sync
)
from api_module.database import get_db
from api_module.models import (
    SignUpIn, LoginIn, LoginOut, ProfileOut, ProfilePatch, SetNewPasswordIn,
    PasswordResetRequestIn, VerifyResetIn, VerifySignupIn, SignupCodeRequestIn, ChatIn,
    FavoritePlayerIn, FavoritePlayerOut, ReachOutIn, PlanUpdateIn, IAPActivateIn
)

import hmac, uuid, json, re, os
import datetime as dt

PASSWORD_RE = re.compile(r'^(?=.*[A-Za-z])(?=.*\d).{8,}$')

ADMIN_SUBSCRIPTION_SYNC_TOKEN = os.getenv("SUBSCRIPTION_SYNC_TOKEN", "")
IOS_PRO_PRODUCT_ID = os.getenv("IOS_PRO_PRODUCT_ID", "scoutwise_pro_monthly_ios")
ANDROID_PRO_PRODUCT_ID = os.getenv("ANDROID_PRO_PRODUCT_ID", "scoutwise_pro_monthly_android")


app = FastAPI()

# CORS
origins_env = os.environ.get("CORS_ORIGINS")
origins = [o.strip() for o in origins_env.split(",")] if origins_env else ["http://localhost:19006"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- endpoints ----------
@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True}

@app.post("/auth/signup")
def signup(payload: SignUpIn, db: Session = Depends(get_db)):
    if not PASSWORD_RE.match(payload.password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters and include at least one letter and one number.",
        )

    email_norm = payload.email.strip()

    # Block if already a real user
    exists = db.execute(
        text("SELECT 1 FROM users WHERE lower(email) = lower(:e)"),
        {"e": email_norm}
    ).first()
    if exists:
        raise HTTPException(status_code=400, detail="Email already registered")

    salt = new_salt()
    pw_hash = hash_pw(payload.password, salt)

    db.execute(text("""
        INSERT INTO pending_signups (email, password_hash, salt, dob, country, plan, favorites_json, newsletter, created_at)
        VALUES (:email, :ph, :salt, CAST(:dob AS date), :country, :plan, CAST(:favs AS jsonb), CAST(:newsletter AS boolean), NOW())
        ON CONFLICT (email) DO UPDATE
        SET password_hash = EXCLUDED.password_hash,
            salt          = EXCLUDED.salt,
            dob           = EXCLUDED.dob,
            country       = EXCLUDED.country,
            plan          = EXCLUDED.plan,
            favorites_json= EXCLUDED.favorites_json,
            newsletter    = EXCLUDED.newsletter,
            created_at    = NOW()
    """), {
        "email": email_norm,
        "ph": pw_hash,
        "salt": salt,
        "dob": payload.dob or None,
        "country": payload.country,
        "plan": (payload.plan or "Free"),
        "favs": json.dumps(payload.favorite_players or [], ensure_ascii=False),
        "newsletter": bool(payload.newsletter),
    })
    db.commit()

    # No code here — client should call /auth/request_signup_code next
    return {"ok": True}

@app.post("/auth/login", response_model=LoginOut)
def login(payload: LoginIn, accept_language: str | None = Header(default=None), db: Session = Depends(get_db)):
    row = db.execute(text("SELECT * FROM users WHERE email = :e"), {"e": payload.email}).mappings().first()
    if not row:
        raise HTTPException(status_code=400, detail="Invalid credentials")

    salt = row["salt"]
    if not hmac.compare_digest(hash_pw(payload.password, salt), row["password_hash"]):
        raise HTTPException(status_code=400, detail="Invalid credentials")

    # Determine preferred language
    preferred = normalize_lang(payload.uiLanguage) or normalize_lang(accept_language)
    if preferred:
        db.execute(text("UPDATE users SET language = :l WHERE id = :id"), {"l": preferred, "id": row["id"]})
        db.commit()
        row = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": row["id"]}).mappings().first()

    token = uuid.uuid4().hex
    lang_for_session = normalize_lang(row.get("language")) or "en"
    db.execute(
        text("""
        INSERT INTO sessions (token, user_id, language, created_at, ended_at)
        VALUES (:t, :uid, :l, :ts, NULL)
        """),
        {"t": token, "uid": row["id"], "l": lang_for_session, "ts": now_iso()}
    )
    db.commit()

    user = user_row_to_dict(row)
    ''''
    print(json.dumps({
    "event": "login_success",
    "user_id": user["id"],
    "email": user["email"],
    "plan": user.get("plan"),
    "uiLanguage": user.get("uiLanguage"),
    "created_at": user.get("created_at"),
    }, ensure_ascii=False, default=str)) 
    '''

    return {"token": token, "user": user}

@app.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(authorization: str | None = Header(None), db: Session = Depends(get_db)):
    try:
        token = get_bearer_token(authorization)
    except HTTPException:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    revoke_session(db, token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

@app.post("/auth/set_new_password")
def set_new_password(
    body: SetNewPasswordIn,
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    # Determine preferred language: DB (by email) -> Accept-Language -> 'en'
    row_lang = db.execute(
        text("SELECT language FROM users WHERE lower(email) = lower(:e)"),
        {"e": body.email},
    ).mappings().first()
    preferred_lang = normalize_lang((row_lang or {}).get("language")) or normalize_lang(accept_language) or "en"

    # 1) Password strength check
    if not PASSWORD_RE.match(body.new_password):
        raise HTTPException(
            status_code=400,
            detail=pick(preferred_lang, "weak_pw"),
            headers={"Content-Language": preferred_lang},
        )

    # 2) Fetch user creds
    row = db.execute(
        text("SELECT id, password_hash, salt FROM users WHERE lower(email) = lower(:e)"),
        {"e": body.email}
    ).mappings().first()

    if not row:
        # Don't reveal whether the email exists; just return OK
        return {"ok": True}

    user_id = int(row["id"])
    old_hash = row["password_hash"]
    old_salt = row["salt"]

    # 3) Prevent reusing the same password
    new_with_old_salt = hash_pw(body.new_password, old_salt)
    if hmac.compare_digest(new_with_old_salt, old_hash):
        raise HTTPException(
            status_code=400,
            detail=pick(preferred_lang, "same_pw"),
            headers={"Content-Language": preferred_lang},
        )

    # 4) Update password + invalidate sessions
    fresh_salt = new_salt()
    fresh_hash = hash_pw(body.new_password, fresh_salt)

    db.execute(
        text("UPDATE users SET password_hash = :ph, salt = :s WHERE id = :id"),
        {"ph": fresh_hash, "s": fresh_salt, "id": user_id}
    )
    db.execute(text("DELETE FROM sessions WHERE user_id = :id"), {"id": user_id})
    db.commit()

    return {"ok": True}

@app.get("/me", response_model=ProfileOut)
def me(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    row = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return user_row_to_dict(row)

@app.patch("/me", response_model=ProfileOut)
def update_me(patch: ProfilePatch, user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    row = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    dob = patch.dob if patch.dob is not None else row["dob"]
    country = patch.country if patch.country is not None else row["country"]
    plan = patch.plan if patch.plan is not None else row["plan"]
    favs = json.dumps(patch.favorite_players) if patch.favorite_players is not None else row["favorites_json"]

    db.execute(
        text("UPDATE users SET dob = CAST(:dob AS date), country = :country, plan = :plan, favorites_json = :favs WHERE id = :id"),
        {"dob": dob, "country": country, "plan": plan, "favs": favs, "id": user_id}
    )
    db.commit()

    row2 = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).mappings().first()
    return user_row_to_dict(row2)


@app.post("/logout_all")
def logout_all(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    db.execute(text("DELETE FROM sessions WHERE user_id = :id"), {"id": user_id})
    db.commit()
    return {"ok": True}

# --- email codes: reset ---
@app.post("/auth/request_reset")
def request_reset(body: PasswordResetRequestIn, db: Session = Depends(get_db)):
    email = body.email
    row = db.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email}).mappings().first()
    if row:
        code = create_email_code(email, purpose="reset")
        send_email_code(email, code, mail_type="reset")
    return {"ok": True}

@app.post("/help/reach_out")
def reach_out(
    body: ReachOutIn,
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    msg = (body.message or "").strip()
    if not msg or len(msg) > 2000:
        return {"ok": True}

    # Use the authenticated user's email in the subject
    email = get_user_email_by_id(db, user_id) or "unknown@user"
    try:
        send_reachout_email(email, msg)
    except Exception:
        return {"ok": True}

    return {"ok": True}

@app.post("/auth/verify_reset")
def verify_reset(body: VerifyResetIn):
    ok = verify_email_code(body.email, body.code, purpose="reset")
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    return {"ok": True}

# --- email codes: signup ---
@app.post("/auth/request_signup_code")
def request_signup_code(body: SignupCodeRequestIn, db: Session = Depends(get_db)):
    email = body.email.strip()
    staged_exists = db.execute(
        text("SELECT 1 FROM pending_signups WHERE lower(email) = lower(:e)"),
        {"e": email}
    ).first()
    if not staged_exists:
        db.execute(text("""
            INSERT INTO pending_signups (email, password_hash, salt, created_at)
            VALUES (:email, '', '', NOW())
            ON CONFLICT (email) DO NOTHING
        """), {"email": email})
        db.commit()

    code = create_email_code(email, purpose="signup")
    send_email_code(email, code, mail_type="signup")
    return {"ok": True}

@app.post("/auth/verify_signup_code")
def verify_signup_code(body: VerifySignupIn, db: Session = Depends(get_db)):
    email = body.email.strip()

    if not verify_email_code(email, body.code, purpose="signup"):
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    ps = db.execute(text("""
        SELECT email, password_hash, salt, dob, country, plan, favorites_json, newsletter
        FROM pending_signups
        WHERE lower(email) = lower(:e)
    """), {"e": email}).mappings().first()
    if not ps:
        raise HTTPException(status_code=400, detail="No pending signup for this email")

    # If user already created meanwhile, just clean staged row and return ok
    already = db.execute(
        text("SELECT 1 FROM users WHERE lower(email) = lower(:e)"),
        {"e": email}
    ).first()
    if not already:
        db.execute(text("""
            INSERT INTO users
            (email, password_hash, salt, dob, country, plan, favorites_json, created_at, language, newsletter)
            VALUES
            (:email, :ph, :salt, CAST(:dob AS date), :country, :plan, CAST(:favs AS jsonb), NOW(), NULL, :newsletter)
        """), {
            "email": ps["email"],
            "ph": ps["password_hash"],
            "salt": ps["salt"],
            "dob": ps["dob"],
            "country": ps["country"],
            "plan": ps["plan"] or "Free",
            "favs": ps["favorites_json"],        # already jsonb
            "newsletter": bool(ps["newsletter"]),
        })

    db.execute(text("DELETE FROM pending_signups WHERE lower(email) = lower(:e)"), {"e": email})
    db.commit()
    return {"ok": True}



@app.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_me(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        email = get_user_email_by_id(db, user_id)
        if not email:
            raise HTTPException(status_code=404, detail="User not found")
        delete_user_everywhere(db, user_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Could not delete account")
    return Response(status_code=status.HTTP_204_NO_CONTENT)

# --- chat ---
@app.post("/chat")
async def chat(body: ChatIn, user_id: int = Depends(require_auth), db: Session = Depends(get_db)) -> Dict[str, Any]:
    session_id = body.session_id or "default"

    try:
        if not session_exists_and_active(db, session_id):
            lang = normalize_lang(get_user_language(db, user_id)) or "en"
            # emulate SQLite INSERT OR REPLACE with UPSERT
            db.execute(
                text("""
                INSERT INTO sessions (token, user_id, language, created_at, ended_at)
                VALUES (:t, :uid, :l, :ts, NULL)
                ON CONFLICT (token) DO UPDATE
                SET user_id = EXCLUDED.user_id,
                    language = EXCLUDED.language,
                    ended_at = NULL
                """),
                {"t": session_id, "uid": user_id, "l": lang, "ts": now_iso()}
            )
            db.commit()
    finally:
        pass

    result = answer_question(
        body.message,
        session_id=session_id,
        strategy=body.strategy,
    )
    answer_text = (result.get("answer") or "").strip()
    payload = result.get("data") or {"players": []}
    return {
        "response": answer_text,
        "data": payload,
        "response_parts": split_response_parts(answer_text),
    }

@app.post("/reset")
async def reset(session_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Ends a chat: drops persisted history and marks the session ended.
    Next message with the same session_id will create a fresh session row.
    """
    # delete chat history
    delete_chat_messages(db, session_id)

    # mark session ended (soft logout for this token)
    db.execute(
        text("UPDATE sessions SET ended_at = :ts WHERE token = :t AND ended_at IS NULL"),
        {"ts": now_iso(), "t": session_id}
    )
    db.commit()

    return {"ok": True, "session_id": session_id, "reset": True}

# --- favorite players ---
@app.get("/me/favorites", response_model=List[FavoritePlayerOut])
def list_favorites(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    rows = db.execute(
        text("""
        SELECT id,
               name,
               nationality,
               age,
               potential,
               gender,
               height,
               weight,
               team,
               roles_json
        FROM favorite_players
        WHERE user_id = :uid
        ORDER BY created_at DESC
        """),
        {"uid": user_id}
    ).mappings().all()

    out: List[FavoritePlayerOut] = []
    for r in rows:
        val = r["roles_json"]
        if isinstance(val, str):
            try:
                roles = json.loads(val) or []
            except Exception:
                roles = []
        elif isinstance(val, (list, tuple)):
            roles = list(val)
        elif val is None:
            roles = []
        else:
            try:
                roles = list(val)  # best effort
            except Exception:
                roles = []

        out.append(FavoritePlayerOut(
            id=r["id"],
            name=r["name"],
            nationality=r["nationality"],
            age=r["age"],
            potential=r["potential"],
            gender=r["gender"],
            height=r["height"],
            weight=r["weight"],
            team=r["team"],
            roles=roles,
        ))
    return out

@app.post("/me/favorites", response_model=FavoritePlayerOut, status_code=status.HTTP_201_CREATED)
def add_favorite(
    payload: FavoritePlayerIn,
    user_id: int = Depends(require_auth),
    response: Response = None,
    db: Session = Depends(get_db),
):
    roles_long = to_long_roles(payload.roles)

    existing = db.execute(
        text("""
        SELECT id, name, nationality, age, potential, gender, height, weight, team, roles_json
        FROM favorite_players
        WHERE user_id = :uid
          AND lower(name) = lower(:name)
          AND lower(COALESCE(nationality, '')) = lower(COALESCE(:nat, ''))
          AND COALESCE(age, -1) = COALESCE(:age, -1)
        LIMIT 1
        """),
        {
            "uid": user_id,
            "name": payload.name,
            "nat": payload.nationality,
            "age": payload.age,
        }
    ).mappings().first()

    if existing:
        try:
            existing_roles = json.loads(existing["roles_json"]) or []
        except Exception:
            existing_roles = []
        if response is not None:
            response.status_code = status.HTTP_200_OK
        return FavoritePlayerOut(
            id=existing["id"],
            name=existing["name"],
            nationality=existing["nationality"],
            age=existing["age"],
            potential=existing["potential"],
            gender=existing["gender"],
            height=existing["height"],
            weight=existing["weight"],
            team=existing["team"],
            roles=existing_roles,
        )

    fav_id = uuid.uuid4().hex
    created_at = now_iso()

    db.execute(
        text("""
        INSERT INTO favorite_players (
            id,
            user_id,
            name,
            nationality,
            age,
            potential,
            gender,
            height,
            weight,
            team,
            roles_json,
            created_at
        )
        VALUES (
            :id,
            :uid,
            :name,
            :nat,
            :age,
            :pot,
            :gender,
            :height,
            :weight,
            :team,
            :roles,
            :ts
        )
        """),
        {
            "id": fav_id,
            "uid": user_id,
            "name": payload.name,
            "nat": payload.nationality,
            "age": payload.age,
            "pot": payload.potential,
            "gender": payload.gender,
            "height": payload.height,
            "weight": payload.weight,
            "team": payload.team,
            "roles": json.dumps(roles_long, ensure_ascii=False),
            "ts": created_at,
        }
    )
    db.commit()

    return FavoritePlayerOut(
        id=fav_id,
        name=payload.name,
        nationality=payload.nationality,
        age=payload.age,
        potential=payload.potential,
        gender=payload.gender,
        height=payload.height,
        weight=payload.weight,
        team=payload.team,
        roles=roles_long,
    )



@app.delete("/me/favorites/{favorite_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_favorite(favorite_id: str, user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    res = db.execute(
        text("DELETE FROM favorite_players WHERE id = :id AND user_id = :uid"),
        {"id": favorite_id, "uid": user_id}
    )
    deleted = res.rowcount or 0
    db.commit()

    if deleted == 0:
        raise HTTPException(status_code=404, detail="Favorite not found")

    return Response(status_code=status.HTTP_204_NO_CONTENT)

@app.post("/me/subscription/iap")
def activate_subscription(
    body: IAPActivateIn,
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    # Only allow our known subscription SKUs
    allowed_product_ids = {IOS_PRO_PRODUCT_ID, ANDROID_PRO_PRODUCT_ID}
    if body.product_id not in allowed_product_ids:
        raise HTTPException(status_code=400, detail="Unknown product")

    if body.platform == "ios":
        ok, expires_at, auto_renew = verify_ios_subscription(
            IOS_PRO_PRODUCT_ID, 
            body.external_id
        )
    else:
        ok, expires_at, auto_renew = verify_android_subscription(
            ANDROID_PRO_PRODUCT_ID, 
            body.external_id, 
            body.receipt
        )

    if not ok:
        raise HTTPException(status_code=400, detail="Could not verify purchase")

    db.execute(
        text("""
            UPDATE users
            SET plan                     = 'Pro',
                subscription_platform    = :platform,
                subscription_external_id = :ext_id,
                subscription_end_at      = :end_at,
                subscription_auto_renew  = :auto_renew,
                subscription_last_checked_at = :checked_at,
                subscription_receipt     = :receipt
            WHERE id = :id
        """),
        {
            "platform": body.platform,
            "ext_id": body.external_id,
            "end_at": expires_at.isoformat(),
            "auto_renew": auto_renew,
            "checked_at": now_iso(),
            "receipt": body.receipt,  # can be None
            "id": user_id,
        },
    )
    db.commit()

    return {
        "ok": True,
        "plan": "Pro",
        "subscriptionEndAt": expires_at.isoformat(),
    }

# OPTIONAL ENDPOINT: run subscription sync via endpoint
@app.post("/internal/subscriptions/sync")
def sync_subscriptions(
    x_admin_token: str = Header(..., alias="X-Admin-Token"),
    db: Session = Depends(get_db),
):

    if not ADMIN_SUBSCRIPTION_SYNC_TOKEN or not hmac.compare_digest(
        x_admin_token,
        ADMIN_SUBSCRIPTION_SYNC_TOKEN,
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")

    run_subscription_sync(db)
    return {"ok": True}
