from __future__ import annotations

from typing import Any, Dict, List
import datetime as dt
import json
import os
import random
import ssl
import urllib.error
import urllib.request

import certifi
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.orm import Session

from chatbot_module.metrics import ALLOWED_METRICS, POSITIVE_METRICS
from daily_quiz_module.prompts import DAILY_SCOUT_FALLBACK_STRATEGIES, DAILY_SCOUT_QUIZ_PROMPT, DAILY_SCOUT_THEMES


load_dotenv()


METADATA_SKIP = {
    "id", "name", "player_name", "player_name_norm", "player_key", "player_key_norm",
    "nationality", "nationality_name", "team", "team_name", "team_name_norm",
    "league", "league_name", "league_name_norm", "position_name", "roles", "positions",
    "gender", "age", "height", "weight", "potential", "form", "match_count",
    "minutes", "birth_date",
}

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/")
QUIZ_LLM_TIMEOUT_SECONDS = float(os.getenv("DAILY_SCOUT_LLM_TIMEOUT_SECONDS", "20"))


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _week_start(day: dt.date | None = None) -> dt.date:
    d = day or _today()
    return d - dt.timedelta(days=d.weekday())


def _theme_for_date(day: dt.date | None = None) -> Dict[str, Any]:
    themes = DAILY_SCOUT_THEMES or []
    if not themes:
        return {}
    d = day or _today()
    return themes[d.toordinal() % len(themes)]


def _num(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip().replace("%", ""))
        except ValueError:
            return None
    return None


