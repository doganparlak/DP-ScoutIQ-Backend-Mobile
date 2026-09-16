from api_module.report_access import user_report_tier
from fastapi import APIRouter, Depends, HTTPException, Query
from api_module.utilities import require_auth
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

class TeamAnalysisIn(BaseModel):
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
def report_data(payload: TeamAnalysisIn, user_id=Depends(require_auth), accept_language: str | None = Header(default=None), base_only: bool = False):
    # Check membership and current season on the server, not only in the picker.
    try:
        eligible={m["fixtureId"] for m in get_current_team_matches(payload.teamId,payload.leagueId)}
    except (SportMonksError,requests.RequestException):
        raise HTTPException(502,"Team match data is temporarily unavailable") from None
    if not set(payload.fixtureIds).issubset(eligible):
        raise HTTPException(400,"Select up to five completed matches from this team's current season")
    fixture_ids=payload.fixtureIds
    lang="tr" if (accept_language or "").lower().startswith("tr") else "en"
    try:
        with ThreadPoolExecutor(max_workers=min(5, len(fixture_ids))) as executor:
            reports = list(executor.map(lambda fixture_id: _raw_report(fixture_id, lang), fixture_ids))
        # All implemented sections now use numeric evidence only.
        players = build_team_report_player_perspectives(reports, payload.teamId, lang, build_narratives=False)
        score_flow = build_team_report_score_flow_profile(reports, payload.teamId, lang, build_narratives=False)
        team_metrics, _ = build_team_report_metrics(reports, payload.teamId, lang, build_narratives=False)
        return dict(reports=reports, playerPerspectives=players, scoreFlowProfile=score_flow, teamMetrics=team_metrics)
    except Exception:
        raise HTTPException(502,"The team analysis report could not be generated. Please retry.") from None

@router.post("/report-profile/{kind}")
def report_profile(kind: str, payload: TeamAnalysisIn, user_id=Depends(require_auth), accept_language: str | None = Header(default=None)):
    if kind not in {"attack", "defense", "assessment"}:
        raise HTTPException(400, "Invalid profile")
    paid = user_report_tier(user_id) == 'paid'
    base = report_data(payload, user_id, accept_language, True)
    language = "tr" if (accept_language or "").lower().startswith("tr") else "en"
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
        return {"strengths": strengths, "weaknesses": weaknesses}
    builder = build_team_report_attack_profile if kind == "attack" else build_team_report_defense_profile
    try:
        profile = builder(reports, payload.teamId, metrics, language, build_narratives=paid)
        profile["themes"] = (profile.get("themes") or [])[:2]
        for item in [*profile.get("themes", []), *profile.get("players", [])]:
            item["analysis"] = engine._mobile_profile_analysis(item.get("analysis"))
        return profile
    except Exception:
        raise HTTPException(502, "Profile generation failed. Please retry.") from None
