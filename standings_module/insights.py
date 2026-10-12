"""Database-backed league summaries and a bounded, restartable refresh worker."""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import date, datetime, timedelta, timezone

import requests
from sqlalchemy import text

from api_module.database import SessionLocal
from .stages import EUROPEAN_LEAGUES, SCOPES, scoped_schedule
from .metrics import Aggregate, COMPLETED_STATES, PERIOD_ZONE, VERSION, kickoff, normalize_fixture, period_for

log = logging.getLogger(__name__)
BASE = os.getenv('SPORTMONKS_BASE_URL', 'https://api.sportmonks.com/v3/football').rstrip('/')
_stop = threading.Event()
_wake = threading.Event()
_thread = None
CLAIM_LOCK = 716394820
SCOPED_VERSION = 4


def provider(session, path, **params):
    try:
        response = session.get(f'{BASE}/{path}', params=params, timeout=(10, 45))
        if response.status_code != 200:
            raise RuntimeError(f'League statistics provider returned HTTP {response.status_code}')
        value = response.json()
        if 'data' not in value:
            raise RuntimeError('League statistics provider returned no data')
        return value['data']
    except (requests.RequestException, ValueError):
        # Never expose request URLs or credentials in job errors.
        raise RuntimeError('League statistics provider is temporarily unavailable') from None


def request_summary(db, league_id, season_id, period_start=None, scope=None):
    params = {'league': league_id, 'season': season_id}
    row = db.execute(text('''SELECT league_id, season_id, status, computed_at, next_refresh_at,
        team_metrics, metric_catalog, team_best_players, league_best_player, fixture_count, coverage,
        calculation_version, last_error FROM league_season_summaries
        WHERE league_id=:league AND season_id=:season'''), params).mappings().first()
    if row is None:
        raise ValueError('Open the league standings before requesting its statistics')
    periods = db.execute(text('''SELECT period_start, period_end, fixture_count, computed_at
        FROM league_biweekly_summaries WHERE league_id=:league AND season_id=:season
        ORDER BY period_start DESC'''), params).mappings().all()
    chosen = period_start or period_for(datetime.now(PERIOD_ZONE).date())[0]
    if period_start and period_for(period_start)[0] != period_start:
        raise ValueError('Invalid two-week period')
    biweek = db.execute(text('''SELECT period_start, period_end, status, computed_at,
        team_best_players, league_best_player, fixture_count, coverage
        FROM league_biweekly_summaries WHERE league_id=:league AND season_id=:season
        AND period_start=:period'''), {**params, 'period': chosen}).mappings().first()
    # On a new period boundary, queue a refresh once so an empty or newly played
    # period is materialized instead of showing the preceding period as current.
    if biweek is None and not period_start and row['computed_at'] and row['status'] == 'ready':
        db.execute(text('''UPDATE league_season_summaries SET next_refresh_at=NOW()
            WHERE league_id=:league AND season_id=:season AND status='ready' ''') , params)
        db.commit()
    season_result = dict(row)
    biweekly_result = dict(biweek) if biweek else None
    coverage = dict(season_result.get('coverage') or {})
    scoped = coverage.pop('stageSummaries', {})
    season_result['coverage'] = coverage
    scope = scope or ('league' if league_id in EUROPEAN_LEAGUES else 'overall')
    if league_id in EUROPEAN_LEAGUES:
        # Never serve the old whole-season cache while the scoped rebuild runs.
        ready = row['calculation_version'] == SCOPED_VERSION and scope in scoped
        if ready:
            selected = scoped[scope]
            season_result.update(selected['season'])
            periods = selected['periods']
            biweekly_result = next((dict(p) for p in periods if str(p['period_start']) == str(chosen)), None)
        else:
            season_result.update(Aggregate().finish())
            season_result.update(status='pending' if row['status'] != 'failed' else 'failed', computed_at=None)
            periods, biweekly_result = [], None
        season_result.pop('player_rating_totals', None)
    if biweekly_result:
        biweekly_result['coverage'] = {k: v for k, v in (biweekly_result.get('coverage') or {}).items() if k != 'stageSummaries'}
    # Images can be refreshed independently of the weekly statistics cache.
    hydrate_winner_images(db, season_result)
    if biweekly_result:
        hydrate_winner_images(db, biweekly_result)
    return {'leagueId': league_id, 'seasonId': season_id, 'season': season_result,
            'biweekly': biweekly_result,
            'periods': [dict(p) for p in periods], 'selectedPeriodStart': chosen,
            'periodTimezone': 'Europe/Istanbul',
            # A missing period can wait for the next scheduled check; do not
            # make browsers poll for hours while no refresh is running.
            'scope': scope, 'refreshing': row['status'] in {'pending', 'processing'} or (league_id in EUROPEAN_LEAGUES and not ready and row['status'] != 'failed')}


