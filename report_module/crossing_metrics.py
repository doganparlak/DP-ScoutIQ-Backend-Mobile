"""Canonical crossing rates for current and previously saved match reports."""
from math import isfinite
from typing import Any

CROSSING_RATE = "Accurate Crosses (%)"
CROSSING_RATE_NAMES = frozenset({CROSSING_RATE, "Successful Crosses Percentage", "Successful Crosses (%)"})


def normalize_crossing_rows(rows: list[Any]) -> list[Any]:
    """Prefer the calculated canonical rate; retain a provider-only rate as fallback."""
    chosen = None
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or row["name"] not in CROSSING_RATE_NAMES:
            continue
        try:
            value = float(str(row.get("value")).strip().rstrip("%"))
        except (ValueError, TypeError):
            continue
        if not isfinite(value) or not 0 <= value <= 100:
            continue
        if chosen is None or (row["name"] == CROSSING_RATE and chosen["name"] != CROSSING_RATE):
            chosen = row
    result, inserted = [], False
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"] in CROSSING_RATE_NAMES:
            if not inserted and chosen is not None:
                result.append({**chosen, "name": CROSSING_RATE})
                inserted = True
        else:
            result.append(row)
    return result


def normalize_crossing_metrics(content: Any) -> Any:
    """Normalize snapshots on read without rewriting the database or AI text."""
    if isinstance(content, list):
        return normalize_crossing_rows([normalize_crossing_metrics(item) for item in content])
    if isinstance(content, dict):
        return {key: normalize_crossing_metrics(value) for key, value in content.items()}
    return content
