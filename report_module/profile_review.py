"""Deterministic wording checks used by the joint profile editor."""
import re


def prose_issues(text: str, lang: str, *, analysis: bool) -> list[str]:
    issues = []
    if lang == "tr":
        if re.search(r"\b(?:hacim\w*|hacm\w*|sıkl[ıi][kğ]\w*|dikey\w*)\b|ikinci dalga", text, re.I):
            issues.append("Use natural Turkish football wording; remove prohibited expressions.")
        if re.search(r"\b(?:Clearances|Aerials Won|Duels Won|Tackles Won|Shots On Target|Key Passes|Dribble Attempts|Successful Dribbles)\b", text):
            issues.append("Translate English metric references inside prose into Turkish.")
    if re.search(r"(?<!\d)\d+[.,]\d{3,}(?!\d)", text):
        issues.append("Display metric values with at most two decimal places.")
    if analysis and len(re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)?", text)) > 1:
        issues.append("Keep at most one short numerical fact; prioritize interpretation.")
    # Word counts are prompt targets, never a reason to discard usable prose.
    return issues
