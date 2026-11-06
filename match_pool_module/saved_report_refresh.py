"""Keep a readable snapshot until its replacement is successfully generated."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone


def readable_snapshot(content, language, report_format, foundations):
    return (content.get('format') == report_format and content.get('language') == language
            and all(section in content and (content.get('sections') or {}).get(section, {}).get('status') == 'ready'
                    for section in foundations))


def archived_snapshot(content):
    fixture = (content.get('data') or {}).get('fixture') or {}
    try:
        start = datetime.fromisoformat(str(fixture.get('starting_at') or fixture.get('startingAt')).replace('Z', '+00:00'))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return start <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def refresh_deferred(content):
    try:
        return datetime.fromisoformat(content['refresh_retry_after']) > datetime.now(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False


def checkpoint_writer(save, favorite_id, user_id, row, language, report_format, foundations, ready):
    previous = deepcopy(row.get('report_content') or {})
    preserve = row.get('report_status') == 'ready' and readable_snapshot(previous, language, report_format, foundations)

    def checkpoint(content, status):
        if preserve:
            if status != 'failed' and ready(content):
                return save(favorite_id, user_id, content, status)
            if status == 'failed' or any(state.get('status') == 'failed' for state in content.get('sections', {}).values()):
                # A short durable cooldown prevents polling from hammering a failing provider.
                backup = deepcopy(previous)
                backup['refresh_retry_after'] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
                return save(favorite_id, user_id, backup, 'ready')
            if not ready(content):
                return True
        return save(favorite_id, user_id, content, status)

    return checkpoint
