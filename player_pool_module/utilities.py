from __future__ import annotations


TURKISH_CHAR_MAP_FROM = "çğıöşüÇĞİÖŞÜIİı"
TURKISH_CHAR_MAP_TO = "cgiosuCGIOSUiii"


def clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def player_pool_table(world_cup_mode: bool = False) -> str:
    return "player_data_wc" if world_cup_mode else "player_data"


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
