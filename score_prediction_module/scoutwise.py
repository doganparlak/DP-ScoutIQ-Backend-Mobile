"""ScoutWise's regular score-league account and once-per-round model refresh."""
import json
import logging
import threading
from datetime import timedelta

from sqlalchemy import text

from api_module.database import SessionLocal, engine
from .core import kickoff, phase, score_entry, utcnow, week_start

LOG = logging.getLogger(__name__)
EMAIL = 'scoutwise-forecast@scoutwise.invalid'
NICKNAME = 'ScoutWise'
LOCK_ID = 81423023
_stop = threading.Event()
_wake = threading.Event()
_thread = None
_user_id = None


def initialize():
    global _user_id
    if _user_id is not None:
        return _user_id
    with engine.begin() as db:
        db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': LOCK_ID})
        db.execute(text('''CREATE TABLE IF NOT EXISTS scoutwise_prediction_runs (
            round_id bigint PRIMARY KEY REFERENCES prediction_rounds(id),
            model_id bigint, status text NOT NULL DEFAULT 'pending',
            next_attempt_at timestamptz NOT NULL DEFAULT NOW(),
            skipped_fixtures jsonb NOT NULL DEFAULT '[]'::jsonb,
            updated_at timestamptz NOT NULL DEFAULT NOW())'''))
        db.execute(text('ALTER TABLE scoutwise_prediction_runs ENABLE ROW LEVEL SECURITY'))
        account = db.execute(text('SELECT id,community_nickname,password_hash FROM users WHERE email=:email'), {'email': EMAIL}).mappings().first()
        if account:
            if account['community_nickname'] != NICKNAME or account['password_hash']:
                raise ValueError('ScoutWise account identity does not match')
            _user_id = account['id']
            return _user_id
        taken = db.execute(text('''SELECT EXISTS(SELECT 1 FROM community_league_profiles
            WHERE lower(community_nickname)=lower(:nickname))'''), {'nickname': NICKNAME}).scalar()
        if taken:
            raise ValueError('ScoutWise nickname is already in use')
        # This is a normal users/prediction_entries participant, with no login
        # credentials, paid head start, marketing consent, or simulated history.
        user_id = db.execute(text('''INSERT INTO users(email,community_nickname,plan,created_at,language)
            VALUES(:email,:nickname,'Free',NOW(),'tr') RETURNING id'''),
            {'email': EMAIL, 'nickname': NICKNAME}).scalar_one()
    _user_id = user_id
    return user_id


def predict_fixtures(fixtures, model, now, on_forecast=None):
    from match_pool_module.forecast import build_forecast, utc
    picks, skipped = {}, []
    for fixture in fixtures:
        if phase(fixture) == 'excluded':
            continue
        if kickoff(fixture) <= now or utc(model['asOf']) >= kickoff(fixture):
            raise ValueError('ScoutWise predictions must precede kickoff')
        result = build_forecast(fixture, now, model)
        if result['status'] != 'ready':
            skipped.append({'fixtureId': fixture['fixtureId'], 'reason': result.get('reason')})
            continue
        best = result['forecast']['topScores'][0]
        if on_forecast:
            on_forecast(result)
        picks[str(fixture['fixtureId'])] = {'home': best['home'], 'away': best['away']}
    return picks, skipped


