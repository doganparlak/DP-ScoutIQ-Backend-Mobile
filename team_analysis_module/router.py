from api_module.report_access import user_report_tier
from fastapi import APIRouter, Depends, HTTPException, Query
from api_module.utilities import require_auth
from api_module.database import get_db
from sqlalchemy.orm import Session
from team_pool_module.models import TeamPoolSearchRow
from team_portfolio_module.portfolio import begin_report, finish_report, get_report, PlayedMatchSnapshot, cached_profile, save_profile
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
from .report import generate_match_report, build_team_report_metrics, build_team_report_player_perspectives, build_team_report_momentum_perspectives, build_team_report_regional_perspective, build_team_report_attack_profile, build_team_report_defense_profile, build_team_report_score_flow_profile, build_team_report_strengths, build_team_report_weaknesses, build_team_report_overview
from report_module.profile_editor import review_team_profiles
from report_module.profile_evidence import free_profile_snapshot

from collections import OrderedDict
from copy import deepcopy
from threading import Lock
from time import monotonic

# Reuse raw fixture evidence between the quick squad response and full analysis.
_RAW_REPORTS = OrderedDict()
_RAW_LOCK = Lock()
def _raw_report(fixture_id, language):
    key = (fixture_id, language)
    with _RAW_LOCK:
        cached = _RAW_REPORTS.get(key)
        if cached and monotonic() - cached[0] < 300:
            _RAW_REPORTS.move_to_end(key)
            return deepcopy(cached[1])
    report = generate_match_report(fixture_id, language, False)
    with _RAW_LOCK:
        _RAW_REPORTS[key] = (monotonic(), deepcopy(report))
        _RAW_REPORTS.move_to_end(key)
        while len(_RAW_REPORTS) > 32:
            _RAW_REPORTS.popitem(last=False)
    return report


def _profile_cache_key(kind, paid):
    key = f'{kind}:{"paid" if paid else "free"}:v{engine.MATCH_REPORT_VERSION}:headings-v3'
    return key + (f':assessment-v{engine.ASSESSMENT_VERSION}' if kind == 'assessment' else '')


def _review_profiles(profiles, payload, user_id, db, language, paid):
    if not paid:
        # Never run paid narrative generation/review for free accounts.
        return {key: free_profile_snapshot(value) for key, value in profiles.items()}
    context = dict(profiles)
    if payload.reportId:
        for kind, keys in [('attack', ['attackProfile']), ('defense', ['defenseProfile']),
                           ('assessment', ['strengths', 'weaknesses'])]:
            if all(key in context for key in keys):
                continue
            cached = cached_profile(db, user_id, payload.reportId, _profile_cache_key(kind, True))
            if cached:
                if kind == 'assessment':
                    context.update({key: cached[key] for key in keys if key in cached and key not in context})
                else:
                    context.setdefault(keys[0], cached)
    # Lazy sections are reviewed with available cached sections as read-only
    # context. Creating another section never silently rewrites a saved one.
    reviewed = review_team_profiles(context, language, editable_sections=set(profiles), task_contract=(
        'Mobile report: every team and player analysis is ONE paragraph targeting 20-30 words. '
        'Team counts and player counts are BOTH per 90, not per match. Rates retain percentages. '
        'Use at most four cards and preserve plan visibility. Cached sections without editable '
        'passage IDs are read-only context. Evidence_values in cached rows are source-verified values.'
    ))
    return {key: reviewed[key] for key in profiles}

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
    # Check membership and current season on the server, not only in the picker.
    try:
        matches = get_current_team_matches(payload.teamId,payload.leagueId)
        eligible={m["fixtureId"] for m in matches}
    except (SportMonksError,requests.RequestException):
        raise HTTPException(502,"Team match data is temporarily unavailable") from None
    if not set(payload.fixtureIds).issubset(eligible):
        raise HTTPException(400,"Select up to five completed matches from this team's current season")
    fixture_ids=payload.fixtureIds
    lang="tr" if (accept_language or "").lower().startswith("tr") else "en"
    job = None
    if payload.team is not None and not base_only:
        if payload.team.leagueId != payload.leagueId:
            raise HTTPException(422, 'Team league does not match the selection')
        snapshot = SimpleNamespace(team=payload.team, teamId=payload.teamId, fixtureIds=fixture_ids,
                                   matches=[PlayedMatchSnapshot(**m) for m in matches if m['fixtureId'] in fixture_ids])
        job = begin_report(db, user_id, snapshot, lang, engine.MATCH_REPORT_VERSION)
        if job['content'] is not None:
            return {**job['content'], 'reportId': job['id']}
    try:
        with ThreadPoolExecutor(max_workers=min(5, len(fixture_ids))) as executor:
            reports = list(executor.map(lambda fixture_id: _raw_report(fixture_id, lang), fixture_ids))
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
def report_profile(kind: str, payload: TeamAnalysisIn, user_id=Depends(require_auth), accept_language: str | None = Header(default=None), db: Session = Depends(get_db)):
    if kind not in {"attack", "defense", "assessment"}:
        raise HTTPException(400, "Invalid profile")
    paid = user_report_tier(user_id) == 'paid'
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
    metrics, _ = build_team_report_metrics(reports, payload.teamId, language, build_narratives=False)
    if kind == "assessment":
        strengths = build_team_report_strengths(reports, payload.teamId, metrics, language, build_narratives=paid)
        # Preserve enterprise's dependency: weaknesses must respect confirmed strengths.
        weaknesses = build_team_report_weaknesses(reports, payload.teamId, metrics, strengths, language, build_narratives=paid)
        for profile in (strengths, weaknesses):
            profile["themes"] = (profile.get("themes") or [])[:2]
            for item in profile["themes"]:
                item["analysis"] = engine._mobile_profile_analysis(item.get("analysis"))
        result = _review_profiles({"strengths": strengths, "weaknesses": weaknesses}, payload, user_id, db, language, paid)
        if payload.reportId:
            save_profile(db, user_id, payload.reportId, profile_key, result)
        return result
    builder = build_team_report_attack_profile if kind == "attack" else build_team_report_defense_profile
    try:
        profile = builder(reports, payload.teamId, metrics, language, build_narratives=paid)
        profile["themes"] = (profile.get("themes") or [])[:2]
        for item in [*profile.get("themes", []), *profile.get("players", [])]:
            item["analysis"] = engine._mobile_profile_analysis(item.get("analysis"))
        key = "attackProfile" if kind == "attack" else "defenseProfile"
        profile = _review_profiles({key: profile}, payload, user_id, db, language, paid)[key]
        if payload.reportId:
            save_profile(db, user_id, payload.reportId, profile_key, profile)
        return profile
    except Exception:
        raise HTTPException(502, "Profile generation failed. Please retry.") from None
