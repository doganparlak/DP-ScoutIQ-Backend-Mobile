"""Enterprise opponent metric groups over the selected team report snapshots."""
from collections import defaultdict

from report_module.rate_evidence import add_sample, empty, key as rate_key, number, rate_value

MIRRORS = {
    'Opponent Attacks': ('Attacks', 'opponent_threat'),
    'Opponent Dangerous Attacks': ('Dangerous Attacks', 'opponent_threat'),
    'Opponent Big Chances Created': ('Big Chances Created', 'opponent_threat'),
    'Opponent Shots Total': ('Shots Total', 'opponent_threat'),
    'Opponent Shots On Target': ('Shots On Target', 'opponent_threat'),
    'Opponent Shots Insidebox': ('Shots Insidebox', 'opponent_threat'),
    'Opponent Shots Outsidebox': ('Shots Outsidebox', 'opponent_threat'),
    'Expected Goals Against (xGA)': ('Expected Goals', 'opponent_threat'),
    'Expected Goals On Target Against (xGoTA)': ('Expected Goals On Target', 'opponent_threat'),
    'Opponent Key Passes': ('Key Passes', 'opponent_threat'),
    'Opponent Total Crosses': ('Total Crosses', 'opponent_threat'),
    'Opponent Accurate Crosses': ('Accurate Crosses', 'opponent_threat'),
    'Opponent Dribble Attempts': ('Dribble Attempts', 'opponent_threat'),
    'Opponent Successful Dribbles': ('Successful Dribbles', 'opponent_threat'),
    'Opponent Corners': ('Corners', 'opponent_threat'),
    'Opponent Big Chances Missed': ('Big Chances Missed', 'opponent_threat'),
    'Opponent Hit Woodwork': ('Hit Woodwork', 'opponent_threat'),
    'Opponent Passes': ('Passes', 'opponent_profile'),
    'Opponent Accurate Passes': ('Accurate Passes', 'opponent_profile'),
    'Opponent Long Balls': ('Long Balls', 'opponent_profile'),
    'Opponent Long Balls Won': ('Long Balls Won', 'opponent_profile'),
    'Opponent Tackles Won': ('Tackles Won', 'opponent_profile'),
    'Opponent Interceptions': ('Interceptions', 'opponent_profile'),
    'Opponent Saves': ('Saves', 'opponent_profile'),
    'Opponent Counter Attacks': ('Counter Attacks', 'opponent_profile'),
}
RATES = {
    'Opponent Shots On Target (%)': ('Shots On Target (%)', 'opponent_threat'),
    'Opponent Goal Conversion (%)': ('Goal Conversion (%)', 'opponent_threat'),
    'Opponent On Target Goal Conversion (%)': ('On Target Goal Conversion (%)', 'opponent_threat'),
    'Opponent Accurate Crosses (%)': ('Accurate Crosses (%)', 'opponent_threat'),
    'Opponent Dribble Accuracy (%)': ('Dribble Accuracy (%)', 'opponent_threat'),
    'Opponent Shot Quality (%)': ('Shot Quality (%)', 'opponent_threat'),
    'Opponent On Target Shot Quality (%)': ('On Target Shot Quality (%)', 'opponent_threat'),
    'Opponent Accurate Passes (%)': ('Accurate Passes (%)', 'opponent_profile'),
    'Opponent Long Balls Won (%)': ('Long Balls Won (%)', 'opponent_profile'),
}

def _recorded_values(team, metric_aliases):
    values = {}
    sources = [*(team.get("categories") or {}).values(), team.get("expected_metrics"), team.get("extra_metrics")]
    for rows in sources:
        for metric in rows or []:
            if metric.get("_inferred_zero") or not metric.get("name"):
                continue
            name = metric["name"]
            name = metric_aliases.get(name, ("", name))[1]
            if values.get(name) is None:
                values[name] = number(metric.get("value"))
    return values


def build_opponent_metrics(reports, team_id):
    from .report import TEAM_METRIC_CATEGORY

    counts = defaultdict(list)
    rates = empty()
    for report in reports:
        teams = report.get("teams") or []
        if len(teams) != 2:
            continue
        own = next((team for team in teams if str(team.get("id")) == str(team_id)), None)
        if own is None:
            continue
        opponent = next(team for team in teams if team is not own)
        own_values = _recorded_values(own, TEAM_METRIC_CATEGORY)
        opponent_values = _recorded_values(opponent, TEAM_METRIC_CATEGORY)
        add_sample(rates, opponent_values)
        for name, (source, _) in MIRRORS.items():
            value = own_values.get(name) if name == "Expected Goals Against (xGA)" else None
            if value is None:
                value = opponent_values.get(source)
            if value is not None:
                counts[name].append(value)

    result = {"opponent_threat": [], "opponent_profile": []}
    for name, (_, group) in MIRRORS.items():
        if values := counts.get(name):
            result[group].append({"name": name, "perMatch": round(sum(values) / len(values), 4),
                                  "matchesCovered": len(values), "aggregation": "total"})
    for name, (source, group) in RATES.items():
        state = rates["rates"].get(rate_key(source))
        value = rate_value(source, state)
        if value is not None:
            result[group].append({"name": name, "perMatch": round(value, 4),
                                  "matchesCovered": int(state["coverage"]), "aggregation": "derived_rate"})
    return result