def process_round(round_id, user_id):
    from match_pool_module.forecast import train_shared_model, save_forecast_snapshot
    # The persisted refresh key also makes recovery after a process restart
    # reuse the completed model instead of training twice for the same round.
    result = train_shared_model(refresh_key=f'score-prediction:{round_id}')
    with engine.connect() as db:
        model = db.execute(text('SELECT artifact FROM mobile_match_forecast_models WHERE id=:id'), {'id': result['modelId']}).scalar_one()
        row = db.execute(text('SELECT * FROM prediction_rounds WHERE id=:id'), {'id': round_id}).mappings().one()
    now = utcnow()
    if row['status'] != 'open' or now >= row['deadline']:
        with engine.begin() as db:
            db.execute(text("UPDATE scoutwise_prediction_runs SET status='closed',model_id=:model,updated_at=NOW() WHERE round_id=:id"), {'model': result['modelId'], 'id': round_id})
        return
    picks, skipped = predict_fixtures(row['fixtures'], {**model, 'modelId': result['modelId']}, now, save_forecast_snapshot)
    with SessionLocal.begin() as db:
        # Match the human submission lock and recheck after the upstream work.
        fresh = db.execute(text('SELECT * FROM prediction_rounds WHERE id=:id FOR SHARE'), {'id': round_id}).mappings().one()
        submitted = utcnow()
        if fresh['status'] != 'open' or submitted >= fresh['deadline'] or any(kickoff(f) <= submitted for f in fresh['fixtures'] if phase(f) != 'excluded'):
            raise ValueError('Prediction deadline passed')
        if fresh['fixtures'] != row['fixtures']:
            # checkedAt changes are harmless, but fixture identities/exclusion
            # must agree with the exact set used to generate these picks.
            expected = {(f['fixtureId'], phase(f)) for f in row['fixtures']}
            if expected != {(f['fixtureId'], phase(f)) for f in fresh['fixtures']}:
                raise ValueError('Weekly fixtures changed during prediction')
        if picks:
            score = score_entry(picks, fresh['fixtures'], 'free')
            db.execute(text('''INSERT INTO prediction_entries(round_id,user_id,picks,submitted_at,plan_tier,
                base_points,bonus_points,total_points,exact_scores,match_points)
                VALUES(:rid,:uid,CAST(:picks AS jsonb),:submitted,'free',:base,:bonus,:total,:exact,CAST(:details AS jsonb))
                ON CONFLICT(round_id,user_id) DO NOTHING'''),
                {'rid': round_id, 'uid': user_id, 'picks': json.dumps(picks), 'submitted': submitted,
                 **score, 'details': json.dumps(score['details'])})
        db.execute(text('''UPDATE scoutwise_prediction_runs SET status=:status,model_id=:model,
            skipped_fixtures=CAST(:skipped AS jsonb),updated_at=NOW() WHERE round_id=:id'''),
            {'id': round_id, 'model': result['modelId'], 'skipped': json.dumps(skipped),
             'status': 'submitted' if picks else 'unavailable'})
    LOG.info('ScoutWise round=%s submitted=%s skipped=%s model=%s', round_id, len(picks), len(skipped), result['modelId'])


def tick():
    user_id = initialize()
    # Independent from the result-refresh worker: training never delays score
    # settlement or user requests. Only one API instance owns this task.
    with engine.connect() as lock:
        claimed = lock.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': LOCK_ID}).scalar()
        lock.commit()
        if not claimed:
            return
        try:
            with engine.begin() as db:
                db.execute(text('''INSERT INTO scoutwise_prediction_runs(round_id)
                    SELECT id FROM prediction_rounds WHERE week_start=:week AND status='open'
                    AND deadline>NOW() AND jsonb_array_length(fixtures)=10
                    ON CONFLICT(round_id) DO NOTHING'''), {'week': week_start()})
                ids = db.execute(text('''SELECT j.round_id FROM scoutwise_prediction_runs j
                    JOIN prediction_rounds r ON r.id=j.round_id
                    WHERE j.status='pending' AND j.next_attempt_at<=NOW()
                    AND r.status='open' AND r.deadline>NOW() AND r.week_start=:week'''), {'week': week_start()}).scalars().all()
            for round_id in ids:
                if _stop.is_set():
                    break
                try:
                    process_round(round_id, user_id)
                except Exception as exc:
                    LOG.warning('ScoutWise round=%s failed (%s)', round_id, type(exc).__name__)
                    with engine.begin() as db:
                        db.execute(text('''UPDATE scoutwise_prediction_runs
                            SET next_attempt_at=:next,updated_at=NOW() WHERE round_id=:id'''),
                            {'id': round_id, 'next': utcnow() + timedelta(minutes=15)})
        finally:
            lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LOCK_ID})
            lock.commit()


def _run():
    while not _stop.is_set():
        try:
            tick()
        except Exception as exc:
            LOG.warning('ScoutWise competitor unavailable (%s)', type(exc).__name__)
        _wake.wait(60)
        _wake.clear()


def start_worker():
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_run, name='scoutwise-competitor', daemon=True)
        _thread.start()


def stop_worker():
    _stop.set()
    _wake.set()


def wake_worker():
    _wake.set()
