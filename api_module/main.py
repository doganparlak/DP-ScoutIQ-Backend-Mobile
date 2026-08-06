# api_module/main.py
from typing import Optional, Dict, Any, List
import time
from fastapi import FastAPI, HTTPException, Depends, Header, status, Response, Body, BackgroundTasks, Query as FastAPIQuery
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv() 

from sqlalchemy.orm import Session
from sqlalchemy import text

from chatbot_module.chatbot_agentic import answer_question
from chatbot_module.tools_agentic import ensure_player_position_label_cache
from report_module.report import generate_report_content, normalize_mobile_report_format
# import our refactored pieces
from api_module.utilities import (
    hash_pw, new_salt, now_iso, get_user_email_by_id, delete_user_everywhere, get_bearer_token, revoke_session,
    user_row_to_dict, require_auth, create_email_code, verify_email_code, send_email_code, to_long_roles, normalize_lang, get_user_language,
    session_exists_and_active, delete_chat_messages, split_response_parts, pick, send_reachout_email,
    is_user_pro, plan_from_product_id
)
from api_module.payment_utilities import(
     verify_ios_subscription, verify_android_subscription, run_subscription_sync,
)
from api_module.analytics import (
    analytics_mode,
    get_favorite_player_snapshot,
    get_player_snapshot,
    record_analytics_event,
)
from api_module.database import get_db, SessionLocal
from api_module.models import (
    SignUpIn, LoginIn, LoginOut, ProfileOut, ProfilePatch, SetNewPasswordIn,
    PasswordResetRequestIn, VerifyResetIn, VerifySignupIn, SignupCodeRequestIn, ChatIn,
    FavoritePlayerIn, FavoritePlayerOut, ReachOutIn, PlanUpdateIn, IAPActivateIn, 
    ScoutingReportIn, ScoutingReportOut, ConsentPatch, PlayerPoolSearchIn,
    PlayerPoolSearchRow, PlayerPoolFilterOptionsOut, PlayerPoolPotentialOut,
    PlayerPoolFormOut, PlayerPoolWeeklyPopularIn, MatchupComparisonIn,
    MatchupComparisonOut, TutorialPatch, DailyScoutAnswerIn, DailyScoutChallengeOut,
    DailyScoutNicknameIn, DailyScoutNicknameOut, DailyScoutLeaderboardOut,
)
from player_pool_module.player_pool import (
    get_player_pool_filter_options,
    reveal_player_form,
    reveal_player_potential,
    search_players,
)
from player_pool_module.weekly_popular import (
    get_weekly_popular_players,
    record_player_search,
    record_weekly_popular_reveal,
)
from matchup_module.comparison import get_matchup_comparison
from daily_quiz_module.quiz import (
    get_daily_status,
    get_weekly_leaderboard,
    set_weekly_nickname,
    skip_daily_challenge,
    submit_daily_answer,
)
from tutorial_module.tutorial import tutorial_chat_response, tutorial_yamal_scouting_report

import hmac, uuid, json, re, os
import datetime as dt

PASSWORD_RE = re.compile(r'^(?=.*[A-Za-z])(?=.*\d).{8,}$')

ADMIN_SUBSCRIPTION_SYNC_TOKEN = os.getenv("SUBSCRIPTION_SYNC_TOKEN", "")
#IOS_PRO_PRODUCT_ID = os.getenv("IOS_PRO_PRODUCT_ID", "scoutwise_pro_monthly_ios")
#ANDROID_PRO_PRODUCT_ID = os.getenv("ANDROID_PRO_PRODUCT_ID", "scoutwise_pro_monthly_android")
IOS_PRO_MONTHLY_PRODUCT_ID = os.getenv("IOS_PRO_MONTHLY_PRODUCT_ID", "scoutwise_pro_monthly_ios")
IOS_PRO_YEARLY_PRODUCT_ID  = os.getenv("IOS_PRO_YEARLY_PRODUCT_ID", "scoutwise_pro_yearly_ios")
ANDROID_PRO_MONTHLY_PRODUCT_ID = os.getenv("ANDROID_PRO_MONTHLY_PRODUCT_ID", "scoutwise_pro_monthly_android")
ANDROID_PRO_YEARLY_PRODUCT_ID  = os.getenv("ANDROID_PRO_YEARLY_PRODUCT_ID", "scoutwise_pro_yearly_android")
IOS_NO_ADS_MONTHLY_PRODUCT_ID = os.getenv("IOS_NO_ADS_MONTHLY_PRODUCT_ID", "scoutwise_no_ads_monthly_ios")
ANDROID_NO_ADS_MONTHLY_PRODUCT_ID = os.getenv("ANDROID_NO_ADS_MONTHLY_PRODUCT_ID", "scoutwise_no_ads_monthly_android")


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


@app.on_event("startup")
def ensure_favorite_players_columns() -> None:
    db = SessionLocal()
    try:
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS tutorial_completed BOOLEAN NOT NULL DEFAULT FALSE"))
        db.execute(text("ALTER TABLE favorite_players ADD COLUMN IF NOT EXISTS league TEXT"))
        db.execute(text("ALTER TABLE favorite_players ADD COLUMN IF NOT EXISTS form INTEGER CHECK (form BETWEEN 0 AND 100)"))
        ensure_player_position_label_cache(db)
        db.commit()
    finally:
        db.close()


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

    # ---- restore entitlement (Option A) ----
    row_ent = db.execute(text("""
        SELECT platform, external_id, product_id, expires_at, auto_renew
        FROM subscription_entitlements
        WHERE lower(last_seen_email) = lower(:email)
          AND expires_at IS NOT NULL
        ORDER BY expires_at DESC
        LIMIT 1
    """), {"email": payload.email}).mappings().first()

    # IMPORTANT: use SELECT NOW() if you want DB time
    now_db = dt.datetime.now(dt.timezone.utc)

    if row_ent and row_ent["expires_at"] and row_ent["expires_at"] > now_db:
        plan = plan_from_product_id(row_ent.get("product_id"))
        db.execute(text("""
            UPDATE users
            SET plan = :plan,
                subscription_end_at = :end_at,
                subscription_auto_renew = :auto_renew,
                subscription_platform = :platform,
                subscription_external_id = :ext_id
            WHERE id = :id
        """), {
            "plan": plan,
            "end_at": row_ent["expires_at"],
            "auto_renew": row_ent["auto_renew"],
            "platform": row_ent["platform"],
            "ext_id": row_ent["external_id"],
            "id": row["id"],
        })
        db.commit()

    # re-fetch user row AFTER possible updates
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

    return {"token": token, "user": user_row_to_dict(row)}


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
    preferred_lang = normalize_lang(accept_language) or "en"

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
    favs_source = patch.favorite_players if patch.favorite_players is not None else row["favorites_json"]
    favs = favs_source if isinstance(favs_source, str) else json.dumps(favs_source or [], ensure_ascii=False)
    language = normalize_lang(patch.uiLanguage) if patch.uiLanguage is not None else normalize_lang(row["language"])

    db.execute(
        text("""
            UPDATE users
            SET dob = CAST(:dob AS date),
                country = :country,
                plan = :plan,
                favorites_json = :favs,
                language = :language
            WHERE id = :id
        """),
        {"dob": dob, "country": country, "plan": plan, "favs": favs, "language": language, "id": user_id}
    )
    if patch.uiLanguage is not None:
        db.execute(
            text("UPDATE sessions SET language = :language WHERE user_id = :id AND ended_at IS NULL"),
            {"language": language, "id": user_id},
        )
    db.commit()

    row2 = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).mappings().first()
    return user_row_to_dict(row2)

