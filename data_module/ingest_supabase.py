from __future__ import annotations

import os, time, math, json
from typing import List, Tuple, Dict, Any
from dotenv import load_dotenv

import pandas as pd
from supabase import create_client, Client
from langchain_openai import OpenAIEmbeddings
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

load_dotenv()
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
emb = OpenAIEmbeddings(model="text-embedding-3-large", dimensions=3072)

df = pd.read_csv("data_module/player_level_stats.csv")

def row_to_doc(row):
    non_zero_info, numeric_metadata = [], {}
    for col, val in row.items():
        if pd.isna(val):
            continue
        if isinstance(val, str) and val.strip() and val.lower() != "nan":
            non_zero_info.append(f"{col}: {val}")
        elif isinstance(val, (int, float)) and val != 0:
            non_zero_info.append(f"{col}: {val}")
            numeric_metadata[col] = val
    content = "\n".join(non_zero_info)
    metadata = {
        "id": str(row["player_id"]),
        "name": row["player_name"],
        "nationality": row.get("nationality"),
        "position": row.get("position"),
    }
    metadata.update(numeric_metadata)
    return content, metadata

records = [row_to_doc(r) for _, r in df.iterrows()]

# ---- Robust embedding with retries ----
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
        # Heuristic: retry on likely transient errors
        msg = str(e).lower()
        if any(k in msg for k in ["rate", "timeout", "overloaded", "connection", "temporar"]):
            raise TransientOpenAIError(e)
        raise

BATCH = 100
total = len(records)
print(f"Preparing to embed & insert {total} rows into documents_v3 …")

for i in range(0, total, BATCH):
    batch = records[i:i + BATCH]
    contents = [c for c, _ in batch]
    metas = [m for _, m in batch]

    print(f"[{i:05d}] Embedding {len(batch)} texts …")

    embs = emb.embed_documents(contents)
    payload = [
        {
            "content": c,
            "metadata": m,
            "embedding": e,  # PostgREST will accept JSON float arrays; pgvector casts
        }
        for c, m, e in zip(contents, metas, embs)
    ]

    # insert to Supabase
    try:
        res = supabase.table("documents_v3").insert(payload).execute()
        rows = getattr(res, "data", None)
        if rows:
            print(f"[{i:05d}] Inserted {len(rows)} rows (up to {i+len(rows)-1}).")
        else:
            # Some PostgREST configs return null data on insert; still warn
            print(f"[{i:05d}] Insert attempted; no 'data' returned. Check RLS or triggers.")
    except Exception as e:
        print(f"[{i:05d}] ERROR inserting batch: {e}")
