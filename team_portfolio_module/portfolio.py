"""User-owned saved team snapshots and independently persisted analysis selections."""
import hashlib
import json
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from api_module.database import get_db
from team_pool_module.models import TeamPoolSearchRow
from api_module.utilities import require_auth

router = APIRouter()


class PlayedMatchSnapshot(BaseModel):
    fixtureId: int
    name: str
    startingAt: str
    country: str
    league: str
    homeTeam: str
    awayTeam: str
    homeTeamId: int | None = None
    awayTeamId: int | None = None
    homeScore: int | None = None
    awayScore: int | None = None
    thisSeason: bool


class FavoriteTeamIn(BaseModel):
    team: TeamPoolSearchRow


def execute(db, query, params=None):
    try:
        return db.execute(text(query), params or {})
    except ProgrammingError as exc:
        if getattr(exc.orig, 'pgcode', None) == '42P01':
            db.rollback()
            raise HTTPException(503, 'Team Portfolio is not available yet. Apply sql/favorite_teams.sql to the mobile database.') from None
        raise


def team_id(team):
    try:
        value = int(team.id)
    except (TypeError, ValueError):
        raise HTTPException(422, 'Invalid team ID') from None
    if value <= 0 or value > 9223372036854775807 or not team.name.strip():
        raise HTTPException(422, 'Invalid team')
    return value


def favorite_out(row):
    snapshot = dict(row['team_payload'])
    if row.get('stored_logo'):
        snapshot['logoUrl'] = row['stored_logo']
    return {'favoriteId': str(row['id']), 'team': snapshot,
            'createdAt': row['created_at'], 'updatedAt': row['updated_at'],
            'latestReportId': str(row['latest_report_id']) if row.get('latest_report_id') else None}


def save_team(db, user_id, team):
    tid = team_id(team)
    snapshot = team.model_dump()
    snapshot['id'] = str(tid)
    row = execute(db, '''INSERT INTO favorite_teams
        (user_id,team_id,team_name,country_name,league_id,league_name,team_payload)
        VALUES (:user_id,:team_id,:name,:country,:league_id,:league,CAST(:payload AS jsonb))
        ON CONFLICT (user_id,team_id) DO UPDATE SET team_name=EXCLUDED.team_name,
          country_name=EXCLUDED.country_name,league_id=EXCLUDED.league_id,
          league_name=EXCLUDED.league_name,team_payload=EXCLUDED.team_payload,updated_at=NOW()
        RETURNING id,team_payload,created_at,updated_at''',
        {'user_id': user_id, 'team_id': tid, 'name': team.name, 'country': team.country,
         'league_id': team.leagueId, 'league': team.league, 'payload': json.dumps(snapshot)}).mappings().one()
    return favorite_out(row)


@router.get('/favorite-teams')
def list_teams(user_id: int = Depends(require_auth), db=Depends(get_db)):
    rows = execute(db, '''SELECT f.id,f.team_payload,f.created_at,f.updated_at, r.id AS latest_report_id, image.image_url AS stored_logo
        FROM favorite_teams f
        LEFT JOIN enterprise_team_images image ON image.team_id=f.team_id AND image.image_status='available'
        LEFT JOIN LATERAL (SELECT id FROM team_reports
            WHERE user_id=f.user_id AND team_id=f.team_id AND report_status='ready'
              AND report_content IS NOT NULL
            ORDER BY report_ready_at DESC,id LIMIT 1) r ON TRUE
        WHERE f.user_id=:user_id ORDER BY f.created_at DESC,f.id''', {'user_id': user_id}).mappings().all()
    return [favorite_out(row) for row in rows]


@router.post('/favorite-teams')
def create_team(payload: FavoriteTeamIn, user_id: int = Depends(require_auth), db=Depends(get_db)):
    result = save_team(db, user_id, payload.team)
    db.commit()
    return result


