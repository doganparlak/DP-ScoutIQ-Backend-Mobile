# ingest_supabase_small.py
from __future__ import annotations

import os
from typing import List, Tuple, Dict, Any

from dotenv import load_dotenv
import pandas as pd
from supabase import create_client, Client
from langchain_openai import OpenAIEmbeddings
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

# -------------------------------------------------------------------
# Environment & clients
# -------------------------------------------------------------------
load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]  # used implicitly by langchain_openai

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Smaller, cheaper embedding model (must match vector(1536) in documents_v4)
emb = OpenAIEmbeddings(
    model="text-embedding-3-small",
    dimensions=1536,
)

# -------------------------------------------------------------------
# Load CSV
# -------------------------------------------------------------------
# New schema CSV (with gender/height/weight/age/nationality_name/position_name)
CSV_PATH = "player_level_stats_wwa.csv"
df = pd.read_csv(CSV_PATH)

# Ensure expected core columns exist
REQUIRED_COLS = [
    "player_name",
    "gender",
    "age",
    "match_count",
    "nationality_name",
    "position_name",
    "team_name",
]
missing = [c for c in REQUIRED_COLS if c not in df.columns]
if missing:
    raise ValueError(f"Missing required columns in CSV: {missing}")


# -------------------------------------------------------------------
# Row -> (content, metadata)
# -------------------------------------------------------------------
def row_to_doc(row: pd.Series) -> Tuple[str, Dict[str, Any]]:
    """
    Turn one row into a text 'document' + metadata dict.

    - content: human-readable lines like "stat_goals: 3", "team_name: Flamengo"
    - metadata: structured fields for filtering & debugging
    """
    non_zero_info: List[str] = []
    numeric_metadata: Dict[str, float | int] = {}

    for col, val in row.items():
        if pd.isna(val):
            continue

        # strings
        if isinstance(val, str):
            if val.strip() and val.lower() != "nan":
                non_zero_info.append(f"{col}: {val}")
            continue

        # numerics (ints/floats)
        if isinstance(val, (int, float)):
            # skip zeros to reduce noise in the content text
            if val != 0:
                non_zero_info.append(f"{col}: {val}")
                numeric_metadata[col] = val
            continue

        # Fallback: try casting to float
        try:
            fval = float(val)
        except (TypeError, ValueError):
            continue

        if fval != 0.0:
            non_zero_info.append(f"{col}: {fval}")
            numeric_metadata[col] = fval

    content = "\n".join(non_zero_info)

    # Core metadata (ids + label fields)
    # We no longer have a numeric player_id in this CSV, so we treat name+team as identity.
    metadata: Dict[str, Any] = {
        "player_name": str(row["player_name"]),
        "gender": str(row["gender"]) if not pd.isna(row["gender"]) else None,
        "age": int(row["age"]) if not pd.isna(row["age"]) else None,
        "match_count": int(row["match_count"])
        if not pd.isna(row["match_count"])
        else None,
        "nationality_name": str(row["nationality_name"])
        if not pd.isna(row["nationality_name"])
        else None,
        "position_name": str(row["position_name"])
        if not pd.isna(row["position_name"])
        else None,
        "team_name": str(row["team_name"]),
        # Handy composite key if you ever want to dedupe
        "player_key": f"{row['player_name']}|{row['team_name']}",
    }

    # Merge in all numeric stats (including height, weight, rating, etc.)
    metadata.update(numeric_metadata)

    return content, metadata


records: List[Tuple[str, Dict[str, Any]]] = [
    row_to_doc(r) for _, r in df.iterrows()
]


# -------------------------------------------------------------------
# Robust embedding with retries
# -------------------------------------------------------------------
class TransientOpenAIError(Exception):
    pass


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1.0, min=1, max=15),
    retry=retry_if_exception_type(TransientOpenAIError),
)
def embed_batch(texts: List[str]) -> List[List[float]]:
    try:
        return emb.embed_documents(texts)
    except Exception as e:
        msg = str(e).lower()
        # retry on likely transient issues
        if any(k in msg for k in ["rate", "timeout", "overloaded", "connection", "temporar"]):
            raise TransientOpenAIError(e)
        raise


# -------------------------------------------------------------------
# Ingest into Supabase
# -------------------------------------------------------------------
BATCH = 100
total = len(records)
print(
    f"Preparing to embed & insert {total} rows into documents_v4 "
    f"using text-embedding-3-small (1536-dim)…"
)

for i in range(0, total, BATCH):
    batch = records[i : i + BATCH]
    contents = [c for c, _ in batch]
    metas = [m for _, m in batch]

    print(f"[{i:05d}] Embedding {len(batch)} texts…")

    embeddings = embed_batch(contents)

    # NOTE: we don't send 'id' here; let Postgres bigserial handle it.
    payload = [
        {
            "content": c,
            "metadata": m,
            "embedding": e,  # Supabase/pgvector accepts JSON float arrays
        }
        for c, m, e in zip(contents, metas, embeddings)
    ]

    try:
        res = supabase.table("documents_v4").insert(payload).execute()
        rows = getattr(res, "data", None)

        if rows:
            print(f"[{i:05d}] Inserted {len(rows)} rows (up to {i + len(rows) - 1}).")
        else:
            print(
                f"[{i:05d}] Insert attempted; no 'data' returned. "
                f"Check RLS / triggers / PostgREST config."
            )
    except Exception as e:
        print(f"[{i:05d}] ERROR inserting batch: {e}")
