"""Bind each profile paragraph and its visible cards to source metric values."""
from copy import deepcopy
from typing import Any


PROFILE_EVIDENCE_GUIDANCE = """

Evidence contract for every theme and player:
- This contract overrides earlier exact card counts or field lists: include
  evidence_metrics, allow fewer cards, and never fill slots with unrelated data.
- In addition to the requested fields, return evidence_metrics: an array aligned
  with analysis, containing one array of exact source metric names per paragraph.
- Every paragraph must cite its actual supporting metrics internally, including
  relationships discussed without numbers. Use only this team's metrics for a
  team theme and only this player's metrics for a player paragraph. Never use
  another player's evidence. Do not fabricate, translate or rename these IDs.
- Choose a coherent finding before choosing the heading. The title, analysis and
  metric cards must describe the same finding. The visible cards are constructed
  from the union of evidence_metrics, using authoritative source values.
- Keep that union within the requested card limit (up to 4; obey a smaller task-specific limit). Use fewer cards when appropriate; never add unrelated metrics to fill
  slots. Each paragraph needs at least one supporting source metric. Prefer a
  relationship between action count, success rate and outcome where available.
- Interpret only what those measurements establish. Counts of tackles or
  interceptions do not locate actions, reveal timing, show a press scheme or
  prove that space behind the defensive line was protected. Pass accuracy alone
  does not show forward progression. Dribble failures do not locate possession
  losses or establish that the defensive line was broken. Do not infer poor
  finishing from goals alone without matching chance or shot evidence. Separate
  supported relationships from causal claims; do not add invisible movements,
  mental states or tactical intentions to make an interpretation sound deeper.
- evidence_metrics is internal metadata; narrative must still be natural prose
  in the requested language. Word limits remain editorial targets.
"""


def profile_snapshot(profile: dict) -> dict:
    """Keep citation metadata, but omit working source pools from API snapshots."""
    result = deepcopy(profile)
    for kind in ("themes", "players"):
        for row in result.get(kind) or []:
            row.pop("_source_metrics", None)
            row.pop("_metric_limit", None)
    return result


def free_profile_snapshot(value):
    """Free responses expose selections/cards, never narrative or editor metadata."""
    hidden = {'evidence_metrics', 'evidence_values', 'evidence_validation',
              'editorial_review', '_source_metrics', '_metric_limit'}
    if isinstance(value, dict):
        return {key: ([] if key == 'analysis' else free_profile_snapshot(item))
                for key, item in value.items() if key not in hidden}
    if isinstance(value, list):
        return [free_profile_snapshot(item) for item in value]
    return value


def bind_row_evidence(row: dict, source: dict[str, dict], limit: int) -> dict:
    """Unknown references never become cards or authoritative evidence.

    Keep usable original prose even if its citations are incomplete. Record the
    issue for the single editorial repair attempt instead of inventing support.
    """
    result = deepcopy(row)
    raw_analysis = row.get("analysis")
    if isinstance(raw_analysis, str):
        raw_analysis = [raw_analysis]
    raw_analysis = raw_analysis if isinstance(raw_analysis, list) else []
    references = row.get("evidence_metrics")
    references = references if isinstance(references, list) else []
    analyses, citations, issues = [], [], []
    if len(references) != len(raw_analysis):
        issues.append("evidence_metrics must align with every analysis paragraph")
    for index, paragraph in enumerate(raw_analysis):
        if not isinstance(paragraph, str) or not paragraph.strip():
            continue
        refs = references[index] if index < len(references) and isinstance(references[index], list) else []
        valid = []
        for label in refs:
            if not isinstance(label, str) or label not in source:
                issues.append(f"paragraph {len(analyses)}: unknown or out-of-scope metric {label!r}")
            elif label not in valid:
                valid.append(label)
        if not valid:
            issues.append(f"paragraph {len(analyses)}: missing source evidence")
        analyses.append(paragraph.strip())
        citations.append(valid)
    labels = list(dict.fromkeys(label for refs in citations for label in refs))
    if len(labels) > limit:
        issues.append(f"evidence union exceeds {limit} visible cards")
    # For a malformed citation response, retain its source-verified original
    # card selections temporarily; the editor sees that they are not citations.
    if not labels:
        selected = row.get("metrics")
        if isinstance(selected, list):
            labels = list(dict.fromkeys(item["label"] for item in selected
                if isinstance(item, dict) and isinstance(item.get("label"), str) and item["label"] in source))
    result.update(
        analysis=analyses,
        evidence_metrics=citations,
        evidence_values={label: deepcopy(source[label]) for label in labels},
        evidence_validation=issues,
        metrics=[deepcopy(source[label]) for label in labels[:limit]],
        _source_metrics=deepcopy(source),
        _metric_limit=limit,
    )
    return result


def build_evidence_profile(draft: Any, team_source: dict, players: list[dict] | None, *, theme_limit: int = 4) -> dict:
    if not isinstance(draft, dict):
        raise ValueError("Profile response must be an object")
    result = {"themes": [], "players": []} if players is not None else {"themes": []}
    players = players or []
    themes = draft.get("themes")
    for row in (themes if isinstance(themes, list) else [])[:3]:
        if isinstance(row, dict):
            result["themes"].append(bind_row_evidence({
                "title": str(row.get("title") or "").strip(),
                "analysis": row.get("analysis"), "metrics": row.get("metrics"),
                "evidence_metrics": row.get("evidence_metrics"),
            }, team_source, theme_limit))
    by_id = {str(player["playerId"]): player for player in players}
    by_name = {str(player.get("name") or "").strip().casefold(): player for player in players}
    used = set()
    rows = draft.get("players")
    for row in (rows if isinstance(rows, list) else [])[:2]:
        if not isinstance(row, dict):
            continue
        player = by_id.get(str(row.get("playerId"))) or by_name.get(str(row.get("name") or "").strip().casefold())
        if not player or str(player["playerId"]) in used:
            continue
        used.add(str(player["playerId"]))
        identity = {key: deepcopy(value) for key, value in player.items() if key != "source_metrics"}
        result["players"].append(bind_row_evidence({
            **identity, "analysis": row.get("analysis"), "metrics": row.get("metrics"),
            "evidence_metrics": row.get("evidence_metrics"),
        }, player["source_metrics"], 4))
    return result