@app.patch("/me/consent", response_model=ProfileOut)
def update_consent(
    body: ConsentPatch,
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    db.execute(
        text("""
            UPDATE users
            SET consent = :consent
            WHERE id = :id
        """),
        {
            "consent": body.consent,
            "id": user_id,
        }
    )
    db.commit()

    row = db.execute(
        text("SELECT * FROM users WHERE id = :id"),
        {"id": user_id}
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    return user_row_to_dict(row)

@app.patch("/me/tutorial", response_model=ProfileOut)
def update_tutorial_completion(
    body: TutorialPatch,
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    db.execute(
        text("""
            UPDATE users
            SET tutorial_completed = :tutorial_completed
            WHERE id = :id
        """),
        {
            "tutorial_completed": body.tutorialCompleted,
            "id": user_id,
        },
    )
    db.commit()

    row = db.execute(
        text("SELECT * FROM users WHERE id = :id"),
        {"id": user_id},
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    return user_row_to_dict(row)

@app.post("/logout_all")
def logout_all(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    db.execute(text("DELETE FROM sessions WHERE user_id = :id"), {"id": user_id})
    db.commit()
    return {"ok": True}

# --- email codes: reset ---
@app.post("/auth/request_reset")
def request_reset(body: PasswordResetRequestIn, accept_language: str | None = Header(default=None), db: Session = Depends(get_db)):
    email = body.email
    row = db.execute(text("SELECT id, language FROM users WHERE email = :e"), {"e": email}).mappings().first()
    if row:
        code = create_email_code(email, purpose="reset")
        preferred_lang = normalize_lang(accept_language) or "en"
        send_email_code(email, code, mail_type="reset", lang=preferred_lang)
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
def request_signup_code(body: SignupCodeRequestIn, accept_language: str | None = Header(default=None), db: Session = Depends(get_db)):
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
    send_email_code(email, code, mail_type="signup", lang=accept_language)
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

    # Check if user already exists
    already = db.execute(
        text("SELECT id FROM users WHERE lower(email) = lower(:e)"),
        {"e": email}
    ).mappings().first()

    if not already:
        # Create user as Free by default
        db.execute(text("""
            INSERT INTO users
            (email, password_hash, salt, dob, country, plan, favorites_json, created_at, language, newsletter)
            VALUES
            (:email, :ph, :salt, CAST(:dob AS date), :country, 'Free',
             CAST(:favs AS jsonb), NOW(), NULL, :newsletter)
        """), {
            "email": ps["email"],
            "ph": ps["password_hash"],
            "salt": ps["salt"],
            "dob": ps["dob"],
            "country": ps["country"],
            "favs": ps["favorites_json"],  # already jsonb
            "newsletter": bool(ps["newsletter"]),
        })

    row_ent = db.execute(text("""
        SELECT platform, external_id, product_id, expires_at, auto_renew
        FROM subscription_entitlements
        WHERE lower(last_seen_email) = lower(:email)
          AND expires_at IS NOT NULL
        ORDER BY expires_at DESC
        LIMIT 1
    """), {"email": email}).mappings().first()

    now_utc = dt.datetime.now(dt.timezone.utc)

    if row_ent and row_ent["expires_at"] > now_utc:
        plan = plan_from_product_id(row_ent.get("product_id"))
        db.execute(text("""
            UPDATE users
            SET plan = :plan,
                subscription_platform = :platform,
                subscription_external_id = :ext_id,
                subscription_end_at = :end_at,
                subscription_auto_renew = :auto_renew
            WHERE lower(email) = lower(:email)
        """), {
            "plan": plan,
            "platform": row_ent["platform"],
            "ext_id": row_ent["external_id"],
            "end_at": row_ent["expires_at"],
            "auto_renew": row_ent["auto_renew"],
            "email": email,
        })

    # Cleanup staged signup
    db.execute(
        text("DELETE FROM pending_signups WHERE lower(email) = lower(:e)"),
        {"e": email}
    )

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
def _chat_session_label(session_id: str) -> str:
    if not session_id:
        return "default"
    if len(session_id) <= 10:
        return session_id
    return f"{session_id[:6]}...{session_id[-4:]}"


@app.post("/chat")
async def chat(body: ChatIn,
               user_id: int = Depends(require_auth),
               accept_language: str | None = Header(default=None),
               db: Session = Depends(get_db)) -> Dict[str, Any]:
    started_at = time.perf_counter()
    session_id = body.session_id or "default"
    session_label = _chat_session_label(session_id)
    message_chars = len(body.message or "")
    strategy_chars = len(body.strategy or "")
    print(
        "[mobile_chat] event=request_start "
        f"user_id={user_id} session={session_label} tutorial={body.tutorial_mode} "
        f"accept_language={accept_language or 'none'} message_chars={message_chars} "
        f"strategy_chars={strategy_chars}",
        flush=True,
    )

    if not body.tutorial_mode and not is_user_pro(db, user_id):
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        print(
            "[mobile_chat] event=blocked_non_pro "
            f"user_id={user_id} session={session_label} elapsed_ms={elapsed_ms}",
            flush=True,
        )
        raise HTTPException(status_code=403, detail="ScoutWise Pro required")

    header_lang = normalize_lang(accept_language)
    user_lang = normalize_lang(get_user_language(db, user_id))
    lang = header_lang or user_lang or "en"
    print(
        "[mobile_chat] event=session_prepare_start "
        f"session={session_label} lang={lang} header_lang={header_lang or 'none'} "
        f"user_lang={user_lang or 'none'}",
        flush=True,
    )
    try:
        if not session_exists_and_active(db, session_id):
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
            print(
                "[mobile_chat] event=session_prepare_done "
                f"session={session_label} action=upsert",
                flush=True,
            )
        else:
            db.execute(
                text("UPDATE sessions SET language = :l WHERE token = :t AND ended_at IS NULL"),
                {"l": lang, "t": session_id}
            )
            db.commit()
            print(
                "[mobile_chat] event=session_prepare_done "
                f"session={session_label} action=update_language",
                flush=True,
            )
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        print(
            "[mobile_chat] event=session_prepare_error "
            f"session={session_label} elapsed_ms={elapsed_ms} "
            f"error_type={type(exc).__name__} error={str(exc)[:180]}",
            flush=True,
        )
        raise

    if body.tutorial_mode:
        print(f"[mobile_chat] event=tutorial_response_start session={session_label}", flush=True)
        response = tutorial_chat_response(db)
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        print(
            "[mobile_chat] event=tutorial_response_done "
            f"session={session_label} elapsed_ms={elapsed_ms}",
            flush=True,
        )
        return response

    print(f"[mobile_chat] event=answer_question_start session={session_label}", flush=True)
    try:
        result = answer_question(
            body.message,
            session_id=session_id,
            strategy=body.strategy,
        )
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        print(
            "[mobile_chat] event=answer_question_error "
            f"session={session_label} elapsed_ms={elapsed_ms} "
            f"error_type={type(exc).__name__} error={str(exc)[:180]}",
            flush=True,
        )
        raise

    elapsed_answer_ms = int((time.perf_counter() - started_at) * 1000)
    answer_text = (result.get("answer") or "").strip()
    payload = result.get("data") or {"players": []}
    players = payload.get("players") if isinstance(payload, dict) else []
    player_count = len(players) if isinstance(players, list) else 0
    print(
        "[mobile_chat] event=answer_question_done "
        f"session={session_label} elapsed_ms={elapsed_answer_ms} "
        f"answer_chars={len(answer_text)} players={player_count} "
        f"result_keys={','.join(sorted(result.keys())) if isinstance(result, dict) else 'n/a'}",
        flush=True,
    )

    response_parts = split_response_parts(answer_text)
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    print(
        "[mobile_chat] event=response_ready "
        f"session={session_label} elapsed_ms={elapsed_ms} response_parts={len(response_parts)}",
        flush=True,
    )
    return {
        "response": answer_text,
        "data": payload,
        "response_parts": response_parts,
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


@app.post("/player-pool/search", response_model=List[PlayerPoolSearchRow])
def player_pool_search(
    payload: PlayerPoolSearchIn,
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    if payload.minAge is not None and payload.maxAge is not None and payload.minAge > payload.maxAge:
        raise HTTPException(status_code=400, detail="minAge cannot be greater than maxAge")
    if payload.minHeight is not None and payload.maxHeight is not None and payload.minHeight > payload.maxHeight:
        raise HTTPException(status_code=400, detail="minHeight cannot be greater than maxHeight")
    if payload.minWeight is not None and payload.maxWeight is not None and payload.minWeight > payload.maxWeight:
        raise HTTPException(status_code=400, detail="minWeight cannot be greater than maxWeight")

    filters = payload.model_dump(exclude_none=True)
    rows = search_players(db, filters)
    record_analytics_event(
        user_id=user_id,
        event_type="player_pool_search",
        mode=analytics_mode(payload.worldCupMode),
        source="player_pool_search",
        player_table="player_data_wc" if payload.worldCupMode else "player_data",
        search_filters=filters,
        result_count=len(rows),
    )
    return rows


@app.post("/player-pool/{player_id}/search-hit")
def player_pool_record_search_hit(
    player_id: str,
    worldCupMode: bool = FastAPIQuery(False),
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    try:
        record_player_search(db, player_id, worldCupMode)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid player_id")
    db.commit()
    player = get_player_snapshot(db, player_id, worldCupMode)
    record_analytics_event(
        user_id=user_id,
        event_type="player_pool_search_hit",
        mode=analytics_mode(worldCupMode),
        source="player_pool_search_hit",
        player_table="player_data_wc" if worldCupMode else "player_data",
        **player,
    )
    return {"ok": True}


@app.post("/player-pool/weekly-popular", response_model=List[PlayerPoolSearchRow])
def player_pool_weekly_popular(
    payload: PlayerPoolWeeklyPopularIn = Body(default=PlayerPoolWeeklyPopularIn()),
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    world_cup_mode = bool(payload.worldCupMode)
    record_weekly_popular_reveal(db, user_id, world_cup_mode)
    rows = get_weekly_popular_players(db, payload.limit or 10, world_cup_mode)
    db.commit()
    record_analytics_event(
        user_id=user_id,
        event_type="weekly_popular_reveal",
        mode=analytics_mode(world_cup_mode),
        source="player_pool_weekly_popular",
        player_table="player_data_wc" if world_cup_mode else "player_data",
        result_count=len(rows),
        metadata={"limit": payload.limit or 10},
    )
    return rows


@app.post("/player-pool/matchup/comparison", response_model=MatchupComparisonOut)
def player_pool_matchup_comparison(
    payload: MatchupComparisonIn,
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    try:
        world_cup_mode = bool(payload.worldCupMode)
        result = get_matchup_comparison(db, payload.player1Id, payload.player2Id, world_cup_mode)
        player1 = get_player_snapshot(db, payload.player1Id, world_cup_mode)
        player2 = get_player_snapshot(db, payload.player2Id, world_cup_mode)
        record_analytics_event(
            user_id=user_id,
            event_type="matchup_comparison",
            section="matchup_center",
            mode=analytics_mode(world_cup_mode),
            source="player_pool_matchup_comparison",
            player_table="player_data_wc" if world_cup_mode else "player_data",
            **player1,
            secondary_player_id=player2.get("player_id"),
            secondary_player_name=player2.get("player_name"),
            secondary_player_team=player2.get("player_team"),
            secondary_player_league=player2.get("player_league"),
            secondary_player_nationality=player2.get("player_nationality"),
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/player-pool/options", response_model=PlayerPoolFilterOptionsOut)
def player_pool_options(
    worldCupMode: bool = FastAPIQuery(False),
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return get_player_pool_filter_options(db, worldCupMode)


@app.post("/player-pool/{player_id}/potential", response_model=PlayerPoolPotentialOut)
def player_pool_reveal_potential(
    player_id: str,
    worldCupMode: bool = FastAPIQuery(False),
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    try:
        result = reveal_player_potential(db, player_id, worldCupMode)
        player = get_player_snapshot(db, player_id, worldCupMode)
        record_analytics_event(
            user_id=user_id,
            event_type="score_reveal",
            mode=analytics_mode(worldCupMode),
            source="player_pool_reveal_potential",
            player_table="player_data_wc" if worldCupMode else "player_data",
            score_kind="potential",
            score_value=result.get("potential"),
            score_source=result.get("source"),
            **player,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/player-pool/{player_id}/form", response_model=PlayerPoolFormOut)
def player_pool_reveal_form(
    player_id: str,
    worldCupMode: bool = FastAPIQuery(False),
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    try:
        result = reveal_player_form(db, player_id, worldCupMode)
        player = get_player_snapshot(db, player_id, worldCupMode)
        record_analytics_event(
            user_id=user_id,
            event_type="score_reveal",
            mode=analytics_mode(worldCupMode),
            source="player_pool_reveal_form",
            player_table="player_data_wc" if worldCupMode else "player_data",
            score_kind="form",
            score_value=result.get("form"),
            score_source=result.get("source"),
            **player,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))



@app.get("/daily-scout-challenge", response_model=DailyScoutChallengeOut)
def daily_scout_challenge_status(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        return get_daily_status(db, user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/daily-scout-challenge/skip", response_model=DailyScoutChallengeOut)
def daily_scout_challenge_skip(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    return skip_daily_challenge(db, user_id)


@app.post("/daily-scout-challenge/answer", response_model=DailyScoutChallengeOut)
def daily_scout_challenge_answer(
    payload: DailyScoutAnswerIn,
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    try:
        return submit_daily_answer(db, user_id, payload.challengeId, payload.chosenPlayerId)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/daily-scout-challenge/nickname", response_model=DailyScoutNicknameOut)
def daily_scout_challenge_nickname(
    payload: DailyScoutNicknameIn,
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    try:
        return set_weekly_nickname(db, user_id, payload.nickname)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/daily-scout-challenge/leaderboard", response_model=DailyScoutLeaderboardOut)
def daily_scout_challenge_leaderboard(
    limit: int = FastAPIQuery(20, ge=1, le=50),
    user_id: int = Depends(require_auth),
    db: Session = Depends(get_db),
):
    _ = user_id
    return get_weekly_leaderboard(db, limit)


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
               form,
               gender,
               height,
               weight,
               team,
               league,
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
            form=r["form"],
            gender=r["gender"],
            height=r["height"],
            weight=r["weight"],
            team=r["team"],
            league=r["league"],
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
    favorite_values: Dict[str, Any] = {
        "name": payload.name,
        "nationality": payload.nationality,
        "age": payload.age,
        "potential": payload.potential,
        "form": payload.form,
        "gender": payload.gender,
        "height": payload.height,
        "weight": payload.weight,
        "team": payload.team,
        "league": payload.league,
        "roles": roles_long,
    }

    player_row_by_id = None
    if payload.playerId:
        player_row_by_id = db.execute(
            text("""
            SELECT id, metadata
            FROM player_data
            WHERE id = :id
            LIMIT 1
            """),
            {"id": payload.playerId},
        ).mappings().first()

    def player_row_matches_payload(row: Any) -> bool:
        if not row or not row.get("metadata"):
            return False
        metadata = row["metadata"] or {}
        row_name = metadata.get("player_name") or metadata.get("name")
        if not row_name or str(row_name).strip().lower() != payload.name.strip().lower():
            return False
        payload_nat = (payload.nationality or "").strip().lower()
        row_nat = str(metadata.get("nationality_name") or metadata.get("nationality") or "").strip().lower()
        return not payload_nat or not row_nat or payload_nat == row_nat

    if payload.formRevealed and not payload.worldCupMode:
        player_row = db.execute(
            text("""
            SELECT id, metadata
            FROM player_data
            WHERE lower(COALESCE(metadata->>'player_name', '')) = lower(:name)
              AND (:gender IS NULL OR lower(COALESCE(metadata->>'gender', '')) = lower(:gender))
              AND (:age IS NULL OR (
                    COALESCE(metadata->>'age', '') ~ '^-?[0-9]+(\.[0-9]+)?$'
                    AND (metadata->>'age')::numeric = :age
                  ))
              AND (:height IS NULL OR (
                    COALESCE(metadata->>'height', '') ~ '^-?[0-9]+(\.[0-9]+)?$'
                    AND (metadata->>'height')::numeric = :height
                  ))
              AND (:weight IS NULL OR (
                    COALESCE(metadata->>'weight', '') ~ '^-?[0-9]+(\.[0-9]+)?$'
                    AND (metadata->>'weight')::numeric = :weight
                  ))
            ORDER BY id DESC
            LIMIT 1
            """),
            {
                "name": payload.name,
                "gender": payload.gender,
                "age": payload.age,
                "height": payload.height,
                "weight": payload.weight,
            },
        ).mappings().first()
        if not player_row and player_row_matches_payload(player_row_by_id):
            player_row = player_row_by_id

        if player_row and player_row.get("metadata"):
            player_meta = player_row["metadata"] or {}
            player_roles = player_meta.get("roles") or player_meta.get("positions") or []
            if not player_roles and player_meta.get("position_name"):
                player_roles = [player_meta.get("position_name")]
            if not isinstance(player_roles, list):
                player_roles = [player_roles] if player_roles else []
            form_result = reveal_player_form(db, player_row["id"], False)
            potential_result = reveal_player_potential(db, player_row["id"], False)
            player_snapshot = get_player_snapshot(db, player_row["id"], False)
            record_analytics_event(
                user_id=user_id,
                event_type="score_reveal",
                source="favorite_add_auto_reveal",
                player_table="player_data",
                score_kind="form",
                score_value=form_result.get("form"),
                score_source=form_result.get("source"),
                metadata={"favorite_action": "add_or_update"},
                **player_snapshot,
            )
            record_analytics_event(
                user_id=user_id,
                event_type="score_reveal",
                source="favorite_add_auto_reveal",
                player_table="player_data",
                score_kind="potential",
                score_value=potential_result.get("potential"),
                score_source=potential_result.get("source"),
                metadata={"favorite_action": "add_or_update"},
                **player_snapshot,
            )
            favorite_values.update({
                "name": player_meta.get("player_name") or payload.name,
                "nationality": player_meta.get("nationality_name") or player_meta.get("nationality") or payload.nationality,
                "age": player_meta.get("age") or payload.age,
                "gender": player_meta.get("gender") or payload.gender,
                "height": player_meta.get("height") or payload.height,
                "weight": player_meta.get("weight") or payload.weight,
                "team": player_meta.get("team_name") or player_meta.get("team") or payload.team,
                "league": player_meta.get("league_name") or player_meta.get("league") or payload.league,
                "roles": to_long_roles(player_roles),
                "form": form_result.get("form"),
                "potential": potential_result.get("potential"),
            })

    if payload.worldCupMode:
        club_row = db.execute(
            text("""
            SELECT id, metadata
            FROM player_data
            WHERE lower(COALESCE(metadata->>'player_name', '')) = lower(:name)
              AND (:gender IS NULL OR lower(COALESCE(metadata->>'gender', '')) = lower(:gender))
              AND (:age IS NULL OR (
                    COALESCE(metadata->>'age', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                    AND (metadata->>'age')::numeric = :age
                  ))
              AND (:height IS NULL OR (
                    COALESCE(metadata->>'height', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                    AND (metadata->>'height')::numeric = :height
                  ))
              AND (:weight IS NULL OR (
                    COALESCE(metadata->>'weight', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                    AND (metadata->>'weight')::numeric = :weight
                  ))
            ORDER BY id DESC
            LIMIT 1
            """),
            {
                "name": payload.name,
                "gender": payload.gender,
                "age": payload.age,
                "height": payload.height,
                "weight": payload.weight,
            },
        ).mappings().first()
        if not club_row and player_row_matches_payload(player_row_by_id):
            club_row = player_row_by_id

        if club_row and club_row.get("metadata"):
            club_meta = club_row["metadata"] or {}
            club_roles = club_meta.get("roles") or club_meta.get("positions") or []
            if not club_roles and club_meta.get("position_name"):
                club_roles = [club_meta.get("position_name")]
            if not isinstance(club_roles, list):
                club_roles = [club_roles] if club_roles else []
            favorite_values.update({
                "name": club_meta.get("player_name") or payload.name,
                "nationality": club_meta.get("nationality_name") or club_meta.get("nationality"),
                "age": club_meta.get("age"),
                "gender": club_meta.get("gender"),
                "height": club_meta.get("height"),
                "weight": club_meta.get("weight"),
                "team": club_meta.get("team_name") or club_meta.get("team"),
                "league": club_meta.get("league_name") or club_meta.get("league"),
                "roles": to_long_roles(club_roles),
                "potential": None,
                "form": None,
            })
            if payload.formRevealed:
                form_result = reveal_player_form(db, club_row["id"], False)
                potential_result = reveal_player_potential(db, club_row["id"], False)
                player_snapshot = get_player_snapshot(db, club_row["id"], False)
                record_analytics_event(
                    user_id=user_id,
                    event_type="score_reveal",
                    mode=analytics_mode(True),
                    source="favorite_add_auto_reveal",
                    player_table="player_data",
                    score_kind="form",
                    score_value=form_result.get("form"),
                    score_source=form_result.get("source"),
                    metadata={"favorite_action": "add_or_update"},
                    **player_snapshot,
                )
                record_analytics_event(
                    user_id=user_id,
                    event_type="score_reveal",
                    mode=analytics_mode(True),
                    source="favorite_add_auto_reveal",
                    player_table="player_data",
                    score_kind="potential",
                    score_value=potential_result.get("potential"),
                    score_source=potential_result.get("source"),
                    metadata={"favorite_action": "add_or_update"},
                    **player_snapshot,
                )
                favorite_values["form"] = form_result.get("form")
                favorite_values["potential"] = potential_result.get("potential")

    existing = db.execute(
        text("""
        SELECT id, name, nationality, age, potential, form, gender, height, weight, team, league, roles_json
        FROM favorite_players
        WHERE user_id = :uid
          AND lower(name) = lower(:name)
          AND lower(COALESCE(nationality, '')) = lower(COALESCE(:nat, ''))
        LIMIT 1
        """),
        {
            "uid": user_id,
            "name": favorite_values["name"],
            "nat": favorite_values["nationality"],
        }
    ).mappings().first()

    if existing:
        db.execute(
            text("""
            UPDATE favorite_players
            SET
                name = :name,
                nationality = :nat,
                age = :age,
                potential = :pot,
                form = :form,
                gender = :gender,
                height = :height,
                weight = :weight,
                team = :team,
                league = :league,
                roles_json = :roles
            WHERE id = :id
              AND user_id = :uid
            """),
            {
                "id": existing["id"],
                "uid": user_id,
                "name": favorite_values["name"],
                "nat": favorite_values["nationality"],
                "age": favorite_values["age"],
                "pot": favorite_values["potential"],
                "form": favorite_values["form"],
                "gender": favorite_values["gender"],
                "height": favorite_values["height"],
                "weight": favorite_values["weight"],
                "team": favorite_values["team"],
                "league": favorite_values["league"],
                "roles": json.dumps(favorite_values["roles"], ensure_ascii=False),
            },
        )
        db.commit()

        if response is not None:
            response.status_code = status.HTTP_200_OK
        return FavoritePlayerOut(
            id=existing["id"],
            name=favorite_values["name"],
            nationality=favorite_values["nationality"],
            age=favorite_values["age"],
            potential=favorite_values["potential"],
            form=favorite_values["form"],
            gender=favorite_values["gender"],
            height=favorite_values["height"],
            weight=favorite_values["weight"],
            team=favorite_values["team"],
            league=favorite_values["league"],
            roles=favorite_values["roles"],
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
            form,
            gender,
            height,
            weight,
            team,
            league,
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
            :form,
            :gender,
            :height,
            :weight,
            :team,
            :league,
            :roles,
            :ts
        )
        """),
        {
            "id": fav_id,
            "uid": user_id,
            "name": favorite_values["name"],
            "nat": favorite_values["nationality"],
            "age": favorite_values["age"],
            "pot": favorite_values["potential"],
            "form": favorite_values["form"],
            "gender": favorite_values["gender"],
            "height": favorite_values["height"],
            "weight": favorite_values["weight"],
            "team": favorite_values["team"],
            "league": favorite_values["league"],
            "roles": json.dumps(favorite_values["roles"], ensure_ascii=False),
            "ts": created_at,
        }
    )
    db.commit()

    return FavoritePlayerOut(
        id=fav_id,
        name=favorite_values["name"],
        nationality=favorite_values["nationality"],
        age=favorite_values["age"],
        potential=favorite_values["potential"],
        form=favorite_values["form"],
        gender=favorite_values["gender"],
        height=favorite_values["height"],
        weight=favorite_values["weight"],
        team=favorite_values["team"],
        league=favorite_values["league"],
        roles=favorite_values["roles"],
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
    #print("[USER ID]", user_id)
    #print("[BODY]:", body)
    allowed_product_ids = {
        IOS_PRO_MONTHLY_PRODUCT_ID,
        IOS_PRO_YEARLY_PRODUCT_ID,
        ANDROID_PRO_MONTHLY_PRODUCT_ID,
        ANDROID_PRO_YEARLY_PRODUCT_ID,
        IOS_NO_ADS_MONTHLY_PRODUCT_ID,
        ANDROID_NO_ADS_MONTHLY_PRODUCT_ID,
    }
    if body.product_id not in allowed_product_ids:
        raise HTTPException(status_code=400, detail="Unknown product")

    # Verify against store
    if body.platform == "ios":
        #print("[VERIFYING IOS SUBSCRIPTION]", {"product_id": body.product_id, "external_id": body.external_id})
        ok, expires_at, auto_renew = verify_ios_subscription(
            body.product_id,
            body.external_id,  # original_transaction_id
        )
        #print("[IOS VERIFY]", {"ok": ok, "expires_at": expires_at, "auto_renew": auto_renew})
    else:
        ok, expires_at, auto_renew = verify_android_subscription(
            body.product_id,
            body.external_id,  # purchaseToken
            body.receipt,
        )

    if not ok:
        #print("[SUBSCRIPTION VERIFICATION FAILED]", {"platform": body.platform, "product_id": body.product_id, "external_id": body.external_id})
        raise HTTPException(status_code=400, detail="Could not verify purchase")

    # ---- SILENT BLOCK: never allow this (platform, external_id) to link to a different user ----
    ent = db.execute(text("""
        SELECT last_seen_user_id
        FROM subscription_entitlements
        WHERE platform = :platform AND external_id = :ext_id
        LIMIT 1
    """), {"platform": body.platform, "ext_id": body.external_id}).mappings().first()

    if ent:
        other_uid = ent.get("last_seen_user_id")
        if other_uid and int(other_uid) != int(user_id):
            # do nothing, return current user's current plan silently
            me = db.execute(text("""
                SELECT plan, subscription_end_at
                FROM users
                WHERE id = :id
            """), {"id": user_id}).mappings().first()

            end_at = (me or {}).get("subscription_end_at")
            return {
                "ok": True,
                "plan": (me or {}).get("plan") or "Free",
                "subscriptionEndAt": end_at.isoformat() if end_at else None,
            }


    plan = plan_from_product_id(body.product_id)
    # Get user email for entitlement linking (best effort)
    email = get_user_email_by_id(db, user_id)
    #print("ACTIVATING SUBSCRIPTION")
    # Single transaction: update users + upsert entitlement
    db.execute(
        text("""
            UPDATE users
            SET plan = :plan,
                subscription_platform = :platform,
                subscription_external_id = :ext_id,
                subscription_end_at = :end_at,
                subscription_auto_renew = :auto_renew,
                subscription_last_checked_at = :checked_at,
                subscription_receipt = :receipt
            WHERE id = :id
        """),
        {   
            "plan": plan,
            "platform": body.platform,
            "ext_id": body.external_id,
            "end_at": expires_at.isoformat(),
            "auto_renew": bool(auto_renew),
            "checked_at": now_iso(),
            "receipt": body.receipt,
            "id": user_id,
        },
    )
    db.execute(
        text("""
            INSERT INTO subscription_entitlements (
                platform, external_id, product_id,
                is_active, expires_at, auto_renew,
                last_verified_at, last_seen_user_id, last_seen_email, updated_at
            )
            VALUES (
                :platform, :ext_id, :product_id,
                TRUE, :expires_at, :auto_renew,
                NOW(), :uid, :email, NOW()
            )
            ON CONFLICT (platform, external_id) DO UPDATE
            SET product_id = EXCLUDED.product_id,
                is_active = TRUE,
                expires_at = EXCLUDED.expires_at,
                auto_renew = EXCLUDED.auto_renew,
                last_verified_at = NOW(),
                last_seen_user_id = EXCLUDED.last_seen_user_id,
                last_seen_email = EXCLUDED.last_seen_email,
                updated_at = NOW()
        """),
        {
            "platform": body.platform,
            "ext_id": body.external_id,
            "product_id": body.product_id,
            "expires_at": expires_at.isoformat(),
            "auto_renew": bool(auto_renew),
            "uid": user_id,
            "email": email,  # can be None; ok
        },
    )
    db.commit()
    #print("SUBSCRIPTION ACTIVATED")
    return {
        "ok": True,
        "plan": plan,
        "subscriptionEndAt": expires_at.isoformat(),
    }

def _generate_report_background(
    report_id: str,
    favorite_id: str,
    user_id: int,
    lang: str,
    version: int,
    player_payload: dict,
) -> None:
    db = SessionLocal()
    try:
        generated = generate_report_content(
            db,
            favorite_id=favorite_id,
            lang=lang,
            version=version,
            player_identity=player_payload,
        )

        db.execute(
            text("""
                UPDATE scouting_reports
                SET status = 'ready',
                    content = :content,
                    content_json = CAST(:content_json AS jsonb),
                    error = NULL,
                    ready_at = NOW(),
                    updated_at = NOW()
                WHERE id = :id
                  AND user_id = :uid
                  AND favorite_player_id = :fid
            """),
            {
                "id": report_id,
                "uid": user_id,
                "fid": favorite_id,
                "content": generated["content"],
                "content_json": json.dumps(
                    generated["content_json"],
                    ensure_ascii=False,
                    default=str,
                ),
            },
        )
        db.commit()
        favorite_snapshot = get_favorite_player_snapshot(db, favorite_id, user_id)
        record_analytics_event(
            user_id=user_id,
            event_type="scouting_report_ready",
            section="reports",
            source="scouting_report_generation",
            report_id=report_id,
            metadata={"language": lang, "version": version},
            **favorite_snapshot,
        )

    except Exception as e:
        print(f"[report_generation_failed] report_id={report_id} favorite_id={favorite_id} error={e}")
        db.execute(
            text("""
                UPDATE scouting_reports
                SET status = 'failed',
                    error = :err,
                    updated_at = NOW()
                WHERE id = :id
                  AND user_id = :uid
                  AND favorite_player_id = :fid
            """),
            {
                "id": report_id,
                "uid": user_id,
                "fid": favorite_id,
                "err": str(e),
            },
        )
        db.commit()
        favorite_snapshot = get_favorite_player_snapshot(db, favorite_id, user_id)
        record_analytics_event(
            user_id=user_id,
            event_type="scouting_report_failed",
            section="reports",
            source="scouting_report_generation",
            report_id=report_id,
            metadata={"language": lang, "version": version, "error": str(e)},
            **favorite_snapshot,
        )
    finally:
        db.close()


def _metadata_text(metadata: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _metadata_number(metadata: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = metadata.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _metadata_roles(metadata: Dict[str, Any]) -> List[str]:
    roles = metadata.get("roles") or metadata.get("positions") or metadata.get("position_names_seen") or []
    if not roles and metadata.get("position_name"):
        roles = [metadata.get("position_name")]
    if isinstance(roles, str):
        roles = [roles]
    if not isinstance(roles, list):
        return []
    return [str(role).strip() for role in roles if str(role).strip()]


def _get_player_pool_metadata(db: Session, player_id: str, world_cup_mode: bool) -> Dict[str, Any]:
    table_name = "player_data_wc" if world_cup_mode else "player_data"
    row = db.execute(
        text(f"""
        SELECT metadata
        FROM {table_name}
        WHERE id = :player_id
        LIMIT 1
        """),
        {"player_id": player_id},
    ).mappings().first()
    if not row or not row.get("metadata"):
        raise HTTPException(status_code=404, detail="Player not found")
    return dict(row["metadata"] or {})


def _resolve_player_pool_report_club_row(db: Session, player_payload: Dict[str, Any]) -> Optional[Any]:
    club_player_id = player_payload.get("club_player_id") or player_payload.get("clubPlayerId")
    if club_player_id is not None:
        row = db.execute(
            text("""
            SELECT id, metadata
            FROM player_data
            WHERE id = :player_id
            LIMIT 1
            """),
            {"player_id": club_player_id},
        ).mappings().first()
        return row

    player_id = player_payload.get("playerId") or player_payload.get("player_id")
    world_cup_mode = bool(player_payload.get("worldCupMode") or player_payload.get("world_cup_mode"))

    if player_id and not world_cup_mode:
        row = db.execute(
            text("""
            SELECT id, metadata
            FROM player_data
            WHERE id = :player_id
            LIMIT 1
            """),
            {"player_id": player_id},
        ).mappings().first()
        return row

    source_metadata: Dict[str, Any] = {}
    if player_id and world_cup_mode:
        source_metadata = _get_player_pool_metadata(db, str(player_id), True)

    player_name = _metadata_text(source_metadata, "player_name", "name") or str(player_payload.get("name") or "").strip()
    if not player_name:
        return None

    gender = _metadata_text(source_metadata, "gender") or player_payload.get("gender")
    age = _metadata_number(source_metadata, "age")
    if age is None:
        age = _metadata_number(player_payload, "age")
    height = _metadata_text(source_metadata, "height") or (str(player_payload.get("height")) if player_payload.get("height") is not None else None)
    weight = _metadata_text(source_metadata, "weight") or (str(player_payload.get("weight")) if player_payload.get("weight") is not None else None)

    row = db.execute(
        text("""
        SELECT id, metadata
        FROM player_data
        WHERE LOWER(TRIM(metadata->>'player_name')) = LOWER(TRIM(:player_name))
          AND (:gender IS NULL OR LOWER(COALESCE(metadata->>'gender', '')) = LOWER(:gender))
          AND (:age IS NULL OR (
                COALESCE(metadata->>'age', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                AND (metadata->>'age')::numeric = :age
              ))
        ORDER BY
          CASE WHEN :height IS NOT NULL AND COALESCE(metadata->>'height', '') = :height THEN 0 ELSE 1 END,
          CASE WHEN :weight IS NOT NULL AND COALESCE(metadata->>'weight', '') = :weight THEN 0 ELSE 1 END,
          id DESC
        LIMIT 1
        """),
        {
            "player_name": player_name,
            "gender": gender,
            "age": age,
            "height": height,
            "weight": weight,
        },
    ).mappings().first()
    return row


def _apply_club_row_to_report_payload(
    db: Session,
    player_payload: Dict[str, Any],
    club_row: Any,
) -> Dict[str, Any]:
    metadata = dict(club_row["metadata"] or {})
    next_payload = dict(player_payload)
    next_payload["club_player_id"] = int(club_row["id"])
    next_payload["playerId"] = str(club_row["id"])
    next_payload["worldCupMode"] = False
    next_payload["name"] = _metadata_text(metadata, "player_name", "name") or next_payload.get("name")
    next_payload["nationality"] = _metadata_text(metadata, "nationality_name", "nationality") or next_payload.get("nationality")
    next_payload["gender"] = _metadata_text(metadata, "gender") or next_payload.get("gender")
    next_payload["team"] = _metadata_text(metadata, "team_name", "team", "club") or next_payload.get("team")
    next_payload["league"] = _metadata_text(metadata, "league_name", "league") or next_payload.get("league")

    for key in ("age", "height", "weight"):
        numeric = _metadata_number(metadata, key)
        if numeric is not None:
            next_payload[key] = int(numeric) if float(numeric).is_integer() else numeric

    roles = _metadata_roles(metadata)
    if roles:
        next_payload["roles"] = to_long_roles(roles)

    if player_payload.get("worldCupMode"):
        try:
            next_payload["potential"] = reveal_player_potential(db, club_row["id"], False).get("potential")
            next_payload["form"] = reveal_player_form(db, club_row["id"], False).get("form")
        except Exception as exc:
            print(f"[player_pool_report] club_score_resolve_failed club_player_id={club_row['id']} error={exc}", flush=True)

    return next_payload

def _player_pool_report_cache_key(player_payload: dict) -> str:
    cache_identity = {
        key: player_payload.get(key)
        for key in (
            "name",
            "gender",
            "nationality",
            "team",
            "league",
            "age",
            "height",
            "weight",
        )
        if player_payload.get(key) is not None
    }
    raw = json.dumps(cache_identity, ensure_ascii=False, sort_keys=True, default=str).lower()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"scoutwise:player-pool-report:{raw}"))


@app.post("/player-pool/report", response_model=ScoutingReportOut)
def create_player_pool_report(
    payload: ScoutingReportIn,
    user_id: int = Depends(require_auth),
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    lang = normalize_lang(accept_language) or normalize_lang(get_user_language(db, user_id)) or "en"
    version = 2
    player_payload = payload.model_dump(exclude_none=True)
    player_payload.pop("tutorial_mode", None)

    club_row = _resolve_player_pool_report_club_row(db, player_payload)
    if club_row is not None:
        player_payload = _apply_club_row_to_report_payload(db, player_payload, club_row)
    elif player_payload.get("worldCupMode"):
        raise HTTPException(status_code=404, detail="Matching club player not found")

    name = str(player_payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Player name is required")

    cache_key = _player_pool_report_cache_key(player_payload)

    row = db.execute(text("""
            SELECT id, status, content, content_json, language, version, player_payload
            FROM player_pool_scouting_reports
            WHERE user_id = :uid
              AND cache_key = :cache_key
              AND COALESCE(language, 'en') = :lang
              AND version = :ver
            LIMIT 1
    """), {"uid": user_id, "cache_key": cache_key, "lang": lang, "ver": version}).mappings().first()

    if row:
        if row["status"] == "failed":
            db.execute(text("DELETE FROM player_pool_scouting_reports WHERE id = :id"), {"id": row["id"]})
            db.commit()
            row = None
        elif row["status"] == "ready":
            content_json = row["content_json"] if isinstance(row["content_json"], dict) else {}
            player_card = content_json.get("player_card") if isinstance(content_json, dict) else {}
            player_card = player_card if isinstance(player_card, dict) else {}
            missing_requested_score = any(
                player_payload.get(score_key) is not None and player_card.get(score_key) is None
                for score_key in ("potential", "form")
            )
            if missing_requested_score:
                db.execute(text("DELETE FROM player_pool_scouting_reports WHERE id = :id"), {"id": row["id"]})
                db.commit()
                row = None
            else:
                normalized_content = normalize_mobile_report_format(row["content"] or "", row["language"] or lang)
                normalized_content_json = dict(content_json)
                normalized_content_json["report_text"] = normalized_content
                record_analytics_event(
                    user_id=user_id,
                    event_type="scouting_report_direct_cached",
                    section="player_pool",
                    source="player_pool_report_request",
                    report_id=str(row["id"]),
                    metadata={"language": row["language"], "version": row["version"], "player_name": name, "cache_key": cache_key},
                )
                return {
                    "favorite_player_id": cache_key,
                    "status": row["status"],
                    "content": normalized_content,
                    "content_json": normalized_content_json,
                    "language": row["language"],
                    "version": row["version"],
                    "player": payload,
                }
        else:
            record_analytics_event(
                user_id=user_id,
                event_type="scouting_report_direct_pending",
                section="player_pool",
                source="player_pool_report_request",
                report_id=str(row["id"]),
                metadata={"language": row["language"], "version": row["version"], "player_name": name, "cache_key": cache_key, "status": row["status"]},
            )
            return {
                "favorite_player_id": cache_key,
                "status": row["status"],
                "content": row["content"],
                "content_json": row["content_json"],
                "language": row["language"],
                "version": row["version"],
                "player": payload,
            }

    report_id = str(uuid.uuid4())
    try:
        generated = generate_report_content(
            db,
            favorite_id=cache_key,
            lang=lang,
            version=version,
            player_identity=player_payload,
        )

        db.execute(text("""
            INSERT INTO player_pool_scouting_reports (
                id, user_id, cache_key, status, language, version,
                player_name, player_payload, content, content_json,
                ready_at, created_at, updated_at
            )
            VALUES (
                :id, :uid, :cache_key, 'ready', :lang, :ver,
                :player_name, CAST(:player_payload AS jsonb), :content, CAST(:content_json AS jsonb),
                NOW(), NOW(), NOW()
            )
            ON CONFLICT (user_id, cache_key, language, version)
            DO UPDATE SET
                status = 'ready',
                player_name = EXCLUDED.player_name,
                player_payload = EXCLUDED.player_payload,
                content = EXCLUDED.content,
                content_json = EXCLUDED.content_json,
                error = NULL,
                ready_at = NOW(),
                updated_at = NOW()
        """), {
            "id": report_id,
            "uid": user_id,
            "cache_key": cache_key,
            "lang": lang,
            "ver": version,
            "player_name": name,
            "player_payload": json.dumps(player_payload, ensure_ascii=False, default=str),
            "content": generated["content"],
            "content_json": json.dumps(generated["content_json"], ensure_ascii=False, default=str),
        })
        db.commit()

        record_analytics_event(
            user_id=user_id,
            event_type="scouting_report_direct_ready",
            section="player_pool",
            source="player_pool_report_request",
            report_id=report_id,
            metadata={"language": lang, "version": version, "player_name": name, "cache_key": cache_key},
        )
        return {
            "favorite_player_id": cache_key,
            "status": "ready",
            "content": generated["content"],
            "content_json": generated["content_json"],
            "language": lang,
            "version": version,
            "player": payload,
        }
    except Exception as exc:
        db.rollback()
        print(f"[player_pool_report_failed] user_id={user_id} player={name} error={exc}")
        try:
            db.execute(text("""
                INSERT INTO player_pool_scouting_reports (
                    id, user_id, cache_key, status, language, version,
                    player_name, player_payload, error, created_at, updated_at
                )
                VALUES (
                    :id, :uid, :cache_key, 'failed', :lang, :ver,
                    :player_name, CAST(:player_payload AS jsonb), :error, NOW(), NOW()
                )
                ON CONFLICT (user_id, cache_key, language, version)
                DO UPDATE SET
                    status = 'failed',
                    player_name = EXCLUDED.player_name,
                    player_payload = EXCLUDED.player_payload,
                    error = EXCLUDED.error,
                    updated_at = NOW()
            """), {
                "id": report_id,
                "uid": user_id,
                "cache_key": cache_key,
                "lang": lang,
                "ver": version,
                "player_name": name,
                "player_payload": json.dumps(player_payload, ensure_ascii=False, default=str),
                "error": str(exc),
            })
            db.commit()
        except Exception:
            db.rollback()
        record_analytics_event(
            user_id=user_id,
            event_type="scouting_report_direct_failed",
            section="player_pool",
            source="player_pool_report_request",
            report_id=report_id,
            metadata={"language": lang, "version": version, "player_name": name, "cache_key": cache_key, "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="Report generation failed") from exc


@app.post("/me/favorites/{favorite_id}/report", response_model=ScoutingReportOut)
def get_or_create_report(
    favorite_id: str,
    background_tasks: BackgroundTasks,
    payload: ScoutingReportIn = Body(default=ScoutingReportIn()),
    user_id: int = Depends(require_auth),
    accept_language: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    lang = normalize_lang(accept_language) or normalize_lang(get_user_language(db, user_id)) or "en"
    version = 2
    player_payload = payload.model_dump(exclude_none=True)
    tutorial_mode = bool(player_payload.pop("tutorial_mode", False))

    # Ensure favorite belongs to user
    owned = db.execute(
        text("SELECT 1 FROM favorite_players WHERE id = :fid AND user_id = :uid"),
        {"fid": favorite_id, "uid": user_id},
    ).first()
    if not owned:
        raise HTTPException(status_code=404, detail="Favorite not found")
    favorite_snapshot = get_favorite_player_snapshot(db, favorite_id, user_id)

    if tutorial_mode:
        name = str(player_payload.get("name") or "").strip().lower()
        if "lamine" in name and "yamal" in name:
            return tutorial_yamal_scouting_report(
                db,
                favorite_id=favorite_id,
                lang=lang,
                player_identity=player_payload,
            )

    # Check cache
    row = db.execute(text("""
            SELECT id, status, content, content_json, language, version
            FROM scouting_reports
            WHERE user_id = :uid
            AND favorite_player_id = :fid
            AND COALESCE(language, 'en') = :lang
            AND version = :ver
            LIMIT 1
    """), {"uid": user_id, "fid": favorite_id, "lang": lang, "ver": version}).mappings().first()

    if row:
        if row["status"] == "failed":
            db.execute(
                text("DELETE FROM scouting_reports WHERE id = :id"),
                {"id": row["id"]},
            )
            db.commit()
            row = None  # continue into regeneration flow
        elif row["status"] == "ready":
            content_json = row["content_json"] if isinstance(row["content_json"], dict) else {}
            player_card = content_json.get("player_card") if isinstance(content_json, dict) else {}
            player_card = player_card if isinstance(player_card, dict) else {}
            missing_requested_score = any(
                player_payload.get(score_key) is not None and player_card.get(score_key) is None
                for score_key in ("potential", "form")
            )
            if missing_requested_score:
                db.execute(
                    text("DELETE FROM scouting_reports WHERE id = :id"),
                    {"id": row["id"]},
                )
                db.commit()
                row = None  # regenerate with the newly available score fields
            else:
                normalized_content = normalize_mobile_report_format(row["content"] or "", row["language"] or lang)
                normalized_content_json = dict(content_json)
                normalized_content_json["report_text"] = normalized_content
                record_analytics_event(
                    user_id=user_id,
                    event_type="scouting_report_cached",
                    section="reports",
                    source="scouting_report_request",
                    report_id=str(row["id"]),
                    metadata={"language": row["language"], "version": row["version"], "status": row["status"]},
                    **favorite_snapshot,
                )
                return {
                    "favorite_player_id": favorite_id,
                    "status": row["status"],
                    "content": normalized_content,
                    "content_json": normalized_content_json,
                    "language": row["language"],
                    "version": row["version"],
                    "player": payload,  # NEW
                }
        else:
            record_analytics_event(
                user_id=user_id,
                event_type="scouting_report_pending",
                section="reports",
                source="scouting_report_request",
                report_id=str(row["id"]),
                metadata={"language": row["language"], "version": row["version"], "status": row["status"]},
                **favorite_snapshot,
            )
            return {
                "favorite_player_id": favorite_id,
                "status": row["status"],
                "content": row["content"],
                "content_json": row["content_json"],
                "language": row["language"],
                "version": row["version"],
                "player": payload,  # NEW
            }

    # Create processing record
    rid = str(uuid.uuid4())
    db.execute(text("""
        INSERT INTO scouting_reports (id, user_id, favorite_player_id, status, language, version, created_at, updated_at)
        VALUES (:id, :uid, :fid, 'processing', :lang, :ver, NOW(), NOW())
    """), {"id": rid, "uid": user_id, "fid": favorite_id, "lang": lang, "ver": version})
    db.commit()
    record_analytics_event(
        user_id=user_id,
        event_type="scouting_report_requested",
        section="reports",
        source="scouting_report_request",
        report_id=rid,
        metadata={"language": lang, "version": version},
        **favorite_snapshot,
    )

    # Generate asynchronously with the configured OpenAI chat model and update cache.

    background_tasks.add_task(
        _generate_report_background,
        rid,
        favorite_id,
        user_id,
        lang,
        version,
        player_payload,
    )

    return {
        "favorite_player_id": favorite_id,
        "status": "processing",
        "content": None,
        "content_json": None,
        "language": lang,
        "version": version,
        "player": payload,
    }
