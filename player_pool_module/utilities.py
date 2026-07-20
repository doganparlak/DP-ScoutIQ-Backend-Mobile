from __future__ import annotations

from constants_module.constants import ROLE_SEARCH_SHORT_ALIASES, ROLE_SHORT_TO_LONG

FOLD_CHAR_MAP_FROM = (
    "çğıöşüÇĞİÖŞÜIİı"
    "áàâäãåāăąÁÀÂÄÃÅĀĂĄ"
    "éèêëēĕėęěÉÈÊËĒĔĖĘĚ"
    "íìîïīĭįİÍÌÎÏĪĬĮ"
    "óòôöõøōŏőÓÒÔÖÕØŌŎŐ"
    "úùûüūŭůűųÚÙÛÜŪŬŮŰŲ"
    "ñÑćčĆČłŁńŃřŘśšŚŠýÿÝŸžźżŽŹŻ"
)
FOLD_CHAR_MAP_TO = (
    "cgiosuCGIOSUiii"
    "aaaaaaaaaAAAAAAAAA"
    "eeeeeeeeeEEEEEEEEE"
    "iiiiiiiiIIIIIII"
    "oooooooooOOOOOOOOO"
    "uuuuuuuuuUUUUUUUUU"
    "nNccCClLnNrRssSSyyYYzzzZZZ"
)

SEARCH_LIMIT = 100


def clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def norm_name(value: str | None) -> str:
    if not value:
        return ""
    translated = value.translate(str.maketrans(FOLD_CHAR_MAP_FROM, FOLD_CHAR_MAP_TO))
    return " ".join(translated.lower().split())


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
    return (
        "LOWER(TRANSLATE("
        f"COALESCE(metadata->>'{field_name}', ''), "
        f"'{FOLD_CHAR_MAP_FROM}', '{FOLD_CHAR_MAP_TO}'"
        "))"
    )


def role_value_short_sql(value_expr: str) -> str:
    position = f"LOWER(TRIM(COALESCE({value_expr}, '')))"
    return f"""
    CASE
        WHEN {position} IN ('g', 'gk', 'goalkeeper', 'goal keeper') THEN 'GK'
        WHEN {position} = 'lwb' THEN 'LWB'
        WHEN {position} = 'left wing back' THEN 'LWB'
        WHEN {position} = 'lb' THEN 'LB'
        WHEN {position} = 'left back' THEN 'LB'
        WHEN {position} = 'lcb' THEN 'LCB'
        WHEN {position} = 'left center back' THEN 'LCB'
        WHEN {position} IN ('cb', 'center back', 'centre back') THEN 'CB'
        WHEN {position} = 'rcb' THEN 'RCB'
        WHEN {position} = 'right center back' THEN 'RCB'
        WHEN {position} = 'rb' THEN 'RB'
        WHEN {position} = 'right back' THEN 'RB'
        WHEN {position} = 'rwb' THEN 'RWB'
        WHEN {position} = 'right wing back' THEN 'RWB'
        WHEN {position} = 'lm' THEN 'LM'
        WHEN {position} = 'left midfield' THEN 'LM'
        WHEN {position} = 'ldm' THEN 'LDM'
        WHEN {position} = 'left defensive midfield' THEN 'LDM'
        WHEN {position} = 'lcm' THEN 'LCM'
        WHEN {position} = 'left center midfield' THEN 'LCM'
        WHEN {position} = 'lam' THEN 'LAM'
        WHEN {position} = 'left attacking midfield' THEN 'LAM'
        WHEN {position} IN ('cm', 'center midfield', 'central midfield') THEN 'CM'
        WHEN {position} IN ('cam', 'center attacking midfield', 'attacking midfield') THEN 'CAM'
        WHEN {position} IN ('cdm', 'center defensive midfield', 'defensive midfield') THEN 'CDM'
        WHEN {position} = 'rdm' THEN 'RDM'
        WHEN {position} = 'right defensive midfield' THEN 'RDM'
        WHEN {position} = 'rcm' THEN 'RCM'
        WHEN {position} = 'right center midfield' THEN 'RCM'
        WHEN {position} = 'ram' THEN 'RAM'
        WHEN {position} = 'right attacking midfield' THEN 'RAM'
        WHEN {position} = 'rm' THEN 'RM'
        WHEN {position} = 'right midfield' THEN 'RM'
        WHEN {position} IN ('a', 'f', 'cf', 'center forward', 'centre forward', 'attacker', 'forward') THEN 'CF'
        WHEN {position} = 'rcf' THEN 'RCF'
        WHEN {position} = 'right center forward' THEN 'RCF'
        WHEN {position} = 'lcf' THEN 'LCF'
        WHEN {position} = 'left center forward' THEN 'LCF'
        WHEN {position} = 'lw' THEN 'LW'
        WHEN {position} = 'left wing' THEN 'LW'
        WHEN {position} = 'rw' THEN 'RW'
        WHEN {position} = 'right wing' THEN 'RW'
        ELSE UPPER(TRIM(COALESCE({value_expr}, '')))
    END
    """


def role_short_sql() -> str:
    position = "LOWER(COALESCE(metadata->>'position_name', ''))"
    return f"""
    CASE
        WHEN {position} IN ('g', 'gk', 'goalkeeper', 'goal keeper') THEN 'GK'
        WHEN {position} = 'left wing back' THEN 'LWB'
        WHEN {position} = 'left back' THEN 'LB'
        WHEN {position} = 'left center back' THEN 'LCB'
        WHEN {position} IN ('center back', 'centre back') THEN 'CB'
        WHEN {position} = 'right center back' THEN 'RCB'
        WHEN {position} = 'right back' THEN 'RB'
        WHEN {position} = 'right wing back' THEN 'RWB'
        WHEN {position} = 'left midfield' THEN 'LM'
        WHEN {position} = 'left defensive midfield' THEN 'LDM'
        WHEN {position} = 'left center midfield' THEN 'LCM'
        WHEN {position} = 'left attacking midfield' THEN 'LAM'
        WHEN {position} IN ('center midfield', 'central midfield') THEN 'CM'
        WHEN {position} IN ('center attacking midfield', 'attacking midfield') THEN 'CAM'
        WHEN {position} IN ('center defensive midfield', 'defensive midfield') THEN 'CDM'
        WHEN {position} = 'right defensive midfield' THEN 'RDM'
        WHEN {position} = 'right center midfield' THEN 'RCM'
        WHEN {position} = 'right attacking midfield' THEN 'RAM'
        WHEN {position} = 'right midfield' THEN 'RM'
        WHEN {position} IN ('a', 'f', 'center forward', 'centre forward', 'attacker', 'forward') THEN 'CF'
        WHEN {position} = 'right center forward' THEN 'RCF'
        WHEN {position} = 'left center forward' THEN 'LCF'
        WHEN {position} = 'left wing' THEN 'LW'
        WHEN {position} = 'right wing' THEN 'RW'
        ELSE metadata->>'position_name'
    END
    """