def _allowed_stats(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    stats: List[Dict[str, Any]] = []
    for metric in sorted(ALLOWED_METRICS):
        value = _num((metadata or {}).get(metric))
        if value is None or abs(value) <= 0.05:
            continue
        stats.append({"metric": metric, "value": value})
    return stats


def _positive_score(stats: List[Dict[str, Any]]) -> float:
    values: List[float] = []
    for stat in stats:
        metric = stat.get("metric")
        value = _num(stat.get("value"))
        if value is None:
            continue
        if metric in POSITIVE_METRICS or metric == "Rating":
            values.append(value)
    if not values:
        values = [_num(s.get("value")) or 0 for s in stats]
    return round(sum(sorted(values, reverse=True)[:8]), 4)


THEME_METRICS = {
    "attacking_impact": {
        "Goals", "Assists", "Shots On Target", "Shots On Target (%)", "Shots Total",
        "Big Chances Created", "Chances Created", "Key Passes", "Passes In Final Third",
        "Successful Dribbles", "Dribble Attempts", "Accurate Crosses", "Total Crosses",
        "Fouls Drawn", "Rating",
    },
    "playmaking_control": {
        "Accurate Passes", "Accurate Passes (%)", "Passes", "Touches", "Through Balls",
        "Through Balls Won", "Long Balls", "Passes In Final Third", "Key Passes",
        "Chances Created", "Ball Recovery", "Rating",
    },
    "transition_engine": {
        "Successful Dribbles", "Dribble Attempts", "Passes In Final Third", "Through Balls",
        "Long Balls", "Touches", "Fouls Drawn", "Ball Recovery", "Interceptions", "Rating",
    },
    "defensive_reliability": {
        "Tackles", "Tackles Won", "Tackles Won (%)", "Interceptions", "Clearances",
        "Blocked Shots", "Ball Recovery", "Duels Won", "Duels Won (%)", "Aerials Won",
        "Aerials Won (%)", "Offsides Provoked", "Clearance Offline", "Last Man Tackle",
        "Rating",
    },
    "balanced_value": {
        "Rating", "Minutes Played", "Touches", "Accurate Passes", "Accurate Passes (%)",
        "Goals", "Assists", "Key Passes", "Duels Won", "Duels Won (%)", "Ball Recovery",
        "Fouls Drawn",
    },
}


def _theme_score(stats: List[Dict[str, Any]], theme: Dict[str, Any] | None) -> float:
    key = (theme or {}).get("key")
    theme_metrics = THEME_METRICS.get(str(key or ""), set())
    values: List[float] = []
    for stat in stats:
        metric = stat.get("metric")
        value = _num(stat.get("value"))
        if value is None:
            continue
        if metric in theme_metrics:
            values.append(value)
    if not values:
        return _positive_score(stats)
    return round(sum(sorted(values, reverse=True)[:8]), 4)


def _role_from_meta(metadata: Dict[str, Any]) -> str | None:
    if metadata.get("position_name"):
        return str(metadata.get("position_name"))
    roles = metadata.get("roles")
    if isinstance(roles, list) and roles:
        return str(roles[0])
    if metadata.get("position"):
        return str(metadata.get("position"))
    return None


def _candidate_summary(choice: Dict[str, Any]) -> Dict[str, Any]:
    content = choice.get("content") or {}
    return {
        "id": choice["id"],
        "name": content.get("player_name") or content.get("name"),
        "gender": content.get("gender"),
        "age": content.get("age"),
        "height": content.get("height"),
        "weight": content.get("weight"),
        "nationality": content.get("nationality_name") or content.get("nationality"),
        "team": content.get("team_name") or content.get("team"),
        "league": content.get("league_name") or content.get("league"),
        "role": _role_from_meta(content),
        "match_count": content.get("match_count"),
        "stats": choice.get("stats") or [],
    }


def _row_to_choice(row: Dict[str, Any]) -> Dict[str, Any] | None:
    metadata = dict(row.get("metadata") or {})
    stats = _allowed_stats(metadata)
    if len(stats) < 5:
        return None
    return {
        "id": str(row["id"]),
        "content": metadata,
        "stats": stats,
        "score": _positive_score(stats),
    }


def _extract_json_object(raw: str) -> Dict[str, Any] | None:
    if not raw:
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _quiz_llm_decision(summaries: List[Dict[str, Any]], theme: Dict[str, Any]) -> Dict[str, Any] | None:
    if not DEEPSEEK_API_KEY:
        print("[daily_scout_quiz] missing DEEPSEEK_API_KEY; using fallback", flush=True)
        return None

    body = {
        "model": "deepseek-chat",
        "temperature": 0.35,
        "messages": [
            {"role": "system", "content": DAILY_SCOUT_QUIZ_PROMPT},
            {
                "role": "user",
                "content": (
                    "Today's required theme JSON:\n"
                    f"{json.dumps(theme, ensure_ascii=False)}\n\n"
                    "Candidate players JSON:\n"
                    f"{json.dumps(summaries, ensure_ascii=False)}\n\n"
                    "Return JSON only."
                ),
            },
        ],
    }
    req = urllib.request.Request(
        f"{DEEPSEEK_API_BASE}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(req, timeout=QUIZ_LLM_TIMEOUT_SECONDS, context=ssl_context) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"[daily_scout_quiz] DeepSeek request failed; using fallback: {exc}", flush=True)
        return None

    try:
        raw = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        print(f"[daily_scout_quiz] DeepSeek response missing content; using fallback: {exc}", flush=True)
        return None

    parsed = _extract_json_object(raw)
    if not parsed:
        print("[daily_scout_quiz] DeepSeek response was not valid JSON; using fallback", flush=True)
    return parsed


def _fallback_decision(choices: List[Dict[str, Any]], theme: Dict[str, Any] | None = None) -> Dict[str, Any]:
    strategy = (theme or {}).get("fallback_strategy") or random.choice(DAILY_SCOUT_FALLBACK_STRATEGIES)
    winner = max(choices, key=lambda c: c.get("theme_score", c["score"]))
    content = winner.get("content") or {}
    name = content.get("player_name") or content.get("name") or "this player"
    return {
        "strategy": strategy,
        "question": {
            "en": "Which player best fits today's scouting strategy?",
            "tr": "Bugünün gözlem stratejisine en iyi hangi oyuncu uyuyor?",
        },
        "winner_player_id": winner["id"],
        "explanation": {
            "en": f"{name} fits the brief because the profile combines reliable match involvement with repeated positive actions. The choice is supported by a balanced evidence base rather than one isolated strength.",
            "tr": f"{name}, güvenilir maç içi katılımı tekrar eden pozitif aksiyonlarla birleştirdiği için bu brief'e uyuyor. Seçim tek bir güçlü yöne değil, dengeli bir veri bütününe dayanıyor.",
        },
    }


def _ai_decision(choices: List[Dict[str, Any]], theme: Dict[str, Any]) -> Dict[str, Any]:
    summaries = [_candidate_summary(choice) for choice in choices]
    parsed = _quiz_llm_decision(summaries, theme)

    valid_ids = {choice["id"] for choice in choices}
    if not parsed or str(parsed.get("winner_player_id")) not in valid_ids:
        if parsed:
            print("[daily_scout_quiz] invalid winner_player_id from DeepSeek; using fallback", flush=True)
        return _fallback_decision(choices, theme)

    strategy = parsed.get("strategy") if isinstance(parsed.get("strategy"), dict) else {}
    explanation = parsed.get("explanation") if isinstance(parsed.get("explanation"), dict) else {}
    fallback = _fallback_decision(choices, theme)
    return {
        "strategy": {
            "en": str(strategy.get("en") or fallback["strategy"]["en"]),
            "tr": str(strategy.get("tr") or fallback["strategy"]["tr"]),
        },
        "question": fallback["question"],
        "winner_player_id": str(parsed.get("winner_player_id")),
        "explanation": {
            "en": str(explanation.get("en") or fallback["explanation"]["en"]),
            "tr": str(explanation.get("tr") or fallback["explanation"]["tr"]),
        },
    }


def _challenge_payload(row: Dict[str, Any], attempt: Dict[str, Any] | None = None, skipped: bool = False) -> Dict[str, Any]:
    choices = row["choices_json"] if isinstance(row["choices_json"], list) else json.loads(row["choices_json"] or "[]")
    winner_id = str(row["winner_player_id"])
    chosen_id = str(attempt["chosen_player_id"]) if attempt and attempt.get("chosen_player_id") is not None else None
    completed = bool(attempt and attempt.get("completed_at"))
    return {
        "challengeId": row["id"],
        "challengeDate": row["challenge_date"].isoformat() if hasattr(row["challenge_date"], "isoformat") else str(row["challenge_date"]),
        "strategy": {"en": row.get("strategy_en") or "", "tr": row.get("strategy_tr") or ""},
        "question": {"en": row["question_en"], "tr": row["question_tr"]},
        "choices": [{"id": c["id"], "content": c["content"]} for c in choices],
        "winnerPlayerId": winner_id if completed else None,
        "explanation": {"en": row["explanation_en"], "tr": row["explanation_tr"]} if completed else None,
        "attempt": {
            "status": "completed" if completed else "skipped" if skipped else "available",
            "chosenPlayerId": chosen_id,
            "isCorrect": bool(attempt["is_correct"]) if attempt and attempt.get("is_correct") is not None else None,
            "score": int(attempt["score"]) if attempt and attempt.get("score") is not None else None,
            "needsNickname": bool(attempt.get("needs_nickname")) if attempt else False,
        },
    }


def ensure_daily_challenge(db: Session) -> Dict[str, Any]:
    today = _today()
    theme = _theme_for_date(today)
    existing = db.execute(
        text("SELECT * FROM daily_scout_challenges WHERE challenge_date = :d LIMIT 1"),
        {"d": today},
    ).mappings().first()
    if existing:
        return dict(existing)

    rows = db.execute(
        text("""
        SELECT id, metadata
        FROM player_data
        WHERE lower(COALESCE(metadata->>'gender', '')) = 'male'
        ORDER BY random()
        LIMIT 120
        """),
    ).mappings().all()

    choices = []
    seen = set()
    for row in rows:
        metadata = dict(row["metadata"] or {})
        name = (metadata.get("player_name") or metadata.get("name") or "").strip().lower()
        if not name or name in seen:
            continue
        choice = _row_to_choice(dict(row))
        if not choice:
            continue
        choices.append(choice)
        seen.add(name)
        if len(choices) == 3:
            break

    if len(choices) < 3:
        raise RuntimeError("Not enough eligible male players with at least 5 allowed stats")

    for choice in choices:
        choice["theme_score"] = _theme_score(choice.get("stats") or [], theme)
    random.shuffle(choices)
    decision = _ai_decision(choices, theme)
    challenge_id = f"daily-{today.isoformat()}"

    db.execute(
        text("""
        INSERT INTO daily_scout_challenges (
            id, challenge_date, strategy_en, strategy_tr, question_en, question_tr, choices_json,
            winner_player_id, explanation_en, explanation_tr, created_at
        ) VALUES (
            :id, :challenge_date, :strategy_en, :strategy_tr, :question_en, :question_tr, CAST(:choices AS jsonb),
            :winner_player_id, :explanation_en, :explanation_tr, NOW()
        )
        ON CONFLICT (challenge_date) DO NOTHING
        """),
        {
            "id": challenge_id,
            "challenge_date": today,
            "strategy_en": decision["strategy"]["en"],
            "strategy_tr": decision["strategy"]["tr"],
            "question_en": decision["question"]["en"],
            "question_tr": decision["question"]["tr"],
            "choices": json.dumps(choices, ensure_ascii=False),
            "winner_player_id": decision["winner_player_id"],
            "explanation_en": decision["explanation"]["en"],
            "explanation_tr": decision["explanation"]["tr"],
        },
    )
    db.commit()
    return dict(db.execute(text("SELECT * FROM daily_scout_challenges WHERE challenge_date = :d"), {"d": today}).mappings().first())



def get_daily_status(db: Session, user_id: int) -> Dict[str, Any]:
    challenge = ensure_daily_challenge(db)
    attempt = db.execute(
        text("""
        SELECT a.*, n.nickname IS NULL AS needs_nickname
        FROM daily_scout_attempts a
        LEFT JOIN daily_scout_weekly_nicknames n
          ON n.user_id = a.user_id AND n.week_start = DATE_TRUNC('week', NOW())::date
        WHERE a.user_id = :uid AND a.challenge_id = :cid
        LIMIT 1
        """),
        {"uid": user_id, "cid": challenge["id"]},
    ).mappings().first()
    return _challenge_payload(challenge, dict(attempt) if attempt else None, skipped=bool(attempt and attempt.get("skipped_at")))


def skip_daily_challenge(db: Session, user_id: int) -> Dict[str, Any]:
    challenge = ensure_daily_challenge(db)
    db.execute(
        text("""
        INSERT INTO daily_scout_attempts (user_id, challenge_id, challenge_date, skipped_at, created_at)
        VALUES (:uid, :cid, :d, NOW(), NOW())
        ON CONFLICT (user_id, challenge_date) DO UPDATE
        SET skipped_at = COALESCE(daily_scout_attempts.skipped_at, NOW())
        WHERE daily_scout_attempts.completed_at IS NULL
        """),
        {"uid": user_id, "cid": challenge["id"], "d": challenge["challenge_date"]},
    )
    db.commit()
    return get_daily_status(db, user_id)


def submit_daily_answer(db: Session, user_id: int, challenge_id: str, chosen_player_id: str) -> Dict[str, Any]:
    challenge = db.execute(text("SELECT * FROM daily_scout_challenges WHERE id = :id"), {"id": challenge_id}).mappings().first()
    if not challenge:
        raise ValueError("Challenge not found")
    existing = db.execute(
        text("""
        SELECT completed_at
        FROM daily_scout_attempts
        WHERE user_id = :uid AND challenge_date = :d
        LIMIT 1
        """),
        {"uid": user_id, "d": challenge["challenge_date"]},
    ).mappings().first()
    if existing and existing.get("completed_at"):
        return get_daily_status(db, user_id)

    choices = challenge["choices_json"] if isinstance(challenge["choices_json"], list) else json.loads(challenge["choices_json"] or "[]")
    valid_ids = {str(c["id"]) for c in choices}
    if str(chosen_player_id) not in valid_ids:
        raise ValueError("Invalid choice")
    correct = str(chosen_player_id) == str(challenge["winner_player_id"])
    score = 100 if correct else 20
    db.execute(
        text("""
        INSERT INTO daily_scout_attempts (
            user_id, challenge_id, challenge_date, chosen_player_id, is_correct, score, skipped_at, completed_at, created_at
        ) VALUES (:uid, :cid, :d, :chosen, :correct, :score, NULL, NOW(), NOW())
        ON CONFLICT (user_id, challenge_date) DO UPDATE
        SET chosen_player_id = COALESCE(daily_scout_attempts.chosen_player_id, EXCLUDED.chosen_player_id),
            is_correct = COALESCE(daily_scout_attempts.is_correct, EXCLUDED.is_correct),
            score = COALESCE(daily_scout_attempts.score, EXCLUDED.score),
            skipped_at = NULL,
            completed_at = COALESCE(daily_scout_attempts.completed_at, NOW())
        """),
        {"uid": user_id, "cid": challenge_id, "d": challenge["challenge_date"], "chosen": str(chosen_player_id), "correct": correct, "score": score},
    )
    db.commit()
    return get_daily_status(db, user_id)


def set_weekly_nickname(db: Session, user_id: int, nickname: str) -> Dict[str, Any]:
    nick = " ".join((nickname or "").strip().split())[:24]
    if len(nick) < 2:
        raise ValueError("Nickname must be at least 2 characters")
    week = _week_start()
    taken = db.execute(
        text("""
        SELECT 1
        FROM daily_scout_weekly_nicknames
        WHERE week_start = :week
          AND user_id <> :uid
          AND lower(nickname) = lower(:nick)
        LIMIT 1
        """),
        {"uid": user_id, "week": week, "nick": nick},
    ).first()
    if taken:
        raise ValueError("Nickname is already taken for this week")

    db.execute(
        text("""
        INSERT INTO daily_scout_weekly_nicknames (user_id, week_start, nickname, created_at)
        VALUES (:uid, :week, :nick, NOW())
        ON CONFLICT (user_id, week_start) DO NOTHING
        """),
        {"uid": user_id, "week": week, "nick": nick},
    )
    db.commit()
    return {"nickname": db.execute(text("SELECT nickname FROM daily_scout_weekly_nicknames WHERE user_id=:uid AND week_start=:week"), {"uid": user_id, "week": week}).scalar_one()}


def get_weekly_leaderboard(db: Session, limit: int = 20) -> Dict[str, Any]:
    week = _week_start()
    rows = db.execute(
        text("""
        SELECT n.nickname,
               SUM(a.score)::int AS score,
               COUNT(*) FILTER (WHERE a.completed_at IS NOT NULL)::int AS played,
               COUNT(*) FILTER (WHERE a.is_correct IS TRUE)::int AS correct
        FROM daily_scout_attempts a
        JOIN daily_scout_weekly_nicknames n
          ON n.user_id = a.user_id AND n.week_start = DATE_TRUNC('week', a.challenge_date)::date
        WHERE a.challenge_date >= :week
          AND a.completed_at IS NOT NULL
        GROUP BY n.user_id, n.nickname
        ORDER BY score DESC, correct DESC, played DESC, n.nickname ASC
        LIMIT :limit
        """),
        {"week": week, "limit": int(limit or 20)},
    ).mappings().all()
    return {"weekStart": week.isoformat(), "rows": [dict(r) for r in rows]}
