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
TEAM_ANALYSIS_VERSION = 2
_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mobile-match-report")
_PENDING = set()
_LAZY_PENDING = set()
_MUTEX = threading.Lock()
SECTIONS = ('data', 'team_analysis', 'player_perspectives')
AI_SECTIONS = ('team_analysis', 'player_perspectives')

def now():
    return datetime.now(timezone.utc).isoformat()

def compatible(content, language):
    return content.get('format') == 'mobile_post_match' and content.get('version') == VERSION and content.get('language') == language and isinstance(content.get('sections'), dict)

def section_ready(content, section):
    ready = (content.get('sections') or {}).get(section,{}).get('status') == 'ready' and section in content
    if section == 'team_analysis':
        return ready and (content.get(section) or {}).get('analysis_version') == TEAM_ANALYSIS_VERSION
    return ready

def complete(content):
    return all(section_ready(content, section) for section in SECTIONS)

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
            if compatible(content,language) and row['report_status']=='ready' and complete(content):
                return
            if compatible(content,language) and row['report_status']=='failed' and not retry_failed:
                return
            if not compatible(content,language):
                content = {'format':'mobile_post_match','version':VERSION,'language':language,'sections':{k:{'status':'pending'} for k in SECTIONS}}
            for section in SECTIONS:
                state = content['sections'].get(section, {})
                if section_ready(content, section):
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
                        value = {'teams':build_team_analysis(content['data'],language),'analysis_version':TEAM_ANALYSIS_VERSION}
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

def schedule_lazy(favorite_id, user_id, language, section='data'):
    if section not in SECTIONS:
        raise ValueError('Unknown report section')
    key = f'{favorite_id}:{section}'
    with _MUTEX:
        if key in _LAZY_PENDING:
            return
        _LAZY_PENDING.add(key)
    try:
        _EXECUTOR.submit(run_lazy, str(favorite_id), user_id, language, section, key)
    except Exception:
        with _MUTEX:
            _LAZY_PENDING.discard(key)
        raise

def run_lazy(favorite_id, user_id, language, requested, pending_key):
    try:
        with engine.begin() as owner:
            owner.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key':'mobile-match-report:'+favorite_id})
            row = read_report(favorite_id,user_id)
            if not row:
                return
            content = row.get('report_content') or {}
            if not compatible(content,language):
                content = {'format':'mobile_post_match','version':VERSION,'language':language,'generation_mode':'lazy_sections','sections':{k:{'status':'pending'} for k in SECTIONS}}
            targets = ['data'] if requested == 'data' else ['data',requested]
            for section in targets:
                if section_ready(content, section):
                    continue
                content['sections'][section] = {'status':'processing','started_at':now()}
                if not save(favorite_id,user_id,content,'processing' if section == 'data' else 'ready'):
                    return
                try:
                    if section == 'data':
                        report = generate_match_report(int(row['fixture_id']),lang=language,build_narratives=False)
                        from .router import _is_completed_enterprise_fixture
                        if not _is_completed_enterprise_fixture({'state':report.get('state') or {}}):
                            raise ValueError('Match is not completed')
                        content[section] = clean_data(report)
                    elif section == 'team_analysis':
                        content[section] = {'teams':build_team_analysis(content['data'],language),'analysis_version':TEAM_ANALYSIS_VERSION}
                    else:
                        data = content['data']
                        content[section] = {'teams':build_player_perspectives(data.get('teams') or [],data.get('lineups') or [],data.get('events') or [],language,strict=True)}
                    content['sections'][section] = {'status':'ready','ready_at':now()}
                    save(favorite_id,user_id,content,'ready')
                except Exception:
                    content['sections'][section] = {'status':'failed','failed_at':now()}
                    save(favorite_id,user_id,content,'failed' if section == 'data' else 'ready')
                    return
    finally:
        with _MUTEX:
            _LAZY_PENDING.discard(pending_key)
