from __future__ import annotations

from typing import Any, Dict
import re

from sqlalchemy import text
from sqlalchemy.orm import Session


TURKISH_CHAR_MAP_FROM = "çğıöşüÇĞİÖŞÜIİı"
TURKISH_CHAR_MAP_TO = "cgiosuCGIOSUiii"


def clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def numeric_filter_sql(field_name: str, param_name: str, operator: str) -> str:
    value_expr = f"""
    CASE
        WHEN COALESCE(metadata->>'{field_name}', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
            THEN (metadata->>'{field_name}')::numeric
        ELSE NULL
    END
    """
    return f"(:{param_name} IS NULL OR ({value_expr}) {operator} :{param_name})"


def folded_text_sql(field_name: str) -> str:
    return f"LOWER(TRANSLATE(COALESCE(metadata->>'{field_name}', ''), '{TURKISH_CHAR_MAP_FROM}', '{TURKISH_CHAR_MAP_TO}'))"


def clamp_potential(value: int) -> int:
    return max(0, min(100, int(value)))


def parse_potential_value(raw_output: str) -> int:
    match = re.search(r"\b(\d{1,3})\b", raw_output or "")
    if not match:
        raise RuntimeError("Potential model did not return a valid integer")
    return clamp_potential(int(match.group(1)))


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


def clean_metadata_for_potential(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in metadata.items()
        if not is_missing_value(value)
    }
