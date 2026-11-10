"""Completed, non-empty biweekly windows shared by team and player views."""
from datetime import date, datetime, timedelta

from .metrics import PERIOD_ZONE, period_for


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def select_completed_periods(periods, requested=None, today=None):
    today = today or datetime.now(PERIOD_ZONE).date()
    available = sorted((dict(row) for row in periods
                        if _date(row['period_end']) <= today
                        and int(row.get('fixture_count') or 0) > 0
                        and row.get('status', 'ready') == 'ready'
                        and row.get('computed_at')), key=lambda row: _date(row['period_start']), reverse=True)
    if requested is not None:
        requested = _date(requested)
        if period_for(requested)[0] != requested or requested + timedelta(days=14) > today:
            raise ValueError('Select a completed two-week period')
        if not any(_date(row['period_start']) == requested for row in available):
            raise ValueError('Completed two-week period has no published matches')
    chosen = requested or (_date(available[0]['period_start']) if available else None)
    return available, chosen
