from datetime import date
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, EmailStr, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from api_module.community import save_community_nickname
from api_module.database import get_db
from api_module.utilities import require_auth
from .prizes import get_week_prizes, send_prize_claim_email
from .core import draw_window, kickoff, phase, plan_bonus, score_entry, utcnow, week_start
from .worker import start_worker, stop_worker, wake_worker
from .honors import ORDER_SQL, all_time_ranking, podium_finishes

router = APIRouter(prefix='/score-prediction', tags=['score-prediction'])


def schema_error(db, exc):
    db.rollback()
    if getattr(exc.orig, 'pgcode', None) in {'42P01', '42703'}:
        raise HTTPException(503, 'SCORE_PREDICTION_SCHEMA_REQUIRED') from None
    raise exc


@router.on_event('startup')
def startup():
    start_worker()


@router.on_event('shutdown')
def shutdown():
    stop_worker()


def ranking(db, round_id, user_id):
    # Rank every submitted entry, return a bounded list and the user's own rank.
    rows = db.execute(text(f'''WITH ranked AS (
        SELECT e.user_id,u.community_nickname AS nickname,e.base_points AS "basePoints",
        0 AS "bonusPoints",e.base_points AS "totalPoints",e.exact_scores AS "exactScores",
        e.plan_tier AS tier,ROW_NUMBER() OVER(ORDER BY {ORDER_SQL}) AS rank
        FROM public.prediction_entries e JOIN public.users u ON u.id=e.user_id
        WHERE e.round_id=:rid AND e.submitted_at IS NOT NULL)
        SELECT * FROM ranked WHERE rank<=50 OR user_id=:uid ORDER BY rank'''),
        {'rid': round_id, 'uid': user_id}).mappings().all()
    result = [{**dict(row), 'isYou': row['user_id'] == user_id} for row in rows]
    for row in result:
        del row['user_id']
    return result


def public_entry(row, fixtures=None):
    if not row:
        return None
    score = score_entry(row['picks'], fixtures, row['plan_tier']) if row['submitted_at'] and fixtures is not None else None
    return {'picks': row['picks'], 'submittedAt': row['submitted_at'], 'basePoints': score['base'] if score else row['base_points'],
            'bonusPoints': 0, 'totalPoints': score['total'] if score else row['base_points'],
            'exactScores': score['exact'] if score else row['exact_scores'], 'matchPoints': score['details'] if score else row['match_points'],
            'tier': row['plan_tier'], 'bonusRate': 0}


