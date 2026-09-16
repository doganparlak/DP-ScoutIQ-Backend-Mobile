from api_module.report_access import user_report_tier, report_tier
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError
from api_module.database import get_db
from api_module.utilities import require_auth, get_user_email_by_id
from team_pool_module.models import MatchAnalysisOptionsIn, MatchAnalysisOptionsOut
from .models import *
from .fixtures import get_match_filter_options, resolve_league_id, resolve_team_id, search_fixtures, get_fixture, SportMonksError
import requests
import json
router = APIRouter(tags=["match-pool"])

@router.post("/match-pool/options", response_model=MatchAnalysisOptionsOut)
def options(payload: MatchAnalysisOptionsIn, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    return get_match_filter_options(db, payload.country, payload.league)

@router.post("/match-pool/search", response_model=MatchAnalysisSearchOut)
def search(payload: MatchAnalysisSearchIn, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    try:
        filters = payload.model_dump(exclude_none=True)
        if not filters.get("leagueId"):
            filters["leagueId"] = resolve_league_id(db, payload.league, payload.country)
        filters["homeTeamId"] = resolve_team_id(db, payload.homeTeam)
        filters["awayTeamId"] = resolve_team_id(db, payload.awayTeam)
        return search_fixtures(filters)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (SportMonksError, requests.RequestException):
        raise HTTPException(502, "Fixture data service is temporarily unavailable") from None

def schema_error(db, exc):
    db.rollback()
    if getattr(exc.orig, "pgcode", None) == "42P01":
        raise HTTPException(503, "Saved matches are not ready. Apply favorite_matches.sql to the mobile database.") from None
    raise exc

@router.get("/favorite-matches", response_model=list[EnterpriseFavoriteMatchOut])
def favorites(user_id=Depends(require_auth), db: Session=Depends(get_db)):
    try:
        rows=db.execute(text("SELECT id, fixture_id, fixture_payload, report_type, report_status, created_at FROM favorite_matches WHERE user_id=:uid ORDER BY starting_at DESC, created_at DESC"), {"uid":user_id}).mappings().all()
        def refresh(fixture_id):
            try:
                return get_fixture(fixture_id)
            except (SportMonksError, requests.RequestException, ValueError):
                return None
        from concurrent.futures import ThreadPoolExecutor
        ids=list({int(r["fixture_id"]) for r in rows})
        current={}
        if ids:
            with ThreadPoolExecutor(max_workers=min(6,len(ids))) as executor:
                current=dict(zip(ids,executor.map(refresh,ids)))
        return [dict(favoriteId=str(r["id"]),fixture=current.get(int(r["fixture_id"])) or r["fixture_payload"],reportType=r["report_type"],reportStatus=r["report_status"],createdAt=r["created_at"]) for r in rows]
    except ProgrammingError as exc:
        schema_error(db, exc)

@router.delete("/favorite-matches/fixtures/{fixture_id}")
def delete_fixture(fixture_id: int, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    # A portfolio row represents a fixture, including both saved report types.
    try:
        db.execute(text("DELETE FROM favorite_matches WHERE user_id=:uid AND fixture_id=:fid"), {"uid":user_id,"fid":fixture_id})
        db.commit()
        return {"ok":True}
    except ProgrammingError as exc:
        schema_error(db, exc)

@router.get("/favorite-matches/{favorite_id}/report")
def existing_report(favorite_id: str, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    from uuid import UUID
    try:
        UUID(favorite_id)
    except ValueError:
        raise HTTPException(400,"Invalid favorite ID") from None
    try:
        row=db.execute(text("SELECT fixture_id, fixture_payload, report_type, report_status, report_content FROM favorite_matches WHERE id=:id AND user_id=:uid"), {"id":favorite_id,"uid":user_id}).mappings().first()
        if not row:
            raise HTTPException(404,"Saved match not found")
        fixture=get_fixture(int(row["fixture_id"])) or row["fixture_payload"]
        completed=_is_completed_enterprise_fixture(fixture)
        allowed=(_is_not_started_enterprise_fixture(fixture) or (completed and row["report_status"]=="ready")) if row["report_type"]=="pre_match" else completed
        if not allowed:
            raise HTTPException(409,"Report is unavailable for the current match status")
        if row["report_status"]!="ready" or not row["report_content"]:
            raise HTTPException(409,"No completed report is available. Report generation is not implemented yet.")
        return {"favoriteId":favorite_id,"reportType":row["report_type"],"status":"ready","content":row["report_content"]}
    except ProgrammingError as exc:
        schema_error(db, exc)
    except (SportMonksError, requests.RequestException):
        raise HTTPException(502,"Fixture data service is temporarily unavailable") from None

@router.post("/favorite-matches", response_model=EnterpriseFavoriteMatchOut)
def save(payload: SaveMatchIn, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    try:
        fixture = get_fixture(payload.fixtureId)
        if not fixture:
            raise HTTPException(404, "Fixture not found")
        report_type = "pre_match" if _is_not_started_enterprise_fixture(fixture) else "post_match"
        return _save_match(EnterpriseFavoriteMatchIn(fixture=fixture, reportType=report_type), user_id, db)
    except (SportMonksError, requests.RequestException):
        raise HTTPException(502, "Fixture data service is temporarily unavailable") from None
    except ProgrammingError as exc:
        schema_error(db, exc)

def _is_completed_enterprise_fixture(fixture: dict[str, Any]) -> bool:
    state = fixture.get("state") or {}
    code = str(
        state.get("code")
        or state.get("short_name")
        or state.get("state")
        or ""
    ).strip().upper().replace(" ", "_")
    if code in {"FT", "AET", "PEN", "WO", "AWARDED"}:
        return True
    name = str(state.get("name") or "").strip().casefold()
    return any(
        marker in name
        for marker in (
            "full time",
            "finished",
            "completed",
            "after extra time",
            "after penalties",
            "walkover",
            "awarded",
        )
    )


def _is_live_enterprise_fixture(fixture: dict[str, Any]) -> bool:
    state = fixture.get("state") or {}
    code = str(state.get("code") or state.get("short_name") or "").strip().upper().replace(" ", "_")
    if code in {"LIVE", "HT", "1ST", "2ND", "ET", "PEN_LIVE", "BREAK", "INT", "SUSP"}:
        return True
    name = str(state.get("name") or "").strip().casefold()
    return any(marker in name for marker in ("live", "in progress", "half time", "extra time", "penalty shootout"))


def _is_not_started_enterprise_fixture(fixture: dict[str, Any]) -> bool:
    if _is_completed_enterprise_fixture(fixture) or _is_live_enterprise_fixture(fixture):
        return False
    state = fixture.get("state") or {}
    code = str(state.get("code") or state.get("short_name") or "").strip().upper().replace(" ", "_")
    if code in {"NS", "TBA", "POSTP", "DELAYED"}:
        return True
    name = str(state.get("name") or "").strip().casefold()
    return any(marker in name for marker in ("not started", "scheduled", "to be announced", "postponed", "delayed"))


def _save_match(
    payload: EnterpriseFavoriteMatchIn,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    fixture = payload.fixture
    email = get_user_email_by_id(db, user_id)
    if not email:
        raise HTTPException(404, "Mobile user not found")
    if payload.reportType == "pre_match" and not _is_not_started_enterprise_fixture(fixture):
        raise HTTPException(
            status_code=409,
            detail="A pre-match report can only be saved before the match starts",
        )
    if payload.reportType == "post_match" and not _is_completed_enterprise_fixture(fixture):
        raise HTTPException(
            status_code=409,
            detail="A post-match report can only be saved after the match is completed",
        )
    country = fixture.get("country") or {}
    league = fixture.get("league") or {}
    home_team = fixture.get("homeTeam") or {}
    away_team = fixture.get("awayTeam") or {}
    required = {
        "fixtureId": fixture.get("fixtureId"),
        "startingAt": fixture.get("startingAt"),
        "homeTeam.id": home_team.get("id"),
        "homeTeam.name": home_team.get("name"),
        "awayTeam.id": away_team.get("id"),
        "awayTeam.name": away_team.get("name"),
    }
    missing = [key for key, value in required.items() if value is None or value == ""]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing fixture fields: {', '.join(missing)}")

    values = {
        "user_id": user_id,
        "user_email": email,
        "fixture_id": int(fixture["fixtureId"]),
        "report_type": payload.reportType,
        "starting_at": fixture["startingAt"],
        "country_id": country.get("id"),
        "country_name": str(country.get("name") or ""),
        "country_image_url": country.get("imageUrl"),
        "league_id": league.get("id"),
        "league_name": str(league.get("name") or ""),
        "league_image_url": league.get("imageUrl"),
        "home_team_id": int(home_team["id"]),
        "home_team_name": str(home_team["name"]),
        "home_team_image_url": home_team.get("imageUrl"),
        "home_score": home_team.get("score"),
        "away_team_id": int(away_team["id"]),
        "away_team_name": str(away_team["name"]),
        "away_team_image_url": away_team.get("imageUrl"),
        "away_score": away_team.get("score"),
        "state_code": (fixture.get("state") or {}).get("code"),
        "state_name": (fixture.get("state") or {}).get("name"),
        "result_info": fixture.get("resultInfo"),
        "fixture_payload": json.dumps(fixture),
    }
    row = db.execute(
        text(
            """
            INSERT INTO favorite_matches (
              user_id, user_email, fixture_id, report_type, starting_at,
              country_id, country_name, country_image_url,
              league_id, league_name, league_image_url,
              home_team_id, home_team_name, home_team_image_url, home_score,
              away_team_id, away_team_name, away_team_image_url, away_score,
              state_code, state_name, result_info, fixture_payload
            ) VALUES (
              :user_id, :user_email, :fixture_id, :report_type, :starting_at,
              :country_id, :country_name, :country_image_url,
              :league_id, :league_name, :league_image_url,
              :home_team_id, :home_team_name, :home_team_image_url, :home_score,
              :away_team_id, :away_team_name, :away_team_image_url, :away_score,
              :state_code, :state_name, :result_info, CAST(:fixture_payload AS jsonb)
            )
            ON CONFLICT (user_id, fixture_id, report_type) DO UPDATE SET
              user_email = EXCLUDED.user_email,
              starting_at = EXCLUDED.starting_at,
              country_id = EXCLUDED.country_id,
              country_name = EXCLUDED.country_name,
              country_image_url = EXCLUDED.country_image_url,
              league_id = EXCLUDED.league_id,
              league_name = EXCLUDED.league_name,
              league_image_url = EXCLUDED.league_image_url,
              home_team_id = EXCLUDED.home_team_id,
              home_team_name = EXCLUDED.home_team_name,
              home_team_image_url = EXCLUDED.home_team_image_url,
              home_score = EXCLUDED.home_score,
              away_team_id = EXCLUDED.away_team_id,
              away_team_name = EXCLUDED.away_team_name,
              away_team_image_url = EXCLUDED.away_team_image_url,
              away_score = EXCLUDED.away_score,
              state_code = EXCLUDED.state_code,
              state_name = EXCLUDED.state_name,
              result_info = EXCLUDED.result_info,
              fixture_payload = EXCLUDED.fixture_payload,
              updated_at = now()
            RETURNING id, fixture_payload, report_type, created_at
            """
        ),
        values,
    ).mappings().one()
    db.commit()
    return EnterpriseFavoriteMatchOut(
        favoriteId=str(row["id"]),
        fixture=dict(row["fixture_payload"] or {}),
        reportType=str(row.get("report_type") or "post_match"),
        createdAt=row["created_at"],
    )




@router.get("/match-pool/fixtures/{fixture_id}/post-match-card")
def post_match_card(fixture_id: int, user_id=Depends(require_auth)):
    """Read-only report cover using the same match data engine as enterprise."""
    from team_analysis_module.report import generate_match_report, MatchReportError
    if fixture_id <= 0:
        raise HTTPException(400, "Invalid fixture ID")
    try:
        report = generate_match_report(fixture_id, build_narratives=False)
    except (MatchReportError, requests.RequestException, ValueError):
        raise HTTPException(502, "Match report data is temporarily unavailable") from None
    if not _is_completed_enterprise_fixture({"state": report.get("state") or {}}):
        raise HTTPException(409, "Post-match reports are available after the match finishes")
    # No AI generation or saved-report mutation for these data-backed report sections.
    return {
        "fixture": report["fixture"],
        "league": report.get("league"),
        "venue": report.get("venue"),
        "weather": report.get("weather"),
        "state": report.get("state"),
        "scores": report.get("scores") or [],
        "teams": [{**{key: team.get(key) for key in (
            "id", "name", "location", "image_url", "coach_name", "formation",
            "categories", "expected_metrics"
        )}, "possession": next((metric.get("value") for metric in
            team.get("categories", {}).get("contribution_impact", [])
            if metric.get("name") == "Ball Possession %"), None)
        } for team in report.get("teams") or []],
        "period_teams": {period: [{key: team.get(key) for key in (
            "id", "name", "location", "image_url", "categories", "expected_metrics"
        )} for team in teams] for period, teams in (report.get("period_teams") or {}).items()},
        "pressure": [{key: point.get(key) for key in ("team_id", "minute", "value")} for point in report.get("pressure") or []],
        "lineups": [{key: player.get(key) for key in (
            "id", "player_id", "player_name", "team_id", "position_id",
            "position_name", "starter", "jersey_number", "formation_field",
            "formation_position", "categories", "expected_metrics"
        )} for player in report.get("lineups") or []],
        "events": [{key: event.get(key) for key in (
            "id", "team_id", "player_id", "player_name", "related_player_id",
            "related_player_name", "minute", "extra_minute", "type_id", "type",
            "team_name", "result"
        )} for event in report.get("events") or []],
        "coverage": {key: report.get("coverage", {}).get(key, 0) for key in (
            "unique_team_metrics", "unique_player_metrics", "players_with_minutes"
        )},
    }


@router.post("/match-pool/fixtures/{fixture_id}/team-analysis")
def post_match_team_analysis(fixture_id: int, language: str = "tr", user_id=Depends(require_auth)):
    from team_analysis_module.report import generate_match_report, MatchReportError
    from .team_analysis import build_team_analysis, cached_analysis, cache_analysis
    if fixture_id <= 0 or language not in {"tr", "en"}:
        raise HTTPException(400, "Invalid fixture or language")
    tier = user_report_tier(user_id)
    key = (user_id, fixture_id, language, tier)
    cached = cached_analysis(key)
    if cached is not None:
        return cached
    try:
        report = generate_match_report(fixture_id, lang=language, build_narratives=False)
        if not _is_completed_enterprise_fixture({"state": report.get("state") or {}}):
            raise HTTPException(409, "Team analysis is available after the match finishes")
        result = {"teams": build_team_analysis(report, language, include_locked=tier == 'paid')}
        cache_analysis(key, result)
        return result
    except (MatchReportError, requests.RequestException, ValueError):
        raise HTTPException(502, "Team analysis is temporarily unavailable") from None


@router.post("/match-pool/fixtures/{fixture_id}/player-perspectives")
def post_match_player_perspectives(fixture_id: int, language: str = "tr", user_id=Depends(require_auth)):
    from team_analysis_module.report import generate_match_report, MatchReportError
    from .player_perspectives import build_player_perspectives
    from .team_analysis import cached_analysis, cache_analysis
    if fixture_id <= 0 or language not in {"tr", "en"}:
        raise HTTPException(400, "Invalid fixture or language")
    tier = user_report_tier(user_id)
    key = ('player-perspectives', user_id, fixture_id, language, tier)
    cached = cached_analysis(key)
    if cached is not None:
        return cached
    try:
        report = generate_match_report(fixture_id, lang=language, build_narratives=False)
        if not _is_completed_enterprise_fixture({"state": report.get("state") or {}}):
            raise HTTPException(409, "Player analysis is available after the match finishes")
        result = {"teams": build_player_perspectives(report.get("teams") or [], report.get("lineups") or [], report.get("events") or [], language, include_locked=tier == "paid")}
        cache_analysis(key, result)
        return result
    except (MatchReportError, requests.RequestException, ValueError):
        raise HTTPException(502, "Player analysis is temporarily unavailable") from None


@router.post("/match-pool/fixtures/{fixture_id}/saved-report")
def open_saved_post_match_report(fixture_id: int, language: str = "tr", lazy: bool = False, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    from .persistent_reports import schedule, schedule_lazy, public_report, complete
    if fixture_id <= 0 or language not in {"tr", "en"}:
        raise HTTPException(400, "Invalid fixture or language")
    row = db.execute(text("SELECT id, fixture_id, report_status, report_content FROM favorite_matches WHERE user_id=:uid AND fixture_id=:fid AND report_type='post_match'"), {'uid':user_id,'fid':fixture_id}).mappings().first()
    if not row:
        fixture = get_fixture(fixture_id)
        if not fixture or not _is_completed_enterprise_fixture(fixture):
            raise HTTPException(409, "A post-match report requires a completed match")
        saved = _save_match(EnterpriseFavoriteMatchIn(fixture=fixture, reportType='post_match'),user_id,db)
        row = db.execute(text("SELECT id, fixture_id, report_status, report_content FROM favorite_matches WHERE id=:id AND user_id=:uid"), {'id':saved.favoriteId,'uid':user_id}).mappings().one()
    result = public_report(row,language)
    db.commit()
    if lazy:
        schedule_lazy(str(row['id']),user_id,language,'data')
    else:
        schedule(str(row['id']),user_id,language,retry_failed=True)
    if result['status'] != 'ready' or (not lazy and result.get('content') and not complete(result['content'], report_tier(db,user_id))):
        result['status'] = 'processing'
    return result


@router.get("/match-pool/fixtures/{fixture_id}/saved-report")
def poll_saved_post_match_report(fixture_id: int, language: str = "tr", lazy: bool = False, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    from .persistent_reports import schedule, schedule_lazy, public_report, compatible, complete
    if fixture_id <= 0 or language not in {"tr", "en"}:
        raise HTTPException(400, "Invalid fixture or language")
    row = db.execute(text("SELECT id, fixture_id, report_status, report_content FROM favorite_matches WHERE user_id=:uid AND fixture_id=:fid AND report_type='post_match'"), {'uid':user_id,'fid':fixture_id}).mappings().first()
    if not row:
        raise HTTPException(404, "Saved report not found")
    result = public_report(row,language)
    db.commit()
    if lazy:
        content = row.get('report_content') or {}
        if not compatible(content,language) or (content.get('sections') or {}).get('data',{}).get('status') != 'ready':
            schedule_lazy(str(row['id']),user_id,language,'data')
    elif row['report_status'] not in {'ready','failed'} or not compatible(row.get('report_content') or {},language) or not complete(row.get('report_content') or {}, report_tier(db,user_id)):
        schedule(str(row['id']),user_id,language)
        result['status'] = 'processing'
    return result


@router.post("/match-pool/fixtures/{fixture_id}/saved-report/sections/{section}")
def ensure_saved_post_match_section(fixture_id: int, section: str, language: str = "tr", user_id=Depends(require_auth), db: Session=Depends(get_db)):
    from .persistent_reports import schedule_lazy, public_report, compatible, section_ready, AI_SECTIONS
    if fixture_id <= 0 or language not in {"tr","en"} or section not in AI_SECTIONS:
        raise HTTPException(400,"Invalid report section")
    row = db.execute(text("SELECT id,fixture_id,report_status,report_content FROM favorite_matches WHERE user_id=:uid AND fixture_id=:fid AND report_type='post_match'"),{'uid':user_id,'fid':fixture_id}).mappings().first()
    if not row:
        raise HTTPException(404,"Saved report not found")
    content = dict(row.get('report_content') or {})
    if compatible(content,language) and not section_ready(content,section,report_tier(db,user_id)):
        sections = dict(content.get('sections') or {});sections[section]={**sections.get(section, {}), 'status':'processing'};content['sections']=sections
        row = {**dict(row),'report_content':content,'report_status':'ready'}
    if not section_ready(content,section,report_tier(db,user_id)):
        schedule_lazy(str(row['id']),user_id,language,section)
    return public_report(row,language)


@router.get("/match-pool/fixtures/{fixture_id}/pre-match-card")
def pre_match_card(fixture_id: int, user_id=Depends(require_auth)):
    from .pre_match_card import build_pre_match_card
    if fixture_id <= 0:
        raise HTTPException(400, "Invalid fixture")
    try:
        return build_pre_match_card(fixture_id)
    except (SportMonksError, requests.RequestException, ValueError):
        raise HTTPException(502, "Pre-match card is temporarily unavailable") from None


@router.get("/match-pool/fixtures/{fixture_id}/pre-match-squad")
def pre_match_squad(fixture_id: int, user_id=Depends(require_auth)):
    import os
    from concurrent.futures import ThreadPoolExecutor
    from .fixtures import SPORTMONKS_BASE_URL
    from .pre_match_usage import _team_recent_squad_usage
    if fixture_id <= 0:
        raise HTTPException(400, "Invalid fixture")
    try:
        response = requests.get(f"{SPORTMONKS_BASE_URL}/fixtures/{fixture_id}", params={"api_token":os.getenv("SPORTMONKS_API_KEY"),"include":"participants;season;league;formations;lineups.player;lineups.position"},timeout=45)
        response.raise_for_status()
        fixture=response.json().get('data') or {}
        def usage(team):
            raw=_team_recent_squad_usage(team,(fixture.get('season') or {}).get('id'),(fixture.get('season') or {}).get('name') or '',(fixture.get('league') or {}).get('id'),fixture.get('starting_at') or '')
            raw['team_name'] = team.get('name')
            raw['location'] = (team.get('meta') or {}).get('location')
            return {**{key:raw.get(key) for key in ('team_id','team_name','location','sample_size','formations','summary','results','performance_summary','momentum','score_flow','team_comparison')},'players':[dict(player) for player in raw.get('players') or []]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            usages=list(pool.map(usage,fixture.get('participants') or []))
        return {'teams':usages}
    except (SportMonksError, requests.RequestException, ValueError):
        raise HTTPException(502, "Squad usage is temporarily unavailable") from None


@router.get("/match-pool/fixtures/{fixture_id}/pre-match-form")
def pre_match_form(fixture_id: int, user_id=Depends(require_auth)):
    from .pre_match_standings import get_league_standings, StandingsError
    from .pre_match_card import build_pre_match_card
    result=pre_match_squad(fixture_id,user_id)
    card=build_pre_match_card(fixture_id)
    teams=sorted(card['teams'],key=lambda t:t.get('location')!='home')
    result['standings']={}
    try:
        standing=get_league_standings(int((card.get('league') or {}).get('id') or 0))
        for table in standing.get('tables') or []:
            for row in table.get('rows') or []:
                tid=str(row.get('teamId'))
                if tid not in result['standings']:
                    result['standings'][tid]={'position':row.get('position'),'points':row.get('points'),'table_label':table.get('label') or ''}
    except (StandingsError, requests.RequestException, ValueError, TypeError):
        pass
    return result


@router.get("/match-pool/fixtures/{fixture_id}/pre-match-players")
def pre_match_players(fixture_id: int, language: str = 'tr', user_id=Depends(require_auth)):
    from .pre_match_usage import _pre_match_player_perspectives
    if language not in {'tr','en'}:
        raise HTTPException(400, 'Invalid language')
    result=pre_match_squad(fixture_id,user_id)
    return {'teams':result['teams'], 'perspectives':(_pre_match_player_perspectives(result['teams'],language) if user_report_tier(user_id) == 'paid' else {})}


@router.get("/match-pool/fixtures/{fixture_id}/pre-match-momentum")
def pre_match_momentum(fixture_id: int, language: str = 'tr', user_id=Depends(require_auth)):
    from .pre_match_usage import _pre_match_momentum_perspectives
    if language not in {'tr','en'}:
        raise HTTPException(400, 'Invalid language')
    result=pre_match_squad(fixture_id,user_id)
    usages=result['teams']
    has_data=any(row.get('average_net_pressure') or row.get('goals_for') or row.get('goals_against') for usage in usages for row in usage.get('momentum') or [])
    return {'teams':[{k:u.get(k) for k in ('team_id','sample_size','momentum')} for u in usages], 'perspective':_pre_match_momentum_perspectives(usages,[{'id':u['team_id'],'name':u.get('team_name')} for u in usages],language) if has_data and user_report_tier(user_id) == 'paid' else None}


@router.get("/match-pool/fixtures/{fixture_id}/pre-match-score-flow")
def pre_match_score_flow(fixture_id: int, user_id=Depends(require_auth)):
    result=pre_match_squad(fixture_id,user_id)
    return {'teams':[{key:team.get(key) for key in ('team_id','sample_size','score_flow')} for team in result['teams']]}


@router.get("/match-pool/fixtures/{fixture_id}/pre-match-comparison")
def pre_match_comparison(fixture_id: int, user_id=Depends(require_auth)):
    result=pre_match_squad(fixture_id,user_id)
    return {'teams':[{key:team.get(key) for key in ('team_id','sample_size','team_comparison')} for team in result['teams']]}


@router.get("/match-pool/fixtures/{fixture_id}/pre-match-team-analysis")
def pre_match_team_analysis(fixture_id: int, language: str = 'tr', user_id=Depends(require_auth)):
    from .pre_match_usage import _pre_match_team_analysis
    if language not in {'tr','en'}:
        raise HTTPException(400, 'Invalid language')
    usages=pre_match_squad(fixture_id,user_id)['teams']
    teams=[{'id':u['team_id'],'name':u.get('team_name'),'location':u.get('location')} for u in usages]
    return {'teams':_pre_match_team_analysis(usages,teams,language,include_locked=user_report_tier(user_id) == 'paid')}

@router.post("/match-pool/fixtures/{fixture_id}/saved-pre-report")
def open_saved_pre_match_report(fixture_id: int, language: str = "tr", lazy: bool = False, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    from .persistent_pre_reports import schedule, schedule_lazy, public_report, complete
    if fixture_id <= 0 or language not in {"tr", "en"}:
        raise HTTPException(400, "Invalid fixture or language")
    row = db.execute(text("SELECT id, fixture_id, report_status, report_content FROM favorite_matches WHERE user_id=:uid AND fixture_id=:fid AND report_type='pre_match'"), {'uid':user_id,'fid':fixture_id}).mappings().first()
    if not row:
        fixture = get_fixture(fixture_id)
        if not fixture or not _is_not_started_enterprise_fixture(fixture):
            raise HTTPException(409, "A pre-match report must be created before kickoff")
        saved = _save_match(EnterpriseFavoriteMatchIn(fixture=fixture, reportType='pre_match'),user_id,db)
        row = db.execute(text("SELECT id, fixture_id, report_status, report_content FROM favorite_matches WHERE id=:id AND user_id=:uid"), {'id':saved.favoriteId,'uid':user_id}).mappings().one()
    from .persistent_pre_reports import compatible
    if not compatible(row.get('report_content') or {},language):
        current = get_fixture(fixture_id)
        if not _is_not_started_enterprise_fixture(current or {}):
            raise HTTPException(409, "A new pre-match report must be created before kickoff")
    result = public_report(row,language)
    db.commit()
    if lazy:
        schedule_lazy(str(row['id']),user_id,language,'foundation')
    else:
        schedule(str(row['id']),user_id,language,retry_failed=True)
    if result['status'] != 'ready' or (not lazy and result.get('content') and not complete(result['content'], report_tier(db,user_id))):
        result['status'] = 'processing'
    return result


@router.get("/match-pool/fixtures/{fixture_id}/saved-pre-report")
def poll_saved_pre_match_report(fixture_id: int, language: str = "tr", lazy: bool = False, user_id=Depends(require_auth), db: Session=Depends(get_db)):
    from .persistent_pre_reports import schedule, schedule_lazy, public_report, compatible, complete, FOUNDATION_SECTIONS
    if fixture_id <= 0 or language not in {"tr", "en"}:
        raise HTTPException(400, "Invalid fixture or language")
    row = db.execute(text("SELECT id, fixture_id, report_status, report_content FROM favorite_matches WHERE user_id=:uid AND fixture_id=:fid AND report_type='pre_match'"), {'uid':user_id,'fid':fixture_id}).mappings().first()
    if not row:
        raise HTTPException(404, "Saved report not found")
    if not compatible(row.get("report_content") or {},language):
        if not _is_not_started_enterprise_fixture(get_fixture(fixture_id) or {}):
            raise HTTPException(409, "A new pre-match report must be created before kickoff")
    result = public_report(row,language)
    db.commit()
    if lazy:
        content = row.get('report_content') or {}
        if not compatible(content,language) or not all((content.get('sections') or {}).get(key,{}).get('status') == 'ready' for key in FOUNDATION_SECTIONS):
            schedule_lazy(str(row['id']),user_id,language,'foundation')
    elif row['report_status'] not in {'ready','failed'} or not compatible(row.get('report_content') or {},language) or not complete(row.get('report_content') or {}, report_tier(db,user_id)):
        schedule(str(row['id']),user_id,language)
        result['status'] = 'processing'
    return result


@router.post("/match-pool/fixtures/{fixture_id}/saved-pre-report/sections/{section}")
def ensure_saved_pre_match_section(fixture_id: int, section: str, language: str = "tr", user_id=Depends(require_auth), db: Session=Depends(get_db)):
    from .persistent_pre_reports import schedule_lazy, public_report, compatible, AI_SECTIONS, section_ready
    if fixture_id <= 0 or language not in {"tr","en"} or section not in AI_SECTIONS:
        raise HTTPException(400,"Invalid report section")
    row = db.execute(text("SELECT id,fixture_id,report_status,report_content FROM favorite_matches WHERE user_id=:uid AND fixture_id=:fid AND report_type='pre_match'"),{'uid':user_id,'fid':fixture_id}).mappings().first()
    if not row:
        raise HTTPException(404,"Saved report not found")
    content = dict(row.get('report_content') or {})
    if compatible(content,language) and not section_ready(content,section,report_tier(db,user_id)):
        sections = dict(content.get('sections') or {});sections[section]={**sections.get(section, {}), 'status':'processing'};content['sections']=sections
        row = {**dict(row),'report_content':content,'report_status':'ready'}
    if not section_ready(content,section,report_tier(db,user_id)):
        schedule_lazy(str(row['id']),user_id,language,section)
    return public_report(row,language)
