from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session
from api_module.database import get_db
from api_module.utilities import require_auth
from score_prediction_module.core import week_start

router = APIRouter(tags=["profile"])

@router.get("/me/dashboard-summary")
def dashboard_summary(user_id: int = Depends(require_auth), db: Session = Depends(get_db)):
    # Counts only: opening the profile never refreshes fixtures or generates reports.
    row = db.execute(text("""
        SELECT
          (SELECT count(*) FROM favorite_players WHERE user_id = :uid) AS "portfolioPlayers",
          (SELECT count(DISTINCT fixture_id) FROM favorite_matches WHERE user_id = :uid) AS "portfolioMatches",
          ((SELECT count(*) FROM scouting_reports WHERE user_id = :uid AND status = 'ready') +
           (SELECT count(*) FROM player_pool_scouting_reports WHERE user_id = :uid AND status = 'ready')) AS "readyPlayerReports",
          (SELECT count(*) FROM favorite_matches WHERE user_id = :uid
             AND COALESCE(report_type, 'post_match') = 'post_match'
             AND report_status = 'ready' AND report_content IS NOT NULL) AS "readyPostMatchReports",
          (SELECT count(*) FROM favorite_matches WHERE user_id = :uid
             AND report_type = 'pre_match'
             AND report_status = 'ready' AND report_content IS NOT NULL) AS "readyPreMatchReports"
    """), {"uid": user_id}).mappings().one()
    result = {key: int(value) for key, value in row.items()}
    # Existing dashboard stays usable until the user applies the new migration.
    available = db.execute(text("SELECT to_regclass('public.favorite_teams') IS NOT NULL AND to_regclass('public.team_reports') IS NOT NULL")).scalar()
    if available:
        counts = db.execute(text("""SELECT
            (SELECT count(*) FROM favorite_teams WHERE user_id=:uid) AS "portfolioTeams",
            (SELECT count(*) FROM team_reports WHERE user_id=:uid AND report_status='ready' AND report_content IS NOT NULL) AS "readyTeamReports"
        """), {'uid': user_id}).mappings().one()
        result.update({key: int(value) for key, value in counts.items()})
    else:
        result.update(portfolioTeams=0, readyTeamReports=0)
    predictions_available = db.execute(text("SELECT to_regclass('public.prediction_rounds') IS NOT NULL AND to_regclass('public.prediction_entries') IS NOT NULL")).scalar()
    result['weeklyScorePredictions'] = 0
    if predictions_available:
        result['weeklyScorePredictions'] = int(db.execute(text("""SELECT count(*)
            FROM public.prediction_entries e
            JOIN public.prediction_rounds r ON r.id=e.round_id
            CROSS JOIN LATERAL jsonb_object_keys(e.picks) AS pick
            WHERE e.user_id=:uid AND e.submitted_at IS NOT NULL AND r.week_start=:week
        """), {'uid': user_id, 'week': week_start()}).scalar() or 0)
    return result
