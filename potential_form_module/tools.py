from __future__ import annotations

from typing import Any, Dict
import re

from sqlalchemy import text
from sqlalchemy.orm import Session


def clamp_score(value: int) -> int:
    return max(0, min(100, int(value)))


def parse_score_value(raw_output: str, score_name: str) -> int:
    match = re.search(r"\b(\d{1,3})\b", raw_output or "")
    if not match:
        raise RuntimeError(f"{score_name} model did not return a valid integer")
    return clamp_score(int(match.group(1)))


def clamp_potential(value: int) -> int:
    return max(30, clamp_score(value))


def parse_potential_value(raw_output: str) -> int:
    match = re.search(r"\b(\d{1,3})\b", raw_output or "")
    if not match:
        raise RuntimeError("Potential model did not return a valid integer")
    return clamp_potential(int(match.group(1)))


def parse_form_value(raw_output: str) -> int:
    return parse_score_value(raw_output, "Form")


def get_player_metadata_by_id(db: Session, player_id: int | str) -> Dict[str, Any]:
    row = db.execute(
        text("""
            SELECT metadata
            FROM player_data
            WHERE id = :id
            LIMIT 1
        """),
        {"id": player_id},
    ).mappings().first()

    if not row or not row.get("metadata"):
        raise ValueError("Player not found")

    return row["metadata"]


def is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def clean_metadata_for_score(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in metadata.items()
        if not is_missing_value(value)
    }


def clean_metadata_for_potential(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return clean_metadata_for_score(metadata)


def clean_metadata_for_form(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return clean_metadata_for_score(metadata)


def get_cached_player_pool_score(metadata: Dict[str, Any], field_name: str) -> int | None:
    raw_value = metadata.get(field_name)
    if raw_value is None:
        return None

    if isinstance(raw_value, (int, float)):
        value = clamp_score(int(raw_value))
        return value if value > 0 else None

    if isinstance(raw_value, str):
        match = re.search(r"\b(\d{1,3})\b", raw_value)
        if not match:
            return None
        value = clamp_score(int(match.group(1)))
        return value if value > 0 else None

    return None


def get_cached_player_pool_potential(metadata: Dict[str, Any]) -> int | None:
    value = get_cached_player_pool_score(metadata, "potential")
    return clamp_potential(value) if value is not None else None


def get_cached_player_pool_form(metadata: Dict[str, Any]) -> int | None:
    return get_cached_player_pool_score(metadata, "form")


def save_player_pool_score(
    db: Session,
    player_id: int | str,
    field_name: str,
    score: int,
) -> None:
    if field_name not in {"potential", "form"}:
        raise ValueError("Unsupported player score field")

    db.execute(
        text(f"""
            UPDATE player_data
            SET metadata = jsonb_set(
                COALESCE(metadata::jsonb, '{{}}'::jsonb),
                '{{{field_name}}}',
                to_jsonb(CAST(:score AS integer)),
                true
            )
            WHERE id = :id
        """),
        {"id": player_id, "score": int(score)},
    )
    db.commit()


def save_player_pool_potential(db: Session, player_id: int | str, potential: int) -> None:
    save_player_pool_score(db, player_id, "potential", clamp_potential(potential))


def save_player_pool_form(db: Session, player_id: int | str, form: int) -> None:
    save_player_pool_score(db, player_id, "form", form)
