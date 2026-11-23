from api_module.report_access import rewarded_report_sections_enabled, user_report_tier
from fastapi import APIRouter, Depends, HTTPException, Query
from api_module.utilities import require_auth
from api_module.database import get_db
from sqlalchemy.orm import Session
from team_pool_module.models import TeamPoolSearchRow
from team_portfolio_module.portfolio import begin_report, finish_report, get_report, PlayedMatchSnapshot, cached_profile, save_profile, ready_report_for_selection, team_id
from types import SimpleNamespace
from uuid import UUID
from .matches import get_current_team_matches
from match_pool_module.fixtures import SportMonksError
import requests
router = APIRouter(prefix="/team-analysis", tags=["team-analysis"])

@router.get("/{team_id}/matches")
def matches(team_id: int, leagueId: int = Query(gt=0), user_id=Depends(require_auth)):
    if team_id <= 0:
        raise HTTPException(400,"Invalid team")
    try:
        return get_current_team_matches(team_id, leagueId)
    except (SportMonksError, requests.RequestException):
        raise HTTPException(502,"Team match data is temporarily unavailable") from None

from fastapi import Header
from pydantic import BaseModel, Field, field_validator
from concurrent.futures import ThreadPoolExecutor
from . import report as engine
from .tactical_styles import STYLE_VERSION
from .opponent_metrics import build_opponent_metrics
from .team_findings import build_findings
from .report import build_team_report_metrics, build_team_report_player_perspectives, build_team_report_momentum_perspectives, build_team_report_regional_perspective, build_team_report_attack_profile, build_team_report_defense_profile, build_team_report_score_flow_profile, build_team_report_overview

from .fixture_reports import load_fixture_report


def _profile_cache_key(kind, paid):
    key = f'{kind}:{"paid" if paid else "free"}:v{engine.MATCH_REPORT_VERSION}:headings-v3'
    if kind in {"attack", "defense"}:
        key += ":selection-v3"
    return key + (f':assessment-v{engine.ASSESSMENT_VERSION}' if kind == 'assessment' else f':styles-v{STYLE_VERSION}')


class TeamAnalysisIn(BaseModel):
    team: TeamPoolSearchRow | None = None
    reportId: UUID | None = None
    teamId: int = Field(gt=0)
    leagueId: int = Field(gt=0)
    fixtureIds: list[int] = Field(min_length=1, max_length=5)

    @field_validator("fixtureIds")
    @classmethod
    def unique_positive_ids(cls, value):
        if len(set(value)) != len(value) or any(v <= 0 for v in value):
            raise ValueError("Select distinct valid matches")
        return value

@router.post("/report-data")
def report_data(payload: TeamAnalysisIn, user_id=Depends(require_auth), accept_language: str | None = Header(default=None), base_only: bool = False, db: Session = Depends(get_db)):
    fixture_ids=payload.fixtureIds
    lang="tr" if (accept_language or "").lower().startswith("tr") else "en"
    if payload.team is not None and not base_only:
        if team_id(payload.team) != payload.teamId or payload.team.leagueId != payload.leagueId:
            raise HTTPException(422, 'Team and league must match the selection')
        cached = ready_report_for_selection(db, user_id, payload.teamId, payload.leagueId, fixture_ids, lang, engine.MATCH_REPORT_VERSION)
        if cached is not None:
            return cached
        db.rollback()
    # Check membership and current season on the server, not only in the picker.
    try:
        matches = get_current_team_matches(payload.teamId,payload.leagueId)
        eligible={m["fixtureId"] for m in matches}
    except (SportMonksError,requests.RequestException):
        raise HTTPException(502,"Team match data is temporarily unavailable") from None
    if not set(payload.fixtureIds).issubset(eligible):
        raise HTTPException(400,"Select up to five completed matches from this team's current season")
    job = None
    if payload.team is not None and not base_only:
        snapshot = SimpleNamespace(team=payload.team, teamId=payload.teamId, fixtureIds=fixture_ids,
                                   matches=[PlayedMatchSnapshot(**m) for m in matches if m['fixtureId'] in fixture_ids])
        job = begin_report(db, user_id, snapshot, lang, engine.MATCH_REPORT_VERSION)
        if job['content'] is not None:
            return {**job['content'], 'reportId': job['id']}
    try:
        with ThreadPoolExecutor(max_workers=min(5, len(fixture_ids))) as executor:
            reports = list(executor.map(lambda fixture_id: load_fixture_report(fixture_id, lang), fixture_ids))
        # All implemented sections now use numeric evidence only.
        players = build_team_report_player_perspectives(reports, payload.teamId, lang, build_narratives=False)
        score_flow = build_team_report_score_flow_profile(reports, payload.teamId, lang, build_narratives=False)
        team_metrics, _ = build_team_report_metrics(reports, payload.teamId, lang, build_narratives=False)
        result = dict(reports=reports, playerPerspectives=players, scoreFlowProfile=score_flow, teamMetrics=team_metrics)
        if job is not None:
            finish_report(db, user_id, job, result)
            result['reportId'] = job['id']
        return result
    except Exception:
        if job is not None:
            db.rollback()
            finish_report(db, user_id, job)
        raise HTTPException(502,"The team analysis report could not be generated. Please retry.") from None

