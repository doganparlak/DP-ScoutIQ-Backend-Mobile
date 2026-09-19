"""Durable mobile pre-match reports; each completed section is checkpointed."""
from api_module.report_access import user_report_tier, scope_satisfies
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from sqlalchemy import text
from api_module.database import engine
from .pre_match_card import build_pre_match_card
from .pre_match_usage import _pre_match_player_perspectives, _pre_match_momentum_perspectives, _pre_match_team_analysis, sanitize_pre_match_standout_metrics
from .pre_match_standings import get_league_standings

VERSION = 4
_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mobile-match-report")
_PENDING = set()
_LAZY_PENDING = set()
_MUTEX = threading.Lock()
SECTIONS = ('data', 'squad', 'form', 'players', 'momentum', 'team_analysis')
FOUNDATION_SECTIONS = ('data','squad','form')
AI_SECTIONS = ('players','momentum','team_analysis')

def now():
    return datetime.now(timezone.utc).isoformat()

def compatible(content, language):
    return content.get('format') == 'mobile_pre_match' and content.get('version') == VERSION and content.get('language') == language and isinstance(content.get('sections'), dict)

def section_ready(content, section, tier='paid'):
    state = (content.get('sections') or {}).get(section, {})
    return (state.get('status') == 'ready' and section in content
            and (section not in AI_SECTIONS or scope_satisfies(state, tier)))

def complete(content, tier='paid'):
    return all(section_ready(content, section, tier) for section in SECTIONS)

def read_report(favorite_id, user_id):
    with engine.connect() as db:
        row = db.execute(text('SELECT id, fixture_id, report_status, report_content FROM favorite_matches WHERE id=:id AND user_id=:uid AND report_type=\'pre_match\''), {'id':favorite_id,'uid':user_id}).mappings().first()
        return dict(row) if row else None

def public_report(row, language):
    content = row.get('report_content') or {}
    if not compatible(content, language):
        return {'favoriteId':str(row['id']), 'status':'processing', 'content':None}
    with _MUTEX:
        pending = str(row['id']) in _PENDING
    status = 'processing' if pending and row['report_status'] != 'ready' else row['report_status']
    return {'favoriteId':str(row['id']), 'status':status, 'content':sanitize_pre_match_standout_metrics(content)}

def save(favorite_id, user_id, content, status):
    with engine.begin() as db:
        result = db.execute(text("""UPDATE favorite_matches SET report_content=CAST(:content AS jsonb), report_status=:status,
            report_error=:error, report_ready_at=CASE WHEN :status='ready' THEN NOW() ELSE NULL END, updated_at=NOW()
            WHERE id=:id AND user_id=:uid AND report_type='pre_match'"""),
            {'id':favorite_id,'uid':user_id,'content':json.dumps(content,ensure_ascii=False,default=str),'status':status,
             'error':'A report section could not be generated. Retry to resume.' if status=='failed' else None})
        return result.rowcount == 1