def claim_job():
    with SessionLocal.begin() as db:
        # Serialize claims across processes. One live job across the service,
        # with a renewable lease so a restarted process cannot strand work.
        db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': CLAIM_LOCK})
        if db.execute(text("SELECT 1 FROM league_season_summaries WHERE status='processing' AND lease_expires_at>NOW() LIMIT 1")).first():
            return None
        row = db.execute(text('''SELECT league_id, season_id FROM league_season_summaries
            WHERE status='pending'
               OR (status='processing' AND (lease_expires_at IS NULL OR lease_expires_at<=NOW()))
               OR (status IN ('ready','failed') AND next_refresh_at<=NOW())
               OR (status='ready' AND calculation_version<>CASE WHEN league_id IN (2,5,2286) THEN :scoped_version ELSE :version END)
            ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, computed_at NULLS FIRST, next_refresh_at NULLS FIRST, created_at
            FOR UPDATE SKIP LOCKED LIMIT 1'''), {'version': VERSION, 'scoped_version': SCOPED_VERSION}).mappings().first()
        if not row:
            return None
        job = {'league': row['league_id'], 'season': row['season_id'], 'token': str(uuid.uuid4())}
        db.execute(text('''UPDATE league_season_summaries SET status='processing',
            lease_token=CAST(:token AS uuid), lease_expires_at=NOW()+INTERVAL '10 minutes',
            last_checked_at=NOW(), updated_at=NOW(), last_error=NULL
            WHERE league_id=:league AND season_id=:season'''), job)
        return job


def renew(db, job):
    result = db.execute(text('''UPDATE league_season_summaries
        SET lease_expires_at=NOW()+INTERVAL '10 minutes', updated_at=NOW()
        WHERE league_id=:league AND season_id=:season AND lease_token=CAST(:token AS uuid)'''), job)
    if not result.rowcount:
        raise RuntimeError('League summary refresh lease was lost')


