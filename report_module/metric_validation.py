"""Validation shared by report calculations and selected player evidence."""
from math import isfinite
from typing import Any


def numeric(value: Any) -> float | None:
    try:
        result = float(str(value).strip().rstrip('%'))
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def is_percentage(name: str) -> bool:
    return '%' in name or 'percentage' in name.casefold()


def valid_metric(name: str, value: Any, *, nonzero: bool = False) -> bool:
    number = numeric(value)
    return number is not None and (not nonzero or number != 0) and (
        not is_percentage(name) or 0 <= number <= 100
    )


def percentage(numerator: Any, denominator: Any) -> float | None:
    a, b = numeric(numerator), numeric(denominator)
    if a is None or b is None or b <= 0 or not 0 <= a <= b:
        return None
    return a / b * 100


def rate_counts(name: str, counts: dict[str, Any], numerator: str,
                denominator: str) -> tuple[float, float] | None:
    a, b = numeric(counts.get(numerator)), numeric(counts.get(denominator))
    if name in {'Aerials Won (%)', 'Aerials Won Percentage'} and a is not None and 'Aerials Lost' in counts:
        lost = numeric(counts['Aerials Lost'])
        if lost is None or lost < 0:
            return None
        b = a + lost
    return (a, b) if percentage(a, b) is not None else None


def nonzero_evidence(groups: dict[str, Any]) -> dict[str, Any]:
    """Filter selected-player evidence, preserving the raw statistical tables."""
    return {group: {name: value for name, value in metrics.items()
                    if valid_metric(name, value, nonzero=True)}
            for group, metrics in groups.items()}


def sanitize_percentages(content: Any) -> Any:
    """Remove invalid percentage values from structured report snapshots."""
    if isinstance(content, list):
        kept = []
        for item in content:
            if isinstance(item, dict):
                name = str(item.get('name') or item.get('metric') or item.get('stat') or item.get('label') or '')
                value = item.get('value', item.get('data'))
                if isinstance(value, dict):
                    value = value.get('value')
                if is_percentage(name) and not valid_metric(name, value):
                    continue
            kept.append(sanitize_percentages(item))
        return kept
    if isinstance(content, dict):
        return {key: sanitize_percentages(value) for key, value in content.items()
                if not is_percentage(str(key)) or valid_metric(str(key), value)}
    return content


def sanitize_player_highlights(content: Any) -> Any:
    """Apply highlight rules to saved snapshots without changing source data."""
    if isinstance(content, list):
        return [sanitize_player_highlights(item) for item in content]
    if not isinstance(content, dict):
        return content
    result = {key: sanitize_player_highlights(value) for key, value in content.items()}
    for key in ('standout_metrics', 'development_metrics'):
        if isinstance(result.get(key), list):
            result[key] = [metric for metric in result[key] if isinstance(metric, dict)
                           and valid_metric(str(metric.get('name') or ''), metric.get('value'), nonzero=True)
                           and (not metric.get('is_percentage') or percentage(metric.get('value'), 100) is not None)]
    return result