def build_section(section, content, fixture_id, user_id, language, tier=None):
    tier = tier or user_report_tier(user_id)
    paid = tier == 'paid'
    from .router import pre_match_squad, _is_not_started_enterprise_fixture
    if section == 'data':
        card = build_pre_match_card(fixture_id)
        if not _is_not_started_enterprise_fixture({'state':card.get('state') or {}}):
            raise ValueError('A new pre-match snapshot requires a match that has not started')
        return card
    if section in {'squad','form'}:
        from .fixtures import get_fixture
        if not _is_not_started_enterprise_fixture(get_fixture(fixture_id) or {}):
            raise ValueError('Pre-match data cannot be refreshed after kickoff')
    if section == 'squad':
        # The historical window is bounded by kickoff; resume reuses the saved snapshot.
        result = pre_match_squad(fixture_id, user_id)
        if len(result.get("teams") or []) != 2:
            raise ValueError("Both teams are required for the report")
        return result
    usages = content['squad']['teams']
    teams = [{'id':u['team_id'],'name':u.get('team_name'),'location':u.get('location')} for u in usages]
    if section == 'form':
        standings = get_league_standings(int((content['data'].get('league') or {}).get('id') or 0))
        by_team = {}
        for table in standings.get('tables') or []:
            for row in table.get('rows') or []:
                key = str(row.get('teamId'))
                if key not in by_team:
                    by_team[key] = {'position':row.get('position'),'points':row.get('points'),'table_label':table.get('label') or ''}
        return {'teams':usages,'standings':by_team}
    if section == 'players':
        return {'teams':usages,'perspectives':(_pre_match_player_perspectives(usages,language,strict=True) if paid else {})}
    if section == 'momentum':
        has_data = any(r.get('average_net_pressure') or r.get('goals_for') or r.get('goals_against') for u in usages for r in u.get('momentum') or [])
        return {'teams':usages,'perspective':_pre_match_momentum_perspectives(usages,teams,language,strict=True) if has_data and paid else None}
    return {'teams':_pre_match_team_analysis(usages,teams,language,strict=True,include_locked=paid)}

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
            tier = user_report_tier(user_id)
            content = row.get('report_content') or {}
            if compatible(content,language) and row['report_status']=='ready' and complete(content,tier):
                return
            if compatible(content,language) and row['report_status']=='failed' and not retry_failed:
                return
            if not compatible(content,language):
                content = {'format':'mobile_pre_match','version':VERSION,'language':language,'sections':{k:{'status':'pending'} for k in SECTIONS}}
            for section in SECTIONS:
                state = content['sections'].get(section, {})
                if section_ready(content, section, tier):
                    continue
                content['sections'][section] = {**content['sections'].get(section, {}), 'status':'processing','started_at':now()}
                if not save(favorite_id,user_id,content,'processing'):
                    return
                try:
                    value = build_section(section,content,int(row['fixture_id']),user_id,language,tier)
                    content[section] = value
                    content['sections'][section] = {'status':'ready','ready_at':now(),'access_tier':tier}
                except Exception:
                    content['sections'][section] = {**content['sections'].get(section, {}), 'status':'failed','failed_at':now()}
                    # If match data failed there is no evidence for either analysis.
                    if section in {'data','squad'}:
                        save(favorite_id,user_id,content,'failed')
                        return
                if not save(favorite_id,user_id,content,'processing'):
                    return
            status='ready' if all(content['sections'][s]['status']=='ready' for s in SECTIONS) else 'failed'
            save(favorite_id,user_id,content,status)
    finally:
        with _MUTEX:
            _PENDING.discard(favorite_id)

def schedule_lazy(favorite_id, user_id, language, section='foundation'):
    if section != 'foundation' and section not in AI_SECTIONS:
        raise ValueError('Unknown report section')
    key = f'{favorite_id}:{section}'
    with _MUTEX:
        if key in _LAZY_PENDING:
            return
        _LAZY_PENDING.add(key)
    try:
        _EXECUTOR.submit(run_lazy,str(favorite_id),user_id,language,section,key)
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
            tier = user_report_tier(user_id)
            content = row.get('report_content') or {}
            if not compatible(content,language):
                content = {'format':'mobile_pre_match','version':VERSION,'language':language,'generation_mode':'lazy_sections','sections':{k:{'status':'pending'} for k in SECTIONS}}
            targets = list(FOUNDATION_SECTIONS)
            if requested != 'foundation':
                targets.append(requested)
            for section in targets:
                if section_ready(content, section, tier):
                    continue
                content['sections'][section] = {**content['sections'].get(section, {}), 'status':'processing','started_at':now()}
                foundation_ready = all(content['sections'].get(key,{}).get('status') == 'ready' and key in content for key in FOUNDATION_SECTIONS)
                if not save(favorite_id,user_id,content,'ready' if foundation_ready else 'processing'):
                    return
                try:
                    content[section] = build_section(section,content,int(row['fixture_id']),user_id,language,tier)
                    content['sections'][section] = {'status':'ready','ready_at':now(),'access_tier':tier}
                    foundation_ready = all(content['sections'].get(key,{}).get('status') == 'ready' and key in content for key in FOUNDATION_SECTIONS)
                    save(favorite_id,user_id,content,'ready' if foundation_ready else 'processing')
                except Exception:
                    content['sections'][section] = {**content['sections'].get(section, {}), 'status':'failed','failed_at':now()}
                    save(favorite_id,user_id,content,'failed' if section in FOUNDATION_SECTIONS else 'ready')
                    return
    finally:
        with _MUTEX:
            _LAZY_PENDING.discard(pending_key)
