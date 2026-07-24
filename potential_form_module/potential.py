from __future__ import annotations

from typing import Any, Dict
import json
import os

from langchain_deepseek import ChatDeepSeek
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy.orm import Session

from potential_form_module.prompts import player_pool_potential_system_prompt
from potential_form_module.tools import (
    clean_metadata_for_potential,
    get_cached_player_pool_potential,
    get_player_metadata_by_id,
    parse_potential_value,
    save_player_pool_potential,
)


CHAT_LLM = ChatDeepSeek(
    model=os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-v4-flash"),
    temperature=0.3,
)

_potential_prompt = ChatPromptTemplate.from_messages([
    ("system", player_pool_potential_system_prompt),
    ("human", "PLAYER_METADATA_JSON:\n{player_metadata_json}"),
])


def reveal_player_potential(db: Session, player_id: int | str, world_cup_mode: bool = False) -> Dict[str, Any]:
    full_metadata = get_player_metadata_by_id(db, player_id, world_cup_mode)
    cached_potential = get_cached_player_pool_potential(full_metadata)

    if cached_potential is not None:
        return {
            "player_id": str(player_id),
            "status": "ready",
            "potential": cached_potential,
            "source": "db",
        }

    metadata = clean_metadata_for_potential(full_metadata)
    metadata_json = json.dumps(metadata, ensure_ascii=False, default=str)
    prompt_messages = _potential_prompt.format_messages(player_metadata_json=metadata_json)

    raw_msg = CHAT_LLM.invoke(prompt_messages)
    raw_output = getattr(raw_msg, "content", "") or ""
    potential = parse_potential_value(raw_output)
    save_player_pool_potential(db, player_id, potential, world_cup_mode)
    return {
        "player_id": str(player_id),
        "status": "ready",
        "potential": potential,
        "source": "model",
    }
