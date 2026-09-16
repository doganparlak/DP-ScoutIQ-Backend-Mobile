"""Server-owned report generation scope, shared by endpoints and workers."""
from sqlalchemy import text

PAID_REPORT_PLANS = frozenset({'No Ads Monthly', 'Pro Monthly', 'Pro Yearly'})


def report_tier(db, user_id):
    plan = db.execute(text('SELECT plan FROM users WHERE id=:uid'), {'uid': user_id}).scalar()
    return 'paid' if plan in PAID_REPORT_PLANS else 'free'


def user_report_tier(user_id):
    from api_module.database import engine
    with engine.connect() as db:
        return report_tier(db, user_id)


def scope_satisfies(state, tier):
    # Old reports contained the full narrative. Reuse those without paying again.
    return tier == 'free' or state.get('access_tier', 'paid') == 'paid'


def player_section_ready(content, section, tier):
    state = (content.get('sections') or {}).get(section, {})
    return (state.get('status') == 'ready' and scope_satisfies(state, tier)
            and bool((content.get('narrative_sections') or {}).get(section)))


def without_analysis(value):
    """Return deterministic team-profile data without paid narrative fields."""
    if isinstance(value, dict):
        return {key: ([] if key == 'analysis' else without_analysis(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [without_analysis(item) for item in value]
    return value
