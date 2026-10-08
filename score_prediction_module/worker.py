"""Restartable in-process worker. SQL locks coordinate multiple API instances."""
from datetime import timedelta
import json
import logging
import os
import threading

from sqlalchemy import text

from api_module.database import SessionLocal, engine
from match_pool_module.fixtures import get_fixture
from .core import draw_window, fetch_week_fixtures, kickoff, phase, score_entry, select_fixtures, utcnow, week_start

log = logging.getLogger(__name__)
_stop = threading.Event()
_wake = threading.Event()
_thread = None
LOCK_ID = 81423019


def scoring_state(fixture):
    return (phase(fixture), fixture['homeTeam'].get('score'), fixture['awayTeam'].get('score'))


def unresolved(fixture):
    stage, home, away = scoring_state(fixture)
    return stage == 'pending' or (stage == 'finished' and
        (not isinstance(home, int) or not isinstance(away, int)))


def rescore(db, round_id, fixtures):
    last_user = 0
    while True:
        rows = db.execute(text('''SELECT user_id,picks,plan_tier FROM public.prediction_entries
            WHERE round_id=:rid AND submitted_at IS NOT NULL AND user_id>:last
            ORDER BY user_id LIMIT 500'''), {'rid': round_id, 'last': last_user}).mappings().all()
        if not rows:
            return
        for row in rows:
            score = score_entry(row['picks'], fixtures, row['plan_tier'])
            db.execute(text('''UPDATE public.prediction_entries SET base_points=:base,
                bonus_rate=0,bonus_points=:bonus,total_points=:total,exact_scores=:exact,
                match_points=CAST(:details AS jsonb),updated_at=NOW()
                WHERE round_id=:rid AND user_id=:uid'''),
                {**score, 'details': json.dumps(score['details']), 'rid': round_id, 'uid': row['user_id']})
        last_user = rows[-1]['user_id']


def process_round(row):
    now = utcnow()
    fixtures = row['fixtures']
    if not fixtures:
        if draw_window(row['week_start'], now):
            fixtures = select_fixtures(fetch_week_fixtures(row['week_start']), now, competition_week=row['week_start'])
        with SessionLocal.begin() as db:
            if not fixtures:
                db.execute(text("UPDATE public.prediction_rounds SET next_check_at=NOW()+INTERVAL '6 hours' WHERE id=:id"), {'id': row['id']})
                return
            for fixture in fixtures:
                fixture['checkedAt'] = now.isoformat()
            deadline = min(map(kickoff, fixtures))
            db.execute(text('''UPDATE public.prediction_rounds SET fixtures=CAST(:fixtures AS jsonb),
                deadline=:deadline,status='open',updated_at=NOW(),next_check_at=:next
                WHERE id=:id AND status='waiting' '''),
                {'id': row['id'], 'fixtures': json.dumps(fixtures), 'deadline': deadline,
                 'next': min(now + timedelta(hours=6), deadline + timedelta(hours=2))})
        return

    verify = row['status'] == 'finalizing'
    updated, next_times = [], []
    for original in fixtures:
        if _stop.is_set():
            return
        item = dict(original)
        stage = phase(item)
        start = kickoff(item)
        checked = item.get('checkedAt')
        from datetime import datetime
        checked_at = datetime.fromisoformat(checked) if checked else None
        if unresolved(item):
            due = now if checked_at is None else checked_at + timedelta(hours=6)
            if now >= start:
                due = max(start + timedelta(hours=2), (checked_at + timedelta(minutes=30)) if checked_at else start)
        else:
            due = None
        if verify or (due is not None and now >= due):
            fresh = get_fixture(int(item['fixtureId']))
            if not fresh or fresh.get('fixtureId') != item['fixtureId']:
                raise RuntimeError('A selected fixture could not be refreshed')
            item = {**fresh, 'checkedAt': now.isoformat()}
            # Once removed from the week's competition, never reintroduce it.
            if stage == 'excluded' or phase(item) == 'excluded':
                item['excluded'] = True
            stage, start = phase(item), kickoff(item)
        if unresolved(item):
            if start > now:
                next_times.append(min(now + timedelta(hours=6), start + timedelta(hours=2)))
            elif now < start + timedelta(hours=2):
                next_times.append(start + timedelta(hours=2))
            else:
                next_times.append(now + timedelta(minutes=30))
        updated.append(item)
    all_done = all(not unresolved(item) for item in updated)
    status = 'settled' if all_done and verify else 'finalizing' if all_done else 'open'
    next_check = min(next_times) if next_times else now + timedelta(minutes=30)
    # Never extend a published deadline, including when a kickoff is postponed.
    deadline = min([row['deadline'], *[kickoff(f) for f in updated if phase(f) != 'excluded']])
    with SessionLocal.begin() as db:
        db.execute(text('''UPDATE public.prediction_rounds SET fixtures=CAST(:fixtures AS jsonb),
            deadline=:deadline,status=:status,next_check_at=:next,updated_at=NOW() WHERE id=:id'''),
            {'id': row['id'], 'fixtures': json.dumps(updated), 'deadline': deadline, 'status': status, 'next': next_check})
        if [scoring_state(f) for f in updated] != [scoring_state(f) for f in fixtures]:
            rescore(db, row['id'], updated)


def tick():
    # This dedicated connection holds a session lock while fetching, without
    # holding row locks that could block a user's prediction submission.
    with engine.connect() as lock:
        claimed = lock.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': LOCK_ID}).scalar()
        lock.commit()
        if not claimed:
            return
        try:
            with SessionLocal.begin() as db:
                ready = db.execute(text("SELECT to_regclass('public.prediction_rounds') IS NOT NULL")).scalar()
                if not ready:
                    return
                db.execute(text('''INSERT INTO public.prediction_rounds(week_start)
                    VALUES(:week) ON CONFLICT(week_start) DO NOTHING'''), {'week': week_start()})
                rows = [dict(row) for row in db.execute(text('''SELECT * FROM public.prediction_rounds
                    WHERE status<>'settled' AND next_check_at<=NOW()
                    AND (status<>'waiting' OR week_start=:week) ORDER BY week_start LIMIT 8'''),
                    {'week': week_start()}).mappings()]
            for row in rows:
                try:
                    process_round(row)
                except Exception as exc:
                    log.warning('Prediction refresh failed for round=%s (%s)', row['id'], type(exc).__name__)
                    with SessionLocal.begin() as db:
                        db.execute(text("UPDATE public.prediction_rounds SET next_check_at=NOW()+INTERVAL '30 minutes' WHERE id=:id"), {'id': row['id']})
        finally:
            lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LOCK_ID})
            lock.commit()


def _run():
    while not _stop.is_set():
        try:
            tick()
        except Exception as exc:
            log.warning('Prediction worker unavailable (%s)', type(exc).__name__)
        # Database scheduling check only. Provider calls follow 6h / +2h / 30m rules.
        _wake.wait(60)
        _wake.clear()


def start_worker():
    global _thread
    if os.getenv('SCORE_PREDICTION_WORKER_ENABLED', '1') == '0':
        return
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_run, name='score-predictions', daemon=True)
        _thread.start()


def stop_worker():
    _stop.set()
    _wake.set()


def wake_worker():
    _wake.set()