def refresh(job):
    now = datetime.now(timezone.utc)
    with requests.Session() as session:
        token = os.getenv('SPORTMONKS_API_KEY')
        if not token:
            raise RuntimeError('League statistics provider is not configured')
        session.headers['Authorization'] = token
        season = provider(session, f"seasons/{job['season']}", include='league')
        if int(season.get('league_id') or 0) != job['league']:
            raise RuntimeError('League and season do not match')
        stages = provider(session, f"schedules/seasons/{job['season']}")
        fixtures = scoped_schedule(stages, job['league'])
        with SessionLocal() as db:
            previous = {r['fixture_id']: dict(r) for r in db.execute(text('''SELECT fixture_id,
                source_fingerprint, fetched_at, kickoff_at, calculation_version, included_in_aggregation, coverage FROM league_match_contributions
                WHERE league_id=:league AND season_id=:season'''), job).mappings()}
        for fid, fixture in sorted(fixtures.items()):
            if _stop.is_set():
                raise RuntimeError('League summary refresh interrupted; it will resume')
            if int(fixture.get('league_id') or 0) != job['league'] or int(fixture.get('season_id') or 0) != job['season']:
                continue
            old = previous.get(fid)
            if old and (old['coverage'] or {}).get('scope') != fixture['_scope']:
                # Scope changes do not change normalized match statistics.
                # Reuse compact contributions rather than refetch every match.
                with SessionLocal.begin() as db:
                    renew(db, job)
                    db.execute(text('''UPDATE league_match_contributions
                        SET coverage=coverage || CAST(:scope AS jsonb),updated_at=NOW() WHERE fixture_id=:id'''),
                        {'id': fid, 'scope': json.dumps({'scope': fixture['_scope']})})
            if fixture.get('state_id') not in COMPLETED_STATES:
                if old:
                    with SessionLocal.begin() as db:
                        renew(db, job)
                        db.execute(text('UPDATE league_match_contributions SET included_in_aggregation=FALSE, updated_at=NOW() WHERE fixture_id=:id'), {'id': fid})
                continue
            played = kickoff(fixture['starting_at'])
            # Recent corrections: 21 days. Reconcile older stats after 28 days.
            # Interrupted initial backfills reuse already fetched contributions.
            recent_due = played >= now - timedelta(days=21) and (not old or old['fetched_at'] < now - timedelta(days=1))
            if old and old['kickoff_at'] == played and old['calculation_version'] == VERSION and not recent_due and old['fetched_at'] >= now - timedelta(days=28):
                if not old['included_in_aggregation']:
                    # Restore a recently fetched qualifier excluded by the previous scope rules.
                    with SessionLocal.begin() as db:
                        renew(db, job)
                        db.execute(text('UPDATE league_match_contributions SET included_in_aggregation=TRUE,updated_at=NOW() WHERE fixture_id=:id'), {'id': fid})
                continue
            detail = provider(session, f'fixtures/{fid}', include='participants;state;statistics.type;lineups.player;lineups.details.type;xGFixture.type')
            contribution = normalize_fixture(detail)
            contribution['coverage']['scope'] = fixture['_scope']
            if contribution['league_id'] != job['league'] or contribution['season_id'] != job['season']:
                raise RuntimeError('Fixture competition changed during refresh')
            with SessionLocal.begin() as db:
                renew(db, job)
                if old and old['source_fingerprint'] == contribution['source_fingerprint'] and old['calculation_version'] == VERSION:
                    db.execute(text('UPDATE league_match_contributions SET fetched_at=NOW(),included_in_aggregation=:included WHERE fixture_id=:id'),
                               {'id': fid, 'included': contribution['included_in_aggregation']})
                    continue
                p = {**contribution, 'version': VERSION}
                for field in ('team_contributions', 'player_contributions', 'coverage'):
                    p[field] = json.dumps(p[field])
                db.execute(text('''INSERT INTO league_match_contributions
                    (fixture_id,league_id,season_id,kickoff_at,home_team_id,away_team_id,included_in_aggregation,
                     team_contributions,player_contributions,coverage,source_fingerprint,calculation_version)
                    VALUES (:fixture_id,:league_id,:season_id,:kickoff_at,:home_team_id,:away_team_id,:included_in_aggregation,
                     CAST(:team_contributions AS jsonb),CAST(:player_contributions AS jsonb),CAST(:coverage AS jsonb),:source_fingerprint,:version)
                    ON CONFLICT (fixture_id) DO UPDATE SET
                     kickoff_at=EXCLUDED.kickoff_at, home_team_id=EXCLUDED.home_team_id,away_team_id=EXCLUDED.away_team_id,
                     included_in_aggregation=EXCLUDED.included_in_aggregation,
                     team_contributions=EXCLUDED.team_contributions,player_contributions=EXCLUDED.player_contributions,
                     coverage=EXCLUDED.coverage,source_fingerprint=EXCLUDED.source_fingerprint,
                     calculation_version=EXCLUDED.calculation_version,fetched_at=NOW(),updated_at=NOW()'''), p)
        # Only retire missing fixtures after the complete schedule was fetched.
        # Publication and aggregation use compact rows, never full match reports.
        with SessionLocal.begin() as db:
            renew(db, job)
            if previous and not stages:
                raise RuntimeError('Season schedule unexpectedly empty')
            db.execute(text('''UPDATE league_match_contributions SET included_in_aggregation=FALSE,updated_at=NOW()
                WHERE league_id=:league AND season_id=:season AND NOT (fixture_id=ANY(CAST(:ids AS bigint[])))'''), {**job, 'ids': list(fixtures)})
        if job['league'] in EUROPEAN_LEAGUES:
            season['_main_team_count'] = len({p['id'] for fixture in fixtures.values() if fixture['_scope'] == 'league'
                                               for p in fixture.get('participants') or [] if p.get('id') and not p.get('placeholder')})
        publish(job, season)


