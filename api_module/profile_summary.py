from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session
from api_module.database import get_db
from api_module.utilities import require_auth

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
    return {key: int(value) for key, value in row.items()}
