"""Opponent comparison over each saved pre-match team's historical window."""
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from standings_module.metrics import normalize_fixture
from standings_module.team_derived import DEFINITIONS, build_team_derived
from .pre_match_usage import _fixture_lineup_usage


def build_comparison(db, usages):
    windows = {}
    for team in usages:
        ids = team.get("selected_fixture_ids") or [row.get("fixture_id") for row in team.get("results") or []]
        window = tuple(dict.fromkeys(int(fixture_id) for fixture_id in ids if fixture_id))
        if len(window) != team.get("sample_size"):
            raise ValueError("Saved opponent comparison window is incomplete")
        windows[int(team["team_id"])] = window
    fixture_ids = sorted({fixture_id for window in windows.values() for fixture_id in window})
    if not fixture_ids:
        return {}
    cached = db.execute(text("""
        SELECT fixture_id, team_contributions FROM league_match_contributions
        WHERE fixture_id = ANY(CAST(:ids AS bigint[])) AND included_in_aggregation = TRUE
    """), {"ids":fixture_ids}).mappings()
    by_id = {row["fixture_id"]: dict(row) for row in cached if len(row["team_contributions"] or {}) == 2}
    missing = {fixture_id: team_id for team_id, window in windows.items() for fixture_id in window
               if fixture_id not in by_id or str(team_id) not in by_id[fixture_id]["team_contributions"]}
    if missing:
        def load(item):
            fixture_id, team_id = item
            return normalize_fixture(_fixture_lineup_usage(fixture_id, team_id))

        with ThreadPoolExecutor(max_workers=5) as pool:
            by_id.update((row["fixture_id"], row) for row in pool.map(load, missing.items()))

    result = {}
    for team_id, window in windows.items():
        groups = {"opponent_threat": [], "opponent_profile": []}
        result[str(team_id)] = groups
        if not window:
            continue
        metrics = build_team_derived(by_id[fixture_id] for fixture_id in window)["teams"].get(str(team_id), {})
        for name, metric in metrics.items():
            _, group, _ = DEFINITIONS[name]
            if group in groups:
                groups[group].append({"name": name, "value": metric["perMatch"],
                                      "matches_covered": metric["matchesCovered"],
                                      "aggregation": metric["aggregation"]})
    return result
