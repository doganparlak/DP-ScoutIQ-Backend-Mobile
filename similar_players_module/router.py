"""Mobile subscription limits are enforced before full-profile hydration."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from api_module.database import get_db
from api_module.report_access import rewarded_report_sections_enabled
from api_module.utilities import require_auth
from .similar import SimilarPlayersIn, similar_players, similarity_eligibility
router = APIRouter(prefix='/similar-players', tags=['similar-players'])

def result_limit(plan):
    return 15 if plan in {'Pro Monthly', 'Pro Yearly'} else 6 if plan == 'No Ads Monthly' else 3

@router.post('/eligibility')
def eligibility(payload: SimilarPlayersIn, user_id=Depends(require_auth), db=Depends(get_db)):
    return similarity_eligibility(db, payload)

@router.post('/search')
def search(payload: SimilarPlayersIn, user_id=Depends(require_auth), db=Depends(get_db),
           rewardedBatches: int = Query(default=0, ge=0, le=4)):
    plan = db.execute(text('SELECT plan FROM users WHERE id=:uid'), {'uid':user_id}).scalar()
    enabled = rewarded_report_sections_enabled()
    base_limit = result_limit(plan)
    limit = min(15, base_limit + 3 * rewardedBatches) if enabled else base_limit
    return {**similar_players(db, payload, limit), 'limit': limit, 'pageSize': 3,
            'rewardedEnabled': enabled and base_limit < 15}
