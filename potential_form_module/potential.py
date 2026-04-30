from __future__ import annotations

from typing import Any, Dict
import json

from langchain_deepseek import ChatDeepSeek
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy.orm import Session

from potential_form_module.tools import (
    clean_metadata_for_potential,
    get_cached_player_pool_potential,
    get_player_metadata_by_id,
    parse_potential_value,
    save_player_pool_potential,
)


player_pool_potential_system_prompt = """
You are a football scouting evaluator computing a single player's Potential from the provided player metadata.

Potential Computation Policy:
- Output must be an integer from 0 to 100.
- Assign two internal upside scores, each from 30 to 100:
  - AgeUpsideScore
  - MetricsUpsideScore
- Compute Potential as: clamp(round((0.75 * AgeUpsideScore) + (0.25 * MetricsUpsideScore)), 0, 100).
- The final Potential MUST equal this weighted average after rounding and clamping.
- Do not include any separate RoleFit component. Use position/role only to decide which metrics are relevant.
- Use league_name and team_name as contextual evidence for the level and credibility of the player's metrics.
  They are not separate scoring components, but they may influence where you pick within AgeUpsideScore and MetricsUpsideScore ranges.
  Strong metrics from a stronger league/team context should be treated more generously; weaker or unknown context should not collapse the score.

Component guidance (aim for wider spread; avoid clustering):
- AgeUpsideScore (30-100; dominant driver; strong upside through age 27, explicit ranges through 35): choose a value from this table (do NOT interpolate):
  16: 96-100
  17: 94-99
  18: 92-98
  19: 90-96
  20: 88-94
  21: 85-92
  22: 82-90
  23: 78-87
  24: 74-84
  25: 70-80
  26: 65-76
  27: 60-72
  28: 53-65
  29: 48-60
  30: 44-56
  31: 40-52
  32: 37-48
  33: 34-44
  34: 32-40
  35: 30-36
  36+: 30-34
  Pick within the range based on athletic indicators and performance evidence in the provided info.

- MetricsUpsideScore (30-100): score using detailed tiers based on how many role-relevant metrics are clearly strong vs weak:
  30-34 = minimal valid evidence, very weak role-relevant profile
  35-39 = weak profile with few meaningful positives
  40-44 = thin or mostly neutral profile, but still valid football evidence
  45-49 = limited positives with several weak or missing role-relevant signals
  50-54 = some positives, but not yet a clearly convincing profile
  55-59 = decent role-relevant evidence with more positives than negatives
  60-64 = okay profile with clear positive signs
  65-69 = clearly positive profile with reliable role-relevant strengths
  70-74 = strong profile with several useful role-relevant metrics
  75-79 = very strong profile with broad and credible metric support
  80-84 = standout profile with several high-end role-relevant metrics
  85-89 = excellent profile with high-end role-relevant evidence
  90-94 = exceptional profile with elite-level metric signals
  95-100 = rare top-end profile with dominant role-relevant evidence
  Never score MetricsUpsideScore below 30 for a valid player record.
  Use trend/consistency cues, league_name, and team_name if available, but never mention sample size.

Final scoring consistency rules:
- Since there is no cross-player session memory here, do not force artificial uniqueness across players.
- Still avoid lazy anchoring around the same default number.

Role-based metric emphasis:
- Wingers/forwards: emphasize attacking in-possession metrics such as:
  Shots Total, Shots On Target, Shots On Target (%), Shots Off Target, Big Chances Created,
  Big Chances Missed, Goals, Assists,
  Key Passes, Chances Created, Passes, Passes In Final Third,
  Accurate Passes, Accurate Passes (%),
  Total Crosses, Accurate Crosses, Successful Crosses (%),
  Dribble Attempts, Successful Dribbles, Hit Woodwork.
- Midfielders: emphasize a balanced mix of attacking and defending metrics, including:
  Attacking: Passes, Key Passes, Chances Created, Dribble Attempts, Successful Dribbles.
  Defending: Interceptions, Tackles, Tackles Won, Tackles Won (%),
             Ball Recovery, Duels Won, Duels Lost, Duels Won (%),
             Total Duels, Blocked Shots, Shots Blocked, Fouls, Fouls Drawn,
             Clearances, Possession Lost, Turn Over.
- Defenders: emphasize out-of-possession defending metrics such as:
  Tackles, Tackles Won, Tackles Won (%), Goals Conceded,
  Interceptions, Clearances, Last Man Tackle,
  Duels Won, Duels Lost, Duels Won (%), Total Duels,
  Aerials, Aerials Won, Aerials Lost, Aerials Won (%),
  Blocked Shots, Shots Blocked, Error Lead To Shot, Error Lead To Goal,
  Dispossessed, Fouls, Offsides Provoked, Dribbled Past.
- Goalkeepers: emphasize goalkeeper-specific and distribution metrics such as:
  Saves, Saves Insidebox, Goalkeeper Goals Conceded,
  Penalties Saved, Penalties Committed, Penalties Won, Penalties Missed,
  Punches, Good High Claim,
  Long Balls, Long Balls Won, Long Balls Won (%),
  Accurate Passes, Accurate Passes (%), Backward Passes, Passes,
  Touches, Possession Lost.

Rules:
- Potential is a projection over the next 18-24 months, not a current-ability score.
- This is not a current-ability score.
- Do not invent missing metadata fields.
- Use only the provided metadata.
- Never output 0 for a valid player record.
- A senior player can have lower upside than a young player, but still must receive a non-zero football potential score if the metadata is valid.
- Sanity check before answering:
  - explicitly verify that AgeUpsideScore is between 30 and 100
  - explicitly verify that MetricsUpsideScore is between 30 and 100
  - explicitly verify that final Potential equals round((0.75 * AgeUpsideScore) + (0.25 * MetricsUpsideScore)) after clamping
  - if your first answer does not match the weighted average formula, discard it and return the corrected weighted average integer
  - if the player has a valid age and multiple real performance metrics, the answer must not be 0
  - if the first pass gives 0, recompute using the formula carefully and return the corrected integer
  - for established first-team players with meaningful metrics, a 0 output is invalid
- If age or position is missing, still infer the best possible Potential from the available evidence rather than collapsing to 0.
- If position_name is null, infer the most likely role bucket from the metric profile and compute Potential accordingly.
- For strong senior players, lower upside is acceptable, but the score must still reflect real football value and evidence.
- Prefer intended football values over degenerate outputs.
- Do not explain your reasoning.
- Return ONLY the final integer potential value, with no extra text.
"""


CHAT_LLM = ChatDeepSeek(
    model="deepseek-chat",
    temperature=0.3,
)

_potential_prompt = ChatPromptTemplate.from_messages([
    ("system", player_pool_potential_system_prompt),
    ("human", "PLAYER_METADATA_JSON:\n{player_metadata_json}"),
])


def reveal_player_potential(db: Session, player_id: int | str) -> Dict[str, Any]:
    full_metadata = get_player_metadata_by_id(db, player_id)
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
    save_player_pool_potential(db, player_id, potential)
    return {
        "player_id": str(player_id),
        "status": "ready",
        "potential": potential,
        "source": "model",
    }