@router.delete('/favorite-teams/{favorite_id}')
def delete_team(favorite_id: UUID, user_id: int = Depends(require_auth), db=Depends(get_db)):
    deleted = execute(db, 'DELETE FROM favorite_teams WHERE id=:id AND user_id=:user_id',
                      {'id': str(favorite_id), 'user_id': user_id}).rowcount
    if not deleted:
        raise HTTPException(404, 'Saved team not found')
    db.commit()
    return {'ok': True}


def report_out(row, include_content=False):
    result = {'id': str(row['id']), 'teamId': row['team_id'], 'team': row['team_payload'],
              'fixtureIds': row['fixture_ids'], 'matches': row['matches_payload'],
              'language': row['language'], 'status': row['report_status'],
              'createdAt': row['created_at'], 'updatedAt': row['updated_at'], 'readyAt': row['report_ready_at']}
    if include_content:
        result['content'] = public_content(row['report_content'])
    return result


REPORT_COLUMNS = 'id,team_id,team_payload,fixture_ids,matches_payload,language,report_status,created_at,updated_at,report_ready_at'


@router.get('/team-analysis/reports')
def list_reports(team_id: int = Query(gt=0), limit: int = Query(default=50, ge=1, le=100), user_id: int = Depends(require_auth), db=Depends(get_db)):
    rows = execute(db, f'''SELECT {REPORT_COLUMNS} FROM team_reports
        WHERE user_id=:user_id AND team_id=:team_id AND report_status='ready'
        ORDER BY report_ready_at DESC,id LIMIT :limit''', {'user_id': user_id, 'team_id': team_id, 'limit': limit}).mappings().all()
    return [report_out(row) for row in rows]


@router.get('/team-analysis/reports/{report_id}')
def get_report(report_id: UUID, user_id: int = Depends(require_auth), db=Depends(get_db)):
    row = execute(db, f'''SELECT {REPORT_COLUMNS},report_content FROM team_reports
        WHERE id=:id AND user_id=:user_id AND report_status='ready' AND report_content IS NOT NULL ''',
        {'id': str(report_id), 'user_id': user_id}).mappings().first()
    if row is None:
        raise HTTPException(404, 'Team report not found')
    return report_out(row, True)


@router.get('/dashboard/team-reports')
def dashboard_reports(limit: int = Query(default=10, ge=1, le=50), offset: int = Query(default=0, ge=0), user_id: int = Depends(require_auth), db=Depends(get_db)):
    rows = execute(db, f'''SELECT {REPORT_COLUMNS} FROM team_reports
        WHERE user_id=:user_id AND report_status='ready'
        ORDER BY report_ready_at DESC,id LIMIT :limit OFFSET :offset''', {'user_id': user_id, 'limit': limit, 'offset': offset}).mappings().all()
    return [report_out(row) for row in rows]


@router.get('/dashboard/portfolio-counts')
def dashboard_counts(user_id: int = Depends(require_auth), db=Depends(get_db)):
    row = execute(db, '''SELECT
        (SELECT COUNT(*) FROM favorite_teams WHERE user_id=:user_id) AS teams,
        (SELECT COUNT(*) FROM team_reports WHERE user_id=:user_id AND report_status='ready') AS team_reports,
        (SELECT COUNT(*) FROM enterprise_favorite_matches WHERE user_id=:user_id
          AND report_type='pre_match' AND report_status='ready' AND report_content IS NOT NULL) AS pre_match_reports
        ''', {'user_id': user_id}).mappings().one()
    return {'portfolioTeams': row['teams'], 'readyTeamReports': row['team_reports'], 'readyPreMatchReports': row['pre_match_reports']}


