"""Idempotent, backend-only league participation for registered automation accounts.

No credentials, purchases, notification subscriptions, or historical answers are
created. Only accounts explicitly registered here can receive automated entries.
"""
from datetime import timedelta
import json
import random

from sqlalchemy import text

from daily_quiz_module.league import calendar
from score_prediction_module.core import kickoff, phase, score_entry, utcnow, week_start

LOCK_ID = 81423020
NICKNAMES = '''emre1907 berkayylmz can1905 mert.10 furkan_61 oguzhn34 arda1903 ahmetkaya kerem7 burak_1998
 yusufbjk kaan1907 efe.17 serhatgs tolga61 alperen_ deniz_10 onurpl batuhann hakan1905
 berkay.34 enesfb umt1903 eren.9 murat_06 alexm92 johnny_7 marco.10 lucasfc daniel_23
 tom.arsenal pedro_rm nico_1999 matteo_9 jamespl leo.messi10 andreasbvb adamcfc rafinha_11 erikfcb
 kadikoylu cimbomlu1905 karakartal03 bordo_mavi eskiacik premwatcher laliga_10 serieafan bundes_09 ucl.23'''.split()
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS public.community_automation_cohorts (
        league text NOT NULL CHECK(league IN ('prediction','discovery')),
        week_start date NOT NULL,
        user_id bigint NOT NULL REFERENCES public.community_automation_accounts(user_id) ON DELETE CASCADE,
        PRIMARY KEY(league,week_start,user_id))''',
    '''CREATE TABLE IF NOT EXISTS public.community_automation_settings (
        id boolean PRIMARY KEY DEFAULT TRUE CHECK(id),
        enabled boolean NOT NULL DEFAULT FALSE,
        next_run_at timestamptz NOT NULL DEFAULT NOW())''',
    '''ALTER TABLE public.community_automation_settings
        ADD COLUMN IF NOT EXISTS next_run_at timestamptz NOT NULL DEFAULT NOW()''',
)


def seed_accounts(db, rng=None):
    """Explicit administrative operation; never called by the periodic job."""
    rng = rng or random.SystemRandom()
    from .storage import migrate
    migrate(db)
    for statement in SCHEMA:
        db.execute(text(statement))
    existing = db.execute(text('SELECT nickname,tier FROM community_automation_accounts')).mappings().all()
    if existing:
        if len(existing) != 50 or {r['nickname'] for r in existing} != set(NICKNAMES):
            raise ValueError('Automation registry differs from the requested 50 accounts')
        return
    collisions = db.execute(text('SELECT count(*) FROM users WHERE lower(community_nickname)=ANY(CAST(:names AS text[]))'),
                            {'names': [n.lower() for n in NICKNAMES]}).scalar_one()
    if collisions:
        raise ValueError('Nickname collision: refusing to reuse an existing user name')
    tiers = ['pro'] * 10 + ['plus'] * 18 + ['free'] * 22
    rng.shuffle(tiers)
    for nickname, tier in zip(NICKNAMES, tiers):
        db.execute(text('INSERT INTO community_automation_accounts(nickname,tier) VALUES(:nickname,:tier)'),
                   {'nickname': nickname, 'tier': tier})
    db.execute(text('INSERT INTO community_automation_settings(id,enabled) VALUES(TRUE,TRUE) ON CONFLICT(id) DO UPDATE SET enabled=TRUE'))


def cohort(db, league, week, accounts, count, rng):
    rows = db.execute(text('''SELECT user_id FROM community_automation_cohorts
        WHERE league=:league AND week_start=:week ORDER BY user_id'''), {'league': league, 'week': week}).scalars().all()
    if not rows:
        rows = rng.sample([a['user_id'] for a in accounts], count)
        for uid in rows:
            db.execute(text('''INSERT INTO community_automation_cohorts(league,week_start,user_id)
                VALUES(:league,:week,:uid)'''), {'league': league, 'week': week, 'uid': uid})
    if len(rows) != count:
        raise ValueError('Unexpected automation cohort size')
    return rows


def random_picks(fixtures, rng):
    return {str(f['fixtureId']): {'home': rng.randint(0, 3), 'away': rng.randint(0, 3)}
            for f in fixtures if phase(f) != 'excluded'}


def run(db, now=None, rng=None):
    """Call in a transaction; safe to retry and safe across multiple API workers."""
    now, rng = now or utcnow(), rng or random.SystemRandom()
    ready = db.execute(text("SELECT to_regclass('public.community_automation_settings') IS NOT NULL")).scalar_one()
    if not ready:
        return {'prediction': 0, 'discovery': 0}
    claimed = db.execute(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': LOCK_ID}).scalar_one()
    if not claimed:
        return {'prediction': 0, 'discovery': 0}
    settings = db.execute(text('SELECT enabled,next_run_at FROM community_automation_settings WHERE id=TRUE FOR UPDATE')).mappings().first()
    if not settings or not settings['enabled'] or now < settings['next_run_at']:
        return {'prediction': 0, 'discovery': 0}
    accounts = [dict(a) for a in db.execute(text('SELECT user_id,tier FROM community_automation_accounts ORDER BY user_id')).mappings()]
    if len(accounts) != 50:
        raise ValueError('Expected exactly 50 registered automation accounts')
    tiers = {a['user_id']: a['tier'] for a in accounts}
    counts = {'prediction': 0, 'discovery': 0}
    row = db.execute(text('SELECT * FROM prediction_rounds WHERE week_start=:week FOR SHARE'),
                     {'week': week_start(now)}).mappings().first()
    if row and row['status'] == 'open' and row['deadline'] and now < row['deadline'] and row['fixtures']:
        fixtures = row['fixtures']
        if all(kickoff(f) > now for f in fixtures if phase(f) != 'excluded'):
            for uid in cohort(db, 'prediction', row['week_start'], accounts, 40, rng):
                # Do not reroll guesses or change a first-submission tier/time.
                if db.execute(text('SELECT 1 FROM community_automation_prediction_entries WHERE round_id=:rid AND user_id=:uid'),
                              {'rid': row['id'], 'uid': uid}).scalar():
                    continue
                tier = tiers[uid]
                picks = random_picks(fixtures, rng)
                score = score_entry(picks, fixtures, tier)
                counts['prediction'] += db.execute(text('''INSERT INTO community_automation_prediction_entries(
                    round_id,user_id,picks,submitted_at,plan_tier,bonus_rate,base_points,
                    bonus_points,total_points,exact_scores,match_points)
                    VALUES(:rid,:uid,CAST(:picks AS jsonb),:now,:tier,0,:base,:bonus,:total,:exact,CAST(:details AS jsonb))
                    ON CONFLICT(round_id,user_id) DO NOTHING'''),
                    {'rid': row['id'], 'uid': uid, 'picks': json.dumps(picks), 'now': now,
                     'tier': tier, **score, 'details': json.dumps(score['details'])}).rowcount
    _, day, week, _ = calendar(now)
    challenge = db.execute(text('SELECT id,choices_json,winner_player_id FROM daily_scout_challenges WHERE challenge_date=:day'),
                           {'day': day}).mappings().first()
    if challenge:
        choices = challenge['choices_json']
        if isinstance(choices, str):
            choices = json.loads(choices)
        if not isinstance(choices, list) or len(choices) != 3 or len({str(c['id']) for c in choices}) != 3:
            raise ValueError('Daily challenge must have three distinct player choices')
        for uid in cohort(db, 'discovery', week, accounts, 30, rng):
            if db.execute(text('SELECT 1 FROM community_automation_discovery_attempts WHERE user_id=:uid AND challenge_date=:day'),
                          {'uid': uid, 'day': day}).scalar():
                continue
            chosen = str(rng.choice(choices)['id'])
            correct = chosen == str(challenge['winner_player_id'])
            counts['discovery'] += db.execute(text('''INSERT INTO community_automation_discovery_attempts(
                user_id,challenge_id,challenge_date,chosen_player_id,is_correct,score,completed_at,plan_tier)
                VALUES(:uid,:cid,:day,:chosen,:correct,:score,:now,:tier)
                ON CONFLICT(user_id,challenge_date) DO NOTHING'''),
                {'uid': uid, 'cid': challenge['id'], 'day': day, 'chosen': chosen,
                 'correct': correct, 'score': 3 if correct else 0, 'now': now, 'tier': tiers[uid]}).rowcount
    db.execute(text('UPDATE community_automation_settings SET next_run_at=:next WHERE id=TRUE'),
               {'next': now + timedelta(hours=6)})
    return counts