def publish(job, season):
    aggregate = Aggregate()
    scoped = job['league'] in EUROPEAN_LEAGUES
    scope_totals = {key: Aggregate() for key in SCOPES} if scoped else {}
    scope_periods = {key: {} for key in SCOPES} if scoped else {}
    # Stream compact match contributions; never retain full fixture reports.
    # European summaries also retain the small per-scope fortnight accumulators.
    with SessionLocal.begin() as db:
        renew(db, job)
        db.execute(text('SELECT 1 FROM league_season_summaries WHERE league_id=:league AND season_id=:season FOR UPDATE'), job)
        previous_periods = db.execute(text('SELECT period_start FROM league_biweekly_summaries WHERE league_id=:league AND season_id=:season'), job).scalars().all()
        periods_written = set()
        period = None
        fortnight = Aggregate(include_team_metrics=False)
        rows = db.execute(text('''SELECT kickoff_at,included_in_aggregation,team_contributions,player_contributions,coverage
            FROM league_match_contributions WHERE league_id=:league AND season_id=:season
            AND included_in_aggregation=TRUE ORDER BY kickoff_at,fixture_id''').execution_options(stream_results=True, yield_per=20), job).mappings()
        for row in rows:
            row_scope = (row['coverage'] or {}).get('scope')
            if scoped and row_scope not in {'qualifying', 'league', 'knockout'}:
                continue
            key = period_for(row['kickoff_at'].astimezone(PERIOD_ZONE).date())[0]
            if scoped:
                # Qualifiers have their own view; Overall starts at the league phase.
                scope_keys = (row_scope,) if row_scope == 'qualifying' else (row_scope, 'overall')
                for scope_key in scope_keys:
                    scope_totals[scope_key].add(row)
                    scope_periods[scope_key].setdefault(key, Aggregate(include_team_metrics=False)).add(row)
                if row_scope == 'qualifying':
                    continue
            if period is not None and key != period:
                save_period(db, job, period, fortnight.finish())
                periods_written.add(period)
                fortnight = Aggregate(include_team_metrics=False)
            period = key
            aggregate.add(row)
            fortnight.add(row)
        if period is not None:
            save_period(db, job, period, fortnight.finish())
            periods_written.add(period)
        for empty in {*previous_periods, period_for(datetime.now(PERIOD_ZONE).date())[0]} - periods_written:
            save_period(db, job, empty, Aggregate(include_team_metrics=False).finish())
        result = aggregate.finish()
        if scoped:
            stage_summaries = {}
            current_period = period_for(datetime.now(PERIOD_ZONE).date())[0]
            for scope_key in SCOPES:
                total = scope_totals[scope_key].finish()
                total.pop('player_rating_totals', None)
                total.pop('latest_included_kickoff_at', None)
                hydrate_winner_images(db, total)
                periods = []
                scope_periods[scope_key].setdefault(current_period, Aggregate(include_team_metrics=False))
                for start, accumulator in sorted(scope_periods[scope_key].items(), reverse=True):
                    summary = accumulator.finish()
                    hydrate_winner_images(db, summary)
                    periods.append({'period_start': start.isoformat(), 'period_end': (start + timedelta(days=14)).isoformat(),
                                    'status': 'ready', 'computed_at': datetime.now(timezone.utc).isoformat(),
                                    'fixture_count': summary['fixture_count'], 'coverage': summary['coverage'],
                                    'team_best_players': summary['team_best_players'], 'league_best_player': summary['league_best_player']})
                stage_summaries[scope_key] = {'season': total, 'periods': periods}
            result['coverage']['stageSummaries'] = stage_summaries
            if '_main_team_count' in season:
                result['coverage']['mainTeamCount'] = season['_main_team_count']
        hydrate_winner_images(db, result)
        params = {**job, **result, 'version': SCOPED_VERSION if scoped else VERSION, 'season_name': season.get('name'),
                  'league_name': (season.get('league') or {}).get('name'), 'finished': bool(season.get('finished'))}
        for field in ('team_metrics', 'metric_catalog', 'player_rating_totals', 'team_best_players', 'league_best_player', 'coverage'):
            params[field] = json.dumps(params[field])
        db.execute(text('''UPDATE league_season_summaries SET team_metrics=CAST(:team_metrics AS jsonb),
            metric_catalog=CAST(:metric_catalog AS jsonb),player_rating_totals=CAST(:player_rating_totals AS jsonb),
            team_best_players=CAST(:team_best_players AS jsonb),league_best_player=NULLIF(CAST(:league_best_player AS jsonb),'null'::jsonb),
            coverage=CAST(:coverage AS jsonb),fixture_count=:fixture_count,latest_included_kickoff_at=:latest_included_kickoff_at,
            league_name=:league_name,season_name=:season_name,calculation_version=:version,status='ready',
            computed_at=NOW(),updated_at=NOW(),next_refresh_at=NOW()+CASE WHEN :finished THEN INTERVAL '28 days' ELSE INTERVAL '7 days' END,
            lease_token=NULL,lease_expires_at=NULL,last_error=NULL
            WHERE league_id=:league AND season_id=:season AND lease_token=CAST(:token AS uuid)'''), params)


