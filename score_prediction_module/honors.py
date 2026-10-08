"""Competition honors derived from settled rounds; no extra tables or counters."""
from sqlalchemy import text

SCORE_SQL = "e.base_points"
ORDER_SQL = f"{SCORE_SQL} DESC, e.submitted_at, e.user_id"


def podium_finishes(db, user_id):
    ready = db.execute(text("SELECT to_regclass('public.prediction_rounds') IS NOT NULL AND to_regclass('public.prediction_entries') IS NOT NULL")).scalar()
    if not ready:
        return {'championships': 0, 'secondPlaces': 0, 'thirdPlaces': 0}
    # Rank only finalized competitions in which this user submitted predictions.
    row = db.execute(text(f'''WITH eligible AS (
        SELECT r.id FROM public.prediction_rounds r
        WHERE r.status='settled' AND EXISTS (
            SELECT 1 FROM public.prediction_entries mine WHERE mine.round_id=r.id
            AND mine.user_id=:uid AND mine.submitted_at IS NOT NULL)
    ), ranked AS (
        SELECT e.user_id, ROW_NUMBER() OVER (PARTITION BY e.round_id ORDER BY {ORDER_SQL}) AS place
        FROM public.prediction_entries e JOIN eligible r ON r.id=e.round_id
        WHERE e.submitted_at IS NOT NULL
    ) SELECT COUNT(*) FILTER (WHERE place=1) AS championships,
        COUNT(*) FILTER (WHERE place=2) AS "secondPlaces",
        COUNT(*) FILTER (WHERE place=3) AS "thirdPlaces"
        FROM ranked WHERE user_id=:uid'''), {'uid': user_id}).mappings().one()
    return {key: int(value) for key, value in row.items()}


def championships(db, user_id):
    return podium_finishes(db, user_id)['championships']


def all_time_ranking(db, user_id, sort='total'):
    primary = 't."averagePoints"' if sort == 'average' else 't."totalPoints"'
    rows = db.execute(text(f'''WITH placements AS (
        SELECT e.user_id, ROW_NUMBER() OVER (PARTITION BY e.round_id ORDER BY {ORDER_SQL}) AS place
        FROM public.prediction_entries e JOIN public.prediction_rounds r ON r.id=e.round_id
        WHERE r.status='settled' AND e.submitted_at IS NOT NULL
    ), totals AS (
        SELECT user_id, COUNT(*) FILTER (WHERE place=1) AS championships,
            COUNT(*) FILTER (WHERE place=2) AS "secondPlaces",
            COUNT(*) FILTER (WHERE place=3) AS "thirdPlaces",
            COUNT(*) AS "weeksParticipated",
            SUM(CASE WHEN place<=3 THEN 4-place ELSE 0 END) AS "totalPoints",
            SUM(CASE WHEN place<=3 THEN 4-place ELSE 0 END)::numeric / COUNT(*) AS "averagePoints"
        FROM placements GROUP BY user_id
    ), ranked AS (
        SELECT t.*, u.community_nickname AS nickname,
            ROW_NUMBER() OVER (ORDER BY {primary} DESC, t."totalPoints" DESC, t.championships DESC,
                t."secondPlaces" DESC, t.user_id) AS rank
        FROM totals t JOIN public.users u ON u.id=t.user_id
    ) SELECT * FROM ranked WHERE rank<=50 OR user_id=:uid ORDER BY rank'''), {'uid': user_id}).mappings().all()
    return [{'nickname': row['nickname'], 'championships': int(row['championships']),
             'secondPlaces': int(row['secondPlaces']), 'thirdPlaces': int(row['thirdPlaces']),
             'totalPoints': int(row['totalPoints']), 'weeksParticipated': int(row['weeksParticipated']),
             'averagePoints': float(row['averagePoints']), 'rank': int(row['rank']),
             'isYou': row['user_id'] == user_id} for row in rows]