@router.post("/report-profile/{kind}")
def report_profile(kind: str, payload: TeamAnalysisIn, user_id=Depends(require_auth), accept_language: str | None = Header(default=None), db: Session = Depends(get_db), rewarded: bool = False):
    if kind not in {"attack", "defense", "assessment", "opponents"}:
        raise HTTPException(400, "Invalid profile")
    if rewarded and not rewarded_report_sections_enabled():
        raise HTTPException(403, "Rewarded reports are disabled")
    paid = rewarded or user_report_tier(user_id) == 'paid'
    if kind == "opponents" and not paid:
        raise HTTPException(403, "Unlock opponent data to view these sections")
    if payload.reportId:
        saved = get_report(payload.reportId, user_id, db)
        if saved['teamId'] != payload.teamId or set(saved['fixtureIds']) != set(payload.fixtureIds) or saved['team'].get('leagueId') != payload.leagueId:
            raise HTTPException(422, 'Saved report does not match the selected team and matches')
        base, language = saved['content'], saved['language']
    else:
        base = report_data(payload, user_id, accept_language, True, db)
        language = "tr" if (accept_language or "").lower().startswith("tr") else "en"
    profile_key = _profile_cache_key(kind, paid)
    if payload.reportId:
        cached = cached_profile(db, user_id, payload.reportId, profile_key)
        if cached is not None:
            return cached
    reports = base["reports"]
    if kind == "opponents":
        result = build_opponent_metrics(reports, payload.teamId)
        if payload.reportId:
            save_profile(db, user_id, payload.reportId, profile_key, result)
        return result
    if kind == "assessment":
        score_flow = base.get("scoreFlowProfile")
        states = (score_flow or {}).get("states") or []
        if not states or any("matchesToLevel" not in state for state in states):
            score_flow = build_team_report_score_flow_profile(reports, payload.teamId, language, build_narratives=False)
        strengths = build_findings(reports, payload.teamId, "strength", language, score_flow)
        weaknesses = build_findings(reports, payload.teamId, "weakness", language, score_flow,
                                     confirmed=strengths)
        result = {"strengths": strengths, "weaknesses": weaknesses}
        if not paid:
            for profile in result.values():
                profile["hasMoreThemes"] = len(profile["themes"]) > 1
                profile["themes"] = profile["themes"][:1]
        if payload.reportId:
            save_profile(db, user_id, payload.reportId, profile_key, result)
        return result
    builder = build_team_report_attack_profile if kind == "attack" else build_team_report_defense_profile
    try:
        profile = builder(reports, payload.teamId, language)
        if not paid:
            for group, flag in (("themes", "hasMoreThemes"), ("players", "hasMorePlayers")):
                profile[flag] = len(profile.get(group, [])) > 1
                profile[group] = profile.get(group, [])[:1]
        if payload.reportId:
            save_profile(db, user_id, payload.reportId, profile_key, profile)
        return profile
    except Exception:
        raise HTTPException(502, "Profile generation failed. Please retry.") from None