def hydrate_winner_images(db, result):
    winners = result['team_best_players']
    players = [*winners.values()]
    if result.get('league_best_player'):
        players.append(result['league_best_player'])
    ids = list({p['playerId'] for p in players})
    if not ids:
        return
    images = dict(db.execute(text("SELECT player_id,image_url FROM enterprise_player_images WHERE player_id=ANY(CAST(:ids AS bigint[])) AND image_url IS NOT NULL AND image_status='available'"), {'ids': ids}).all())
    for player in players:
        player['imageUrl'] = images.get(player['playerId']) or player.get('imageUrl')


def save_period(db, job, start, result):
    hydrate_winner_images(db, result)
    p = {**job, 'start': start, 'end': start + timedelta(days=14), 'version': VERSION,
         'teams': json.dumps(result['team_best_players']), 'best': json.dumps(result['league_best_player']),
         'coverage': json.dumps(result['coverage']), 'count': result['fixture_count']}
    db.execute(text('''INSERT INTO league_biweekly_summaries
        (league_id,season_id,period_start,period_end,team_best_players,league_best_player,coverage,fixture_count,calculation_version,status,computed_at,last_checked_at)
        VALUES (:league,:season,:start,:end,CAST(:teams AS jsonb),NULLIF(CAST(:best AS jsonb),'null'::jsonb),CAST(:coverage AS jsonb),:count,:version,'ready',NOW(),NOW())
        ON CONFLICT (league_id,season_id,period_start) DO UPDATE SET
        team_best_players=EXCLUDED.team_best_players,league_best_player=EXCLUDED.league_best_player,
        coverage=EXCLUDED.coverage,fixture_count=EXCLUDED.fixture_count,calculation_version=EXCLUDED.calculation_version,
        status='ready',computed_at=NOW(),last_checked_at=NOW(),updated_at=NOW(),last_error=NULL'''), p)


def fail_job(job, exc):
    with SessionLocal.begin() as db:
        db.execute(text('''UPDATE league_season_summaries SET status='failed',
            last_error=:error,next_refresh_at=NOW()+INTERVAL '15 minutes',updated_at=NOW(),
            lease_token=NULL,lease_expires_at=NULL
            WHERE league_id=:league AND season_id=:season AND lease_token=CAST(:token AS uuid)'''),
            {**job, 'error': str(exc)[:240] if isinstance(exc, RuntimeError) else 'League summary refresh failed'})