def begin_report(db, user_id, payload, lang, version):
    ids = sorted(set(payload.fixtureIds))
    if any(fid <= 0 for fid in ids) or team_id(payload.team) != payload.teamId:
        raise HTTPException(422, 'Invalid team or match selection')
    if len(payload.matches) != len(ids) or {m.fixtureId for m in payload.matches} != set(ids):
        raise HTTPException(422, 'Match snapshots must match the selected fixtures')
    if any(payload.teamId not in (m.homeTeamId, m.awayTeamId) for m in payload.matches):
        raise HTTPException(422, 'Every selected match must include this team')
    selection_key = hashlib.sha256(','.join(map(str, ids)).encode()).hexdigest()
    save_team(db, user_id, payload.team)
    params = {'user_id': user_id, 'team_id': payload.teamId, 'ids': ids,
              'key': selection_key, 'lang': lang, 'version': version,
              'team': payload.team.model_dump_json(),
              'matches': json.dumps([m.model_dump() for m in payload.matches]), 'token': str(uuid.uuid4())}
    row = execute(db, '''INSERT INTO team_reports
        (user_id,team_id,fixture_ids,selection_key,language,version,team_payload,matches_payload,generation_token)
        VALUES (:user_id,:team_id,:ids,:key,:lang,:version,CAST(:team AS jsonb),CAST(:matches AS jsonb),CAST(:token AS uuid))
        ON CONFLICT (user_id,team_id,selection_key,language,version) DO UPDATE SET
          report_status='processing',report_error=NULL,updated_at=NOW(),generation_token=EXCLUDED.generation_token,
          team_payload=EXCLUDED.team_payload,matches_payload=EXCLUDED.matches_payload
        WHERE team_reports.report_status='failed'
           OR (team_reports.report_status='processing' AND team_reports.updated_at<NOW()-INTERVAL '30 minutes')
        RETURNING id''', params).mappings().first()
    if row:
        db.commit()
        return {'id': str(row['id']), 'token': params['token'], 'content': None}
    existing = execute(db, '''SELECT id,report_status,report_content FROM team_reports
        WHERE user_id=:user_id AND team_id=:team_id AND selection_key=:key AND language=:lang AND version=:version''', params).mappings().one()
    db.commit()
    if existing['report_status'] == 'ready':
        return {'id': str(existing['id']), 'content': public_content(existing['report_content'])}
    raise HTTPException(409, 'Seçtiğiniz maçlar zaten analiz ediliyor. Lütfen kısa bir süre sonra tekrar deneyin.'
                        if lang == 'tr' else 'This match selection is already being analyzed. Please try again shortly.')


def finish_report(db, user_id, job, content=None):
    row = execute(db, '''UPDATE team_reports SET report_status=:status,
        report_content=COALESCE(CAST(:content AS jsonb),report_content),report_error=:error,
        report_ready_at=CASE WHEN :status='ready' THEN NOW() ELSE NULL END,
        updated_at=NOW(),generation_token=NULL
        WHERE id=:id AND user_id=:user_id AND generation_token=CAST(:token AS uuid)
        RETURNING id''', {'id': job['id'], 'token': job['token'], 'user_id': user_id,
                         'status': 'ready' if content is not None else 'failed',
                         'content': json.dumps(content) if content is not None else None,
                         'error': None if content is not None else 'Team report generation failed.'}).first()
    db.commit()
    if content is not None and row is None:
        raise HTTPException(409, 'This report was removed or its generation was superseded.')


def public_content(content):
    # Paid lazy-section caches are not part of the public base report response.
    return {key: value for key, value in (content or {}).items() if key != '_profiles'}


def cached_profile(db, user_id, report_id, key):
    return execute(db, """SELECT report_content->'_profiles'->:key FROM team_reports
        WHERE id=:id AND user_id=:user_id AND report_status='ready'""",
        {'id': str(report_id), 'user_id': user_id, 'key': key}).scalar()


def save_profile(db, user_id, report_id, key, content):
    execute(db, """UPDATE team_reports SET report_content=jsonb_set(report_content,
        '{_profiles}', COALESCE(report_content->'_profiles','{}'::jsonb) ||
        jsonb_build_object(CAST(:key AS text),CAST(:content AS jsonb)),true), updated_at=NOW()
        WHERE id=:id AND user_id=:user_id AND report_status='ready'""",
        {'id': str(report_id), 'user_id': user_id, 'key': key, 'content': json.dumps(content)})
    db.commit()
