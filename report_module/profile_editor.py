"""One joint review, one targeted repair; failed repairs retain original prose."""
import json
import os
import re
from copy import deepcopy

from report_module.profile_evidence import bind_row_evidence
from report_module.profile_review import prose_issues
from report_module.terminology import FOOTBALL_TERMINOLOGY_GUIDANCE


def _decode(response, field):
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(response.content or "").strip(), flags=re.I)
    value = json.loads(raw)
    if isinstance(value, list):
        return {field: value}
    if not isinstance(value, dict):
        raise ValueError("Editor response must be an object")
    return value


def review_team_profiles(profiles: dict, lang: str, *, editable_sections=None, task_contract="") -> dict:
    """Review all four sections together without changing metrics or identities.

    Evidence existence is checked by code; semantic support is reviewed by the
    model. Neither this review nor its single repair can guarantee factual truth.
    """
    result = deepcopy(profiles)
    targets, flagged = {}, {}
    for section, profile in result.items():
        if editable_sections is not None and section not in editable_sections:
            continue
        for kind in ("themes", "players"):
            for index, row in enumerate(profile.get(kind) or []):
                if not isinstance(row, dict):
                    continue
                entries = [("title", None, row.get("title"))]
                entries += [("analysis", i, text) for i, text in enumerate(row.get("analysis") or [])]
                for field, bullet, text in entries:
                    if not isinstance(text, str) or not text.strip():
                        continue
                    key = f"{section}.{kind}.{index}.{field}" + (f".{bullet}" if bullet is not None else "")
                    targets[key] = (row, field, bullet)
                    issues = prose_issues(text, lang, analysis=field == "analysis")
                    if field == "analysis":
                        issues += row.get("evidence_validation") or []
                    if issues:
                        flagged[key] = list(dict.fromkeys(issues))
    accepted = set()
    review_succeeded = False
    model = None
    if targets and os.getenv("OPENAI_API_KEY"):
        try:
            from langchain_openai import ChatOpenAI

            model = ChatOpenAI(
                model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
                api_key=os.environ["OPENAI_API_KEY"], temperature=0.2,
            )
            review = _decode(model.invoke([
                ("system", "You are the final football evidence editor. Review attackProfile, defenseProfile, strengths and weaknesses TOGETHER. Return JSON with exactly issues, an array of {target, reason}. Use only supplied passage IDs. Flag ONLY concrete problems: a statement not supported by its cited metrics, contradiction across sections about the SAME relationship, title/analysis/card mismatch, wrong metric meaning, unjustified benchmark or causal/location/timing/tactical claim, repeated generic prose, or wording issues. Different conclusions about distinct aspects are not automatically contradictions. Check count versus success rate versus outcome and per-match team values versus per-90 player values. _source_metrics is the authoritative metric pool for that row; evidence_metrics identifies each paragraph's claimed support. A reference existing is not enough: check whether it actually supports the claim. Missing references are an issue. Do not infer causation from aggregate association. Do not flag word count deviations. Explicit task_contract units and length targets override generic defaults. Other sections are read-only context when they have no supplied passage IDs. Do not rewrite anything yet. An empty issues array is valid." + FOOTBALL_TERMINOLOGY_GUIDANCE),
                ("human", json.dumps({"language": lang, "passage_ids": list(targets), "sections": result, "machine_issues": flagged, "task_contract": task_contract}, ensure_ascii=False, default=str)),
            ]), "issues")
            if not isinstance(review.get("issues"), list):
                raise ValueError("Missing editor issues array")
            review_succeeded = True
            for issue in review["issues"]:
                if not isinstance(issue, dict):
                    continue
                target, reason = issue.get("target"), issue.get("reason")
                if isinstance(target, str) and target in targets and isinstance(reason, str) and reason.strip():
                    flagged.setdefault(target, []).append(reason.strip())
        except Exception as exc:
            print(f"[enterprise_team_report] event=joint_review_unavailable error_type={type(exc).__name__}")

        # One repair invocation for all flagged passages, never a retry loop.
        if flagged and model is not None:
            try:
                passages = {}
                for key, reasons in flagged.items():
                    row, field, bullet = targets[key]
                    passages[key] = {
                        "original": row[field] if bullet is None else row[field][bullet],
                        "reasons": reasons, "identity": row.get("name") or row.get("title"),
                        "source_metrics": row.get("_source_metrics", {}),
                        "evidence_metrics": row.get("evidence_metrics", []),
                        "metric_limit": row.get("_metric_limit", 4),
                    }
                corrections = _decode(model.invoke([
                    ("system", "Repair ONLY the flagged passages. Return a JSON object in this exact form: {\"corrections\": [{\"target\": \"supplied passage ID\", \"text\": \"replacement prose\", \"evidence_metrics\": [\"exact source metric name\"]}]}. evidence_metrics must be an array of exact source metric names for that paragraph; for title edits omit it. Use only supplied target IDs and that row's source metric pool. Use at most ONE short numerical fact per paragraph; keep other evidence implicit rather than repeating values. Preserve supported facts, identities and units. Resolve the stated contradictions with the other sections, without manufacturing a contrast. Reword unsupported claims to conclusions the evidence can support. Remove unsupported clauses silently: never add caveats, missing-data explanations or statements about what statistics cannot prove. Use the row source pool to select a more diagnostic count/rate/outcome relationship when necessary; do not merely define metrics or repeat the same finding across both paragraphs. Keep unaffected passages unchanged. Target 40-50 words for team paragraphs, 20-30 for player paragraphs and 2-4 for titles; word count is a target. Explicit task_contract units and paragraph limits override these generic targets. The union of paragraph references must fit that row's card limit. Never return generic substitute paragraphs or metric values." + FOOTBALL_TERMINOLOGY_GUIDANCE),
                    ("human", json.dumps({"language": lang, "passages": passages, "sections": result, "task_contract": task_contract}, ensure_ascii=False, default=str)),
                ]), "corrections")
                items = corrections.get("corrections", corrections)
                # Accept both an array and an ID-keyed object. Field-level
                # allowlists and source checks remain identical for both.
                if isinstance(items, dict):
                    items = [{**value, "target": key} if isinstance(value, dict)
                             else {"target": key, "text": value}
                             for key, value in items.items()]
                if not isinstance(items, list):
                    raise ValueError("Missing corrections array")
                pending, queued = {}, set()
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    key, text = item.get("target"), item.get("text")
                    if not isinstance(key, str) or key not in flagged or key in queued or not isinstance(text, str) or not text.strip():
                        continue
                    row, field, bullet = targets[key]
                    if prose_issues(text, lang, analysis=field == "analysis"):
                        continue
                    refs = None
                    if bullet is not None:
                        refs = item.get("evidence_metrics", row["evidence_metrics"][bullet])
                        # The generation contract is paragraph-aligned; a model
                        # may keep that single-paragraph wrapper in a repair.
                        if isinstance(refs, list) and len(refs) == 1 and isinstance(refs[0], list):
                            refs = refs[0]
                        source = row.get("_source_metrics") or {}
                        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in source for ref in refs):
                            continue
                    entry = pending.setdefault(id(row), (row, deepcopy(row), []))
                    candidate, keys = entry[1], entry[2]
                    if bullet is None:
                        candidate[field] = text.strip()
                    else:
                        candidate["analysis"][bullet] = text.strip()
                        candidate["evidence_metrics"][bullet] = list(dict.fromkeys(refs))
                    keys.append(key)
                    queued.add(key)
                # Bind a row's edits together so two paragraphs can change their
                # evidence without an intermediate card union rejecting one.
                for row, candidate, keys in pending.values():
                    bound = bind_row_evidence(candidate, row.get("_source_metrics") or {}, row.get("_metric_limit", 4))
                    if len(bound["evidence_values"]) > row.get("_metric_limit", 4):
                        continue
                    row.update(bound)
                    accepted.update(keys)
            except Exception as exc:
                print(f"[enterprise_team_report] event=joint_repair_original_retained error_type={type(exc).__name__}")

    for section, profile in result.items():
        profile["editorial_review"] = {
            "review_succeeded": review_succeeded,
            "issues": [{"target": key, "reasons": reasons, "resolved": key in accepted}
                       for key, reasons in flagged.items() if key.startswith(section + ".")],
        }
        for kind in ("themes", "players"):
            for row in profile.get(kind) or []:
                row.pop("_source_metrics", None)
                row.pop("_metric_limit", None)
    print(f"[enterprise_team_report] event=joint_profile_review flagged={len(flagged)} repaired={len(accepted)} originals_retained={len(flagged)-len(accepted)}")
    return result
