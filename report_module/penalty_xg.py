"""Penalty xG uses recorded attempts, including missed penalties."""
from typing import Any

from .metric_validation import numeric

PENALTY_XG = 'Expected Goals Penalties (pxG)'


def penalty_xg_sample(value: Any, events: list[dict], team_id: int) -> tuple[float, int] | None:
    xg = numeric(value)
    if xg is None or xg < 0:
        return None
    attempts = 0
    seen = set()
    for event in events:
        if str(event.get('participant_id') or event.get('team_id') or '') != str(team_id) or event.get('rescinded'):
            continue
        kind = event.get('type') or {}
        name = str(kind.get('name') if isinstance(kind, dict) else kind).strip().casefold()
        # Shootout penalties have separate event types and are not match xG.
        if name not in {'penalty', 'missed penalty'}:
            continue
        event_id = event.get('id')
        if event_id is not None:
            if event_id in seen:
                continue
            seen.add(event_id)
        attempts += 1
    return (xg, attempts) if attempts else None


def penalty_xg_average(samples: list[tuple[float, int]]) -> float | None:
    attempts = sum(count for _, count in samples)
    return round(sum(value for value, _ in samples) / attempts, 2) if attempts else None
