from __future__ import annotations

from typing import Any, Dict
import json

from langchain_deepseek import ChatDeepSeek
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy.orm import Session

from potential_form_module.prompts import player_pool_form_system_prompt
from potential_form_module.tools import (
    clean_metadata_for_form,
    get_cached_player_pool_form,
    get_player_metadata_by_id,
    parse_form_value,
    save_player_pool_form,
)


CHAT_LLM = ChatDeepSeek(
    model="deepseek-chat",
    temperature=0.3,
)

_form_prompt = ChatPromptTemplate.from_messages([
    ("system", player_pool_form_system_prompt),
    ("human", "PLAYER_METADATA_JSON:\n{player_metadata_json}"),
])


def reveal_player_form(db: Session, player_id: int | str) -> Dict[str, Any]:
    full_metadata = get_player_metadata_by_id(db, player_id)
    cached_form = get_cached_player_pool_form(full_metadata)

    if cached_form is not None:
        return {
            "player_id": str(player_id),
            "status": "ready",
            "form": cached_form,
            "source": "db",
        }

    metadata = clean_metadata_for_form(full_metadata)
    metadata_json = json.dumps(metadata, ensure_ascii=False, default=str)
    prompt_messages = _form_prompt.format_messages(player_metadata_json=metadata_json)

    raw_msg = CHAT_LLM.invoke(prompt_messages)
    raw_output = getattr(raw_msg, "content", "") or ""
    form = parse_form_value(raw_output)
    save_player_pool_form(db, player_id, form)
    return {
        "player_id": str(player_id),
        "status": "ready",
        "form": form,
        "source": "model",
    }
