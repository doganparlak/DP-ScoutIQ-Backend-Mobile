"""Structured profile generation with one bounded retry, never template prose."""
import json
import os
import re


def _profile_object(raw: str, require_players: bool) -> dict:
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    # Retain usable JSON even if a provider adds a preamble or trailing text.
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw):
        try:
            value, _ = decoder.raw_decode(raw[match.start():])
        except ValueError:
            continue
        if (isinstance(value, dict) and isinstance(value.get("themes"), list)
                and (not require_players or isinstance(value.get("players"), list))):
            return value
    raise ValueError("Profile response has no usable JSON object")


def generate_profile_json(messages: list, *, require_players: bool = False) -> dict:
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model=os.getenv("OPENAI_MATCH_REPORT_MODEL", os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-luna")),
        api_key=os.environ["OPENAI_API_KEY"], temperature=0.25,
        max_retries=0,
        model_kwargs={"response_format": {"type": "json_object"}},
    )
    raw, usable = "", None
    for attempt in range(2):
        try:
            request = messages
            if attempt and raw:
                request = [*messages, ("assistant", raw), ("human",
                    "Keep the generated findings and original source evidence. Correct only the response structure: return one valid JSON object with themes as an array"
                    + (" and players as an array" if require_players else "")
                    + ". Do not return a template, commentary or markdown.")]
            response = model.invoke(request)
            content = response.content
            if isinstance(content, list):
                content = "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
            raw = str(content or "")
            return _profile_object(raw, require_players)
        except Exception as exc:
            if raw:
                try:
                    usable = _profile_object(raw, False)
                except ValueError:
                    pass
            print(f"[enterprise_team_report] event=profile_generation_attempt_failed attempt={attempt + 1} error_type={type(exc).__name__}", flush=True)
            if attempt:
                if usable is not None:
                    # Preserve the first usable generated themes when a retry
                    # cannot supply an optional/malformed player array.
                    if require_players and not isinstance(usable.get("players"), list):
                        usable["players"] = []
                    print("[enterprise_team_report] event=profile_original_retained", flush=True)
                    return usable
                raise
    raise RuntimeError("Profile generation did not return content")
