"""Weekly discovery scoring over existing daily answers; UTC daily boundaries."""
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session
from api_module.database import get_db
from api_module.utilities import require_auth

router = APIRouter(prefix='/daily-scout-league', tags=['daily-scout'])


def calendar(now=None):
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(timezone.utc).date()
    week = day - timedelta(days=day.weekday())
    deadline = datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc)
    return now, day, week, deadline


def membership(user, now):
    if not user.get('subscription_end_at') or user['subscription_end_at'] <= now:
        return 'free'
    return 'pro' if user['plan'] in {'Pro Monthly', 'Pro Yearly'} else 'plus' if user['plan'] == 'No Ads Monthly' else 'free'


def bonus(tier):
    return {'plus': 1, 'pro': 2}.get(tier, 0)


# Read through to_jsonb so summary/rankings stay usable before the migration.
WEEKLY = """WITH stats AS (
    SELECT a.user_id, COUNT(*)::int AS played,
      COUNT(*) FILTER (WHERE a.is_correct IS TRUE)::int AS correct,
      MAX(a.completed_at) AS last_answer,
      (array_agg(COALESCE(to_jsonb(a)->>'plan_tier', CASE
        WHEN member.subscription_end_at>:now AND member.plan IN ('Pro Monthly','Pro Yearly') THEN 'pro'
        WHEN member.subscription_end_at>:now AND member.plan='No Ads Monthly' THEN 'plus' ELSE 'free' END) ORDER BY a.completed_at,a.id))[1] AS tier
    FROM public.daily_scout_attempts a JOIN public.users member ON member.id=a.user_id
    WHERE a.completed_at IS NOT NULL AND a.challenge_date>=:week AND a.challenge_date<:end
      AND a.challenge_date<=:today
    GROUP BY a.user_id,member.plan,member.subscription_end_at
), scored AS (
    SELECT s.*,u.community_nickname AS nickname, correct*3 AS "basePoints",
      CASE tier WHEN 'pro' THEN 2 WHEN 'plus' THEN 1 ELSE 0 END AS "bonusPoints"
    FROM stats s JOIN public.users u ON u.id=s.user_id
), ranked AS (
    SELECT *, "basePoints"+"bonusPoints" AS score,
      ROW_NUMBER() OVER (ORDER BY "basePoints"+"bonusPoints" DESC,
        CASE tier WHEN 'pro' THEN 2 WHEN 'plus' THEN 1 ELSE 0 END DESC,last_answer,user_id) AS rank
    FROM scored
) """


def weekly_ranking(db, user_id=0, limit=50, now=None):
    moment, day, week, _ = calendar(now)
    rows = db.execute(text(WEEKLY + 'SELECT * FROM ranked WHERE rank<=:limit OR user_id=:uid ORDER BY rank'),
                      {'week': week, 'end': week+timedelta(days=7), 'today': day, 'now': moment, 'limit': limit, 'uid': user_id}).mappings().all()
    return {'weekStart': week.isoformat(), 'rows': [
        {k: v for k, v in dict(row, isYou=row['user_id']==user_id).items() if k not in {'user_id', 'last_answer'}}
        for row in rows]}


@router.get('')
def state(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    now, day, week, deadline = calendar()
    user = db.execute(text('SELECT community_nickname,plan,subscription_end_at FROM public.users WHERE id=:uid'), {'uid': user_id}).mappings().one()
    rankings = weekly_ranking(db, user_id, now=now)
    own = next((row for row in rankings['rows'] if row['isYou']), None)
    tier = own['tier'] if own else membership(user, now)
    return {'serverNow': now, 'day': day, 'weekStart': week, 'weekEnd': week+timedelta(days=6), 'deadline': deadline,
            'nickname': user['community_nickname'], 'tier': tier,
            'answered': own['played'] if own else 0, 'available': day.weekday()+1,
            'basePoints': own['basePoints'] if own else 0, 'bonusPoints': bonus(tier),
            'totalPoints': own['score'] if own else bonus(tier), 'rank': own['rank'] if own else None,
            'leaderboard': rankings['rows']}


@router.get('/rankings/all-time')
def all_time(sort: str = 'total', user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    _, _, week, _ = calendar()
    primary = '"averagePoints"' if sort == 'average' else '"totalPoints"'
    rows = db.execute(text(f"""WITH stats AS (
        SELECT a.user_id,date_trunc('week',a.challenge_date)::date AS week,
          COUNT(*) FILTER (WHERE a.is_correct IS TRUE)*3 AS base,
          MAX(a.completed_at) AS last_answer,
          (array_agg(COALESCE(to_jsonb(a)->>'plan_tier','free') ORDER BY a.completed_at,a.id))[1] AS tier
        FROM public.daily_scout_attempts a WHERE a.completed_at IS NOT NULL AND a.challenge_date<:week
        GROUP BY a.user_id,date_trunc('week',a.challenge_date)::date
    ), placements AS (
        SELECT *,ROW_NUMBER() OVER (PARTITION BY week ORDER BY
          base+CASE tier WHEN 'pro' THEN 2 WHEN 'plus' THEN 1 ELSE 0 END DESC,
          CASE tier WHEN 'pro' THEN 2 WHEN 'plus' THEN 1 ELSE 0 END DESC,last_answer,user_id) AS place
        FROM stats
    ), totals AS (
        SELECT user_id,COUNT(*) FILTER (WHERE place=1)::int AS championships,
          COUNT(*) FILTER (WHERE place=2)::int AS "secondPlaces",COUNT(*) FILTER (WHERE place=3)::int AS "thirdPlaces",
          COUNT(*)::int AS "weeksParticipated",SUM(CASE WHEN place<=3 THEN 4-place ELSE 0 END)::int AS "totalPoints",
          SUM(CASE WHEN place<=3 THEN 4-place ELSE 0 END)::numeric/COUNT(*) AS "averagePoints"
        FROM placements GROUP BY user_id
    ), ranked AS (
        SELECT t.*,u.community_nickname AS nickname,
          ROW_NUMBER() OVER (ORDER BY {primary} DESC,"totalPoints" DESC,championships DESC,"secondPlaces" DESC,t.user_id) AS rank
        FROM totals t JOIN public.users u ON u.id=t.user_id
    ) SELECT * FROM ranked WHERE rank<=50 OR user_id=:uid ORDER BY rank"""), {'week': week, 'uid': user_id}).mappings().all()
    return [{k: float(v) if k=='averagePoints' else v for k,v in dict(row,isYou=row['user_id']==user_id).items() if k!='user_id'} for row in rows]
