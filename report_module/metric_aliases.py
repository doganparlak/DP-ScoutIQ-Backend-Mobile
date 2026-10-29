"""Normalize report metric snapshots for current and already-installed clients."""
from math import isfinite
from typing import Any

_ALIASES = {
    'Expected Goals (xG)': 'Expected Goals',
    'Expected Goals on Target (xGoT)': 'Expected Goals On Target',
    'Expected Assists (xA)': 'Expected Assists',
    'Shooting Performance (SP)': 'Shooting Performance',
    'Shots Blocked': 'Blocked Shots',
    'Yellowcards': 'Yellow Cards',
    'Goalkeeper Goals Conceded': 'Goals Conceded',
    'Long Balls Won Percentage': 'Long Balls Won (%)',
    'Successful Long Passes Percentage': 'Long Balls Won (%)',
    'Aerials Won Percentage': 'Aerials Won (%)',
    'Duels Won Percentage': 'Duels Won (%)',
    'Successful Crosses Percentage': 'Accurate Crosses (%)',
    'Successful Crosses (%)': 'Accurate Crosses (%)',
    'Successful Passes Percentage': 'Accurate Passes (%)',
    'Accurate Passes Percentage': 'Accurate Passes (%)',
    'Successful Dribbles Percentage': 'Dribble Accuracy (%)',
    'Tacles Won Percentage': 'Tackles Won (%)',
    'Tackles Won Percentage': 'Tackles Won (%)',
    'On-Target to Goal Conversion (%)': 'On Target Goal Conversion (%)',
    'On-Target Shot Quality (%)': 'On Target Shot Quality (%)',
}
_KEYED = {' '.join(name.split()).casefold(): canonical for name, canonical in _ALIASES.items()}

def canonical_metric_name(name: str) -> str:
    return _KEYED.get(' '.join(name.split()).casefold(), name.strip())

def _valid(row: dict) -> bool:
    try:
        return row.get('value') is not None and str(row['value']).strip() != '' and isfinite(float(str(row['value']).strip().rstrip('%')))
    except (TypeError, ValueError):
        return False

def normalize_metric_rows(rows: list[Any]) -> list[Any]:
    chosen = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str) or 'value' not in row:
            continue
        name = canonical_metric_name(row['name'])
        previous = chosen.get(name)
        if previous is None or (_valid(row) and (not _valid(previous) or (row['name'].strip() == name and previous['name'].strip() != name))):
            chosen[name] = row
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str) or 'value' not in row:
            result.append(row)
            continue
        name = canonical_metric_name(row['name'])
        if name not in seen:
            result.append({**chosen[name], 'name': name})
            seen.add(name)
    return result

def normalize_report_metrics(content: Any) -> Any:
    """Return a normalized copy; preserve narrative text and stored snapshots."""
    if isinstance(content, list):
        return normalize_metric_rows([normalize_report_metrics(item) for item in content])
    if not isinstance(content, dict):
        return content
    result = {key: normalize_report_metrics(value) for key, value in content.items()}
    # Older clients append expected_metrics to categories before aggregation.
    # Keep each player measurement in one source so that addition is not doubled.
    if result.get('player_id') is not None and isinstance(result.get('categories'), dict) and isinstance(result.get('expected_metrics'), list):
        names = {row['name'] for rows in result['categories'].values() if isinstance(rows, list)
                 for row in rows if isinstance(row, dict) and isinstance(row.get('name'), str) and _valid(row)}
        result['expected_metrics'] = [row for row in result['expected_metrics']
                                      if not isinstance(row, dict) or row.get('name') not in names]
    return result
