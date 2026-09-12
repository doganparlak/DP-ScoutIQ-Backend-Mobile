"""Durable mobile post-match reports; each completed section is checkpointed."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from sqlalchemy import text
from api_module.database import engine
from team_analysis_module.report import generate_match_report
from .team_analysis import build_team_analysis
from .player_perspectives import build_player_perspectives

VERSION = 2
_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mobile-match-report")
_PENDING = set()
_MUTEX = threading.Lock()
SECTIONS = ('data', 'team_analysis', 'player_perspectives')

def now():
    return datetime.now(timezone.utc).isoformat()

def compatible(content, language):
    return content.get('format') == 'mobile_post_match' and content.get('version') == VERSION and content.get('language') == language and isinstance(content.get('sections'), dict)

def read_report(favorite_id, user_id):
    with engine.connect() as db:
        row = db.execute(text('SELECT id, fixture_id, report_status, report_content FROM favorite_matches WHERE id=:id AND user_id=:uid AND report_type=\'post_match\''), {'id':favorite_id,'uid':user_id}).mappings().first()
        return dict(row) if row else None

def public_report(row, language):
    content = row.get('report_content') or {}
    if not compatible(content, language):
        return {'favoriteId':str(row['id']), 'status':'processing', 'content':None}
    # Internal generation evidence is never needed by the client.
    with _MUTEX:
        pending = str(row['id']) in _PENDING
    status = 'processing' if pending and row['report_status'] != 'ready' else row['report_status']
    return {'favoriteId':str(row['id']), 'status':status, 'content':content}

def save(favorite_id, user_id, content, status):
    with engine.begin() as db:
        result = db.execute(text("""UPDATE favorite_matches SET report_content=CAST(:content AS jsonb), report_status=:status,
            report_error=:error, report_ready_at=CASE WHEN :status='ready' THEN NOW() ELSE NULL END, updated_at=NOW()
            WHERE id=:id AND user_id=:uid AND report_type='post_match'"""),
            {'id':favorite_id,'uid':user_id,'content':json.dumps(content,ensure_ascii=False,default=str),'status':status,
             'error':'A report section could not be generated. Retry to resume.' if status=='failed' else None})
        return result.rowcount == 1

def clean_data(report):
    data = dict(report)
    data['lineups'] = [dict(p) for p in report.get('lineups') or []]
    data['teams'] = [{**t, 'possession':next((m.get('value') for m in (t.get('categories') or {}).get('contribution_impact',[]) if m.get('name')=='Ball Possession %'),None)} for t in report.get('teams') or []]
    return data

def schedule(favorite_id, user_id, language, retry_failed=False):
    key = str(favorite_id)
    with _MUTEX:
        if key in _PENDING:
            return
        _PENDING.add(key)
    try:
        _EXECUTOR.submit(run, key, user_id, language, retry_failed)
    except Exception:
        with _MUTEX:
            _PENDING.discard(key)
        raise

def run(favorite_id, user_id, language, retry_failed):
    try:
        # Held transaction keeps ownership across processes, including pooled DB connections.
        # A crash releases it, so the next open/poll can resume without guessing a timeout.
        with engine.begin() as owner:
            locked = owner.execute(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))'), {'key':'mobile-match-report:'+favorite_id}).scalar()
            if not locked:
                return
            row = read_report(favorite_id,user_id)
            if not row:
                return
            content = row.get('report_content') or {}
            if compatible(content,language) and row['report_status']=='ready':
                return
            if compatible(content,language) and row['report_status']=='failed' and not retry_failed:
                return
            if not compatible(content,language):
                content = {'format':'mobile_post_match','version':VERSION,'language':language,'sections':{k:{'status':'pending'} for k in SECTIONS}}
            for section in SECTIONS:
                state = content['sections'].get(section, {})
                if state.get('status')=='ready' and section in content:
                    continue
                content['sections'][section] = {'status':'processing','started_at':now()}
                if not save(favorite_id,user_id,content,'processing'):
                    return
                try:
                    if section=='data':
                        report = generate_match_report(int(row['fixture_id']),lang=language,build_narratives=False)
                        from .router import _is_completed_enterprise_fixture
                        if not _is_completed_enterprise_fixture({'state':report.get('state') or {}}):
                            raise ValueError('Match is not completed')
                        value = clean_data(report)
                    elif section=='team_analysis':
                        value = {'teams':build_team_analysis(content['data'],language)}
                    else:
                        data=content['data']
                        value = {'teams':build_player_perspectives(data.get('teams') or [],data.get('lineups') or [],data.get('events') or [],language, strict=True)}
                    content[section] = value
                    content['sections'][section] = {'status':'ready','ready_at':now()}
                except Exception:
                    content['sections'][section] = {'status':'failed','failed_at':now()}
                    # If match data failed there is no evidence for either analysis.
                    if section=='data':
                        save(favorite_id,user_id,content,'failed')
                        return
                if not save(favorite_id,user_id,content,'processing'):
                    return
            status='ready' if all(content['sections'][s]['status']=='ready' for s in SECTIONS) else 'failed'
            save(favorite_id,user_id,content,status)
    finally:
        with _MUTEX:
            _PENDING.discard(favorite_id)
