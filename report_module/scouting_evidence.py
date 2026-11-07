"""Scouting presentation evidence; source statistics stay available for arithmetic."""
from math import isfinite


_IDENTITY_FIELDS = {
    "id", "player_id", "team_id", "contract_team_id", "club_player_id",
    "age", "height", "height_cm", "weight", "weight_kg", "match_count",
    "position_count_total", "potential", "form", "is_on_loan",
    "league_id", "nationality_id", "country_id", "position_id",
}
_STAT_CONTAINERS = {"stats", "statistics", "metrics"}


def _zero(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        number = float(str(value).strip().replace("%", "").replace(",", "."))
        return isfinite(number) and number == 0
    except (TypeError, ValueError):
        return False


def _metadata(source):
    result = {}
    for name, value in source.items():
        if str(name).startswith("_"):
            continue  # Internal rate evidence is not report text or chart data.
        if name in _STAT_CONTAINERS and isinstance(value, dict):
            result[name] = _metadata(value)
        elif name in _STAT_CONTAINERS and isinstance(value, list):
            result[name] = [dict(item) if isinstance(item, dict) else item for item in value
                            if not isinstance(item, dict) or not _zero(
                                item.get("value", item.get("amount", item.get("score"))))]
        elif name in _IDENTITY_FIELDS or not _zero(value):
            result[name] = value
    return result


def sanitize_scouting_documents(documents):
    """Copy scouting documents, omitting zero stats from metadata and snippets."""
    result = []
    for document in documents or []:
        if not isinstance(document, dict):
            continue
        metadata = _metadata(document.get("metadata") or {})
        # Rebuild the snippet from the same filtered evidence, so a raw snippet
        # cannot reintroduce zeros or internal rate counters into the AI input.
        content = "\n".join(f"{name}: {value}" for name, value in metadata.items()
                            if value is not None and not isinstance(value, (dict, list)))
        result.append({**document, "metadata": metadata, "content": content})
    return result