@router.get('/rankings/all-time')
def all_time(sort: Literal['total', 'average'] = 'total', user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        return all_time_ranking(db, user_id, sort)
    except ProgrammingError as exc:
        schema_error(db, exc)


@router.get('/honors')
def honors(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    return podium_finishes(db, user_id)


@router.get('')
def current(weekStart: date | None = None, user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        chosen = weekStart or week_start()
        row = db.execute(text('SELECT * FROM public.prediction_rounds WHERE week_start=:week'), {'week': chosen}).mappings().first()
        user = db.execute(text('SELECT community_nickname,plan,subscription_end_at FROM public.users WHERE id=:uid'), {'uid': user_id}).mappings().one()
        weeks = db.execute(text("SELECT week_start FROM public.prediction_rounds ORDER BY week_start DESC LIMIT 12")).scalars().all()
        if not row:
            wake_worker()
        now = utcnow()
        entry = None
        favorites = []
        if row:
            entry = db.execute(text('SELECT * FROM public.prediction_entries WHERE round_id=:rid AND user_id=:uid'), {'rid': row['id'], 'uid': user_id}).mappings().first()
            ids = [int(f['fixtureId']) for f in row['fixtures']]
            if ids:
                favorites = [dict(f) for f in db.execute(text('''SELECT id::text AS "favoriteId",fixture_payload AS fixture,
                    report_type AS "reportType",report_status AS "reportStatus",created_at AS "createdAt"
                    FROM favorite_matches WHERE user_id=:uid AND fixture_id=ANY(CAST(:ids AS bigint[]))'''),
                    {'uid': user_id, 'ids': ids}).mappings()]
        tier, _ = plan_bonus(user, now)
        return {'viewerId': str(user_id), 'serverNow': now, 'nickname': user['community_nickname'], 'tier': tier,
                'headStartPoints': 0, 'bonusRate': 0,
                'weeks': weeks, 'entry': public_entry(entry, row['fixtures'] if row else None), 'favorites': favorites,
                'prizeClaimSubmitted': bool(entry and entry.get('prize_claim')),
                'round': None if not row else {'id': row['id'], 'weekStart': row['week_start'], 'deadline': row['deadline'],
                    'status': 'unavailable' if row['status'] == 'waiting' and not draw_window(row['week_start'], now) else
                              'closed' if row['status'] == 'open' and now >= row['deadline'] else row['status'],
                    'fixtures': [{**f, 'predictionStatus': phase(f)} for f in row['fixtures']], 'updatedAt': row['updated_at']},
                'leaderboard': ranking(db, row['id'], user_id) if row else [],
                **podium_finishes(db, user_id)}
    except ProgrammingError as exc:
        schema_error(db, exc)


@router.get('/prize-claims/pending')
def pending_prize_claims(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        # Only rank finalized competitions this user entered and has not claimed.
        # to_jsonb keeps this read compatible before the nullable column is added.
        rows = db.execute(text(f'''WITH eligible AS (
            SELECT r.id, r.week_start FROM public.prediction_rounds r
            JOIN public.prediction_entries mine ON mine.round_id=r.id
            WHERE r.status='settled' AND mine.user_id=:uid
              AND mine.submitted_at IS NOT NULL
              AND COALESCE(to_jsonb(mine)->'prize_claim', 'null'::jsonb)='null'::jsonb
        ), ranked AS (
            SELECT e.user_id, r.id AS "roundId", r.week_start AS "weekStart",
                   ROW_NUMBER() OVER (PARTITION BY r.id ORDER BY {ORDER_SQL}) AS rank
            FROM public.prediction_entries e JOIN eligible r ON r.id=e.round_id
            WHERE e.submitted_at IS NOT NULL
        ) SELECT "roundId", "weekStart", rank FROM ranked
          WHERE user_id=:uid AND rank<=3 ORDER BY "weekStart" DESC'''), {'uid': user_id}).mappings().all()
        return [dict(row) for row in rows]
    except ProgrammingError as exc:
        schema_error(db, exc)


@router.get('/prizes/mine')
def my_prizes(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        rows = db.execute(text(f'''WITH eligible AS (
            SELECT r.id, r.week_start, r.prize_snapshot FROM public.prediction_rounds r
            JOIN public.prediction_entries mine ON mine.round_id=r.id
            WHERE r.status='settled' AND mine.user_id=:uid AND mine.submitted_at IS NOT NULL
        ), ranked AS (
            SELECT e.user_id, r.id AS "roundId", r.week_start AS "weekStart", r.prize_snapshot AS prizes,
                   COALESCE(to_jsonb(e)->'prize_claim' <> 'null'::jsonb, false) AS claimed,
                   ROW_NUMBER() OVER (PARTITION BY r.id ORDER BY {ORDER_SQL}) AS rank
            FROM public.prediction_entries e JOIN eligible r ON r.id=e.round_id
            WHERE e.submitted_at IS NOT NULL
        ) SELECT "roundId", "weekStart", rank, claimed, prizes,
                 "weekStart"=(SELECT MAX(week_start) FROM public.prediction_rounds WHERE status='settled') AS "isLatest"
          FROM ranked WHERE user_id=:uid AND rank<=3 ORDER BY "weekStart" DESC'''), {'uid': user_id}).mappings().all()
        return [dict(row) for row in rows]
    except ProgrammingError as exc:
        schema_error(db, exc)


class PrizeClaimIn(BaseModel):
    roundId: int = Field(gt=0)
    contactEmail: EmailStr | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, min_length=7, max_length=30, pattern=r'^\+?[0-9 ()\-]+$')

    @field_validator('contactEmail', 'phone', mode='before')
    @classmethod
    def normalize_contact(cls, value):
        return value.strip() or None if isinstance(value, str) else value

    @field_validator('phone')
    @classmethod
    def valid_phone(cls, value):
        if value is None:
            return None
        digits = ''.join(c for c in value if c.isdigit())
        if not 7 <= len(digits) <= 15:
            raise ValueError('Phone must contain 7 to 15 digits')
        return value.strip()

    @model_validator(mode='after')
    def require_contact(self):
        if not self.contactEmail and not self.phone:
            raise ValueError('Provide a contact email or phone number')
        return self


@router.post('/prize-claim')
def claim_prize(payload: PrizeClaimIn, user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        competition = db.execute(text('SELECT * FROM public.prediction_rounds WHERE id=:rid FOR SHARE'), {'rid': payload.roundId}).mappings().first()
        if not competition or competition['status'] != 'settled':
            raise HTTPException(409, 'PRIZE_RESULTS_NOT_FINAL')
        # The authenticated user and server ranking determine eligibility, never client input.
        own = next((r for r in ranking(db, competition['id'], user_id) if r['isYou']), None)
        if not own or own['rank'] > 3:
            raise HTTPException(403, 'PRIZE_WINNERS_ONLY')
        saved = db.execute(text('SELECT prize_claim FROM public.prediction_entries WHERE round_id=:rid AND user_id=:uid FOR UPDATE'),
                           {'rid': competition['id'], 'uid': user_id}).mappings().one()
        if saved['prize_claim']:
            db.rollback()
            return {'submitted': True}
        try:
            prize_list = competition.get('prize_snapshot') or get_week_prizes(competition['week_start'])
        except Exception:
            db.rollback()
            raise HTTPException(503, 'PRIZE_CONFIGURATION_UNAVAILABLE') from None
        claim = {'contactEmail': str(payload.contactEmail) if payload.contactEmail else None, 'phone': payload.phone,
                 'rank': int(own['rank']), 'submittedAt': utcnow().isoformat(), 'prizes': prize_list}
        # Keep this user's row locked until SMTP accepts the message. Failure rolls back
        # so a genuine retry remains possible; concurrent double taps send only once.
        try:
            send_prize_claim_email(user_id=user_id, nickname=own['nickname'], week=competition['week_start'],
                                   rank=own['rank'], contact_email=claim['contactEmail'], phone=claim['phone'], prizes=prize_list)
        except Exception:
            db.rollback()
            raise HTTPException(503, 'PRIZE_CLAIM_EMAIL_FAILED') from None
        db.execute(text('UPDATE public.prediction_entries SET prize_claim=CAST(:claim AS jsonb) WHERE round_id=:rid AND user_id=:uid'),
                   {'claim': json.dumps(claim), 'rid': competition['id'], 'uid': user_id})
        db.commit()
        return {'submitted': True}
    except ProgrammingError as exc:
        schema_error(db, exc)


class NicknameIn(BaseModel):
    nickname: str = Field(min_length=2, max_length=24)


@router.post('/nickname')
def nickname(payload: NicknameIn, user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        return save_community_nickname(db, user_id, payload.nickname)
    except ProgrammingError as exc:
        schema_error(db, exc)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class Pick(BaseModel):
    home: Annotated[int, Field(strict=True, ge=0, le=99)]
    away: Annotated[int, Field(strict=True, ge=0, le=99)]


class EntryIn(BaseModel):
    roundId: int = Field(gt=0)
    picks: dict[str, Pick] = Field(max_length=10)
    submit: bool = False


@router.post('/entry')
def entry(payload: EntryIn, user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    try:
        # A short shared lock serializes submissions against deadline/fixture updates.
        row = db.execute(text('SELECT * FROM public.prediction_rounds WHERE id=:id FOR SHARE'), {'id': payload.roundId}).mappings().first()
        if not row:
            raise HTTPException(404, 'Round not found')
        # Serialize double taps from the same account, not other participants.
        user = db.execute(text('SELECT community_nickname,plan,subscription_end_at FROM public.users WHERE id=:uid FOR UPDATE'), {'uid': user_id}).mappings().one()
        previous = db.execute(text('SELECT * FROM public.prediction_entries WHERE round_id=:rid AND user_id=:uid'), {'rid': row['id'], 'uid': user_id}).mappings().first()
        now = utcnow()
        if row['status'] != 'open' or not row['deadline'] or now >= row['deadline'] or any(kickoff(f) <= now for f in row['fixtures'] if phase(f) != 'excluded'):
            raise HTTPException(409, 'PREDICTIONS_CLOSED')
        if not user['community_nickname']:
            raise HTTPException(409, 'NICKNAME_REQUIRED')
        valid = {str(f['fixtureId']) for f in row['fixtures'] if phase(f) != 'excluded'}
        if not payload.picks or set(payload.picks) - valid:
            raise HTTPException(400, 'Choose an available match prediction')
        published = bool(previous and previous['submitted_at'])
        incoming = {key: value.model_dump() for key, value in payload.picks.items()}
        # A request edits only its matches. Previously submitted predictions for
        # other matches survive both single-match saves and concurrent requests.
        picks = {**(previous['picks'] if previous and (published or not payload.submit) else {}), **incoming}
        if published and picks == previous['picks']:
            return public_entry(previous, row['fixtures'])
        # The plan snapshot is a display label only. Updating a score moves
        # the equal-points tie-break time to the latest changed submission.
        tier = previous['plan_tier'] if published else plan_bonus(user, now)[0]
        submitted = now if payload.submit or published else None
        score = score_entry(picks, row['fixtures'], tier) if submitted else {'base': 0, 'bonus': 0, 'total': 0, 'exact': 0, 'details': {}}
        # Legacy bonus columns remain for schema/client compatibility, always zero.
        saved = db.execute(text('''INSERT INTO public.prediction_entries(round_id,user_id,picks,submitted_at,plan_tier,bonus_rate,
            base_points,bonus_points,total_points,exact_scores,match_points)
            VALUES(:rid,:uid,CAST(:picks AS jsonb),:submitted,:tier,0,:base,:bonus,:total,:exact,CAST(:details AS jsonb))
            ON CONFLICT(round_id,user_id) DO UPDATE SET picks=EXCLUDED.picks,submitted_at=EXCLUDED.submitted_at,
            plan_tier=EXCLUDED.plan_tier,bonus_rate=0,base_points=EXCLUDED.base_points,bonus_points=EXCLUDED.bonus_points,
            total_points=EXCLUDED.total_points,exact_scores=EXCLUDED.exact_scores,match_points=EXCLUDED.match_points,updated_at=NOW()
            RETURNING *'''),
            {'rid': row['id'], 'uid': user_id, 'picks': json.dumps(picks),
             'submitted': submitted, 'tier': tier, **score, 'details': json.dumps(score['details'])}).mappings().one()
        db.commit()
        return public_entry(saved, row['fixtures'])
    except ProgrammingError as exc:
        schema_error(db, exc)