def _run():
    while not _stop.is_set():
        try:
            job = claim_job()
            if job:
                try:
                    refresh(job)
                except Exception as exc:
                    fail_job(job, exc)
                    log.warning('League summary refresh failed for league=%s season=%s (%s)', job['league'], job['season'], type(exc).__name__)
                continue
        except Exception as exc:
            log.warning('League summary worker unavailable (%s)', type(exc).__name__)
        # Retry queued work promptly after another worker's lease expires.
        # Otherwise weekly refreshes only need twice-daily scheduling checks.
        delay = 12 * 60 * 60
        try:
            with SessionLocal() as db:
                if db.execute(text("SELECT EXISTS (SELECT 1 FROM league_season_summaries WHERE status IN ('pending','processing'))")).scalar():
                    delay = 60
        except Exception:
            delay = 60
        _wake.wait(delay)
        _wake.clear()


def start_worker():
    global _thread
    if os.getenv('LEAGUE_INSIGHTS_WORKER_ENABLED', '1') == '0':
        return
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_run, daemon=True, name='league-insights')
        _thread.start()


def stop_worker():
    _stop.set()
    _wake.set()


def player_profile(db, player_id):
    row = db.execute(text("""SELECT id, metadata AS content FROM player_data
        WHERE (CASE WHEN (metadata->>'player_id') ~ '^[0-9]+([.]0+)?$'
          THEN trunc((metadata->>'player_id')::numeric)::text ELSE NULL END)=:pid
        ORDER BY id DESC LIMIT 1"""), {'pid': str(player_id)}).mappings().first()
    if row is None:
        return None
    content = dict(row['content'] or {})
    image = db.execute(text("""SELECT image_url FROM enterprise_player_images
        WHERE player_id=:pid AND image_status='available'"""), {'pid': player_id}).scalar()
    if image:
        content['image_url'] = image
    for entity in ('team', 'league'):
        entity_id = content.get(f'{entity}_id')
        if str(entity_id or '').replace('.0', '').isdigit():
            image = db.execute(text(f"SELECT image_url FROM enterprise_{entity}_images WHERE {entity}_id=:id AND image_status='available'"), {'id': int(float(entity_id))}).scalar()
            if image:
                content[f'{entity}_image_url'] = image
    return {'id': row['id'], 'content': content}


def enqueue_season(db, standings):
    inserted = db.execute(text("""INSERT INTO league_season_summaries (league_id,season_id,season_name)
        VALUES (:league,:season,:name) ON CONFLICT DO NOTHING"""),
        {'league': standings['leagueId'], 'season': standings['seasonId'], 'name': standings.get('seasonName')}).rowcount
    if standings.get('stageScoped'):
        team_count = len({row['teamId'] for table in standings['tables'] for row in table['rows'] if row.get('teamId')})
        if team_count:
            db.execute(text('''UPDATE league_season_summaries SET coverage=coverage || CAST(:metadata AS jsonb)
                WHERE league_id=:league AND season_id=:season'''),
                {'league': standings['leagueId'], 'season': standings['seasonId'], 'metadata': json.dumps({'mainTeamCount': team_count})})
    stale = db.execute(text('''UPDATE league_season_summaries SET status='pending', next_refresh_at=NOW()
        WHERE league_id=:league AND season_id=:season AND calculation_version<>:version
        AND status NOT IN ('pending','processing')'''),
        {'league': standings['leagueId'], 'season': standings['seasonId'], 'version': SCOPED_VERSION if standings['leagueId'] in EUROPEAN_LEAGUES else VERSION}).rowcount
    queued = db.execute(text('SELECT status FROM league_season_summaries WHERE league_id=:league AND season_id=:season'),
        {'league': standings['leagueId'], 'season': standings['seasonId']}).scalar() == 'pending'
    db.commit()
    if inserted or stale or queued:
        _wake.set()
