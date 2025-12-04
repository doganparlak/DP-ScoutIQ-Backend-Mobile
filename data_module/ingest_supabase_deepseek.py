# ingest_supabase_deepseek.py
from __future__ import annotations

import os
from typing import List, Tuple

import pandas as pd
import requests
from dotenv import load_dotenv
from supabase import create_client, Client
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type


# ------------------ Config & setup ------------------

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

# DeepSeek-specific env vars
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
DEEPSEEK_EMBEDDING_MODEL = os.getenv("DEEPSEEK_EMBEDDING_MODEL", "deepseek-embedding-v2")

# For deepseek-embedding-v2 the dimensionality is 768
# (make sure documents_v4.embedding is vector(768) in Postgres/pgvector)
DEEPSEEK_EMBEDDING_DIM = 768

CSV_PATH = "player_level_stats_wwa.csv"  # adjust if your path differs
TABLE_NAME = "documents_v4"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def log_env():
    print("==== ENV CHECK ====")
    print(f"SUPABASE_URL                = {SUPABASE_URL}")
    print(f"SUPABASE_SERVICE_ROLE_KEY   = {SUPABASE_KEY[:6]}... (len={len(SUPABASE_KEY)})")
    print(f"DEEPSEEK_API_KEY            = {DEEPSEEK_API_KEY[:6]}... (len={len(DEEPSEEK_API_KEY)})")
    print(f"DEEPSEEK_API_BASE           = {DEEPSEEK_API_BASE!r}")
    print(f"DEEPSEEK_EMBEDDING_MODEL    = {DEEPSEEK_EMBEDDING_MODEL!r}")
    print("====================")


def build_embeddings_url() -> str:
    """
    Build the final embeddings URL based on DEEPSEEK_API_BASE.

    Supported patterns:
    - https://api.deepseek.com                -> https://api.deepseek.com/v1/embeddings
    - https://api.deepseek.com/v1            -> https://api.deepseek.com/v1/embeddings
    - https://api.deepseek.com/v1/embeddings -> stays as is
    - any-other-base                         -> base + "/embeddings" if it already ends with /v1
                                               else base + "/v1/embeddings"
    """
    base = DEEPSEEK_API_BASE.rstrip("/")

    if base.endswith("/v1/embeddings") or base.endswith("/embeddings"):
        url = base
    elif base.endswith("/v1"):
        url = base.rsplit("/v1", 1)[0] + "/embeddings"
    else:
        # Assume pure host, so add /v1/embeddings
        url = base + "/embeddings"

    print(f"[URL BUILDER] DEEPSEEK_API_BASE={DEEPSEEK_API_BASE!r} -> embeddings URL={url!r}")
    return url


# ------------------ Data to document conversion ------------------

def row_to_doc(row: pd.Series) -> Tuple[str, dict]:
    """
    Convert a player row to (content, metadata) for RAG.
    - content: human-readable text to embed
    - metadata: structured fields stored alongside the embedding
    """

    non_zero_info: List[str] = []
    numeric_metadata = {}

    for col, val in row.items():
        if pd.isna(val):
            continue

        # Keep strings that are non-empty and not the literal "nan"
        if isinstance(val, str):
            if val.strip() and val.strip().lower() != "nan":
                non_zero_info.append(f"{col}: {val}")
        # Keep non-zero numeric values
        elif isinstance(val, (int, float)) and val != 0:
            non_zero_info.append(f"{col}: {val}")
            numeric_metadata[col] = val

    content = "\n".join(non_zero_info)

    # Adapted to new CSV schema
    metadata = {
        "id": str(row["player_id"]),
        "name": row["player_name"],
        "nationality_id": row.get("nationality_id"),
        "position_id": row.get("position_id"),
        "team_name": row.get("team_name"),
    }
    metadata.update(numeric_metadata)

    return content, metadata


# ------------------ DeepSeek embedding with retries ------------------

class TransientDeepSeekError(Exception):
    """Errors that are likely transient (rate limiting, timeouts, etc.)."""


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1.0, min=1, max=15),
    retry=retry_if_exception_type(TransientDeepSeekError),
)
def embed_batch(texts: List[str]) -> List[List[float]]:
    """
    Call DeepSeek's embeddings endpoint on a batch of texts.
    Uses tenacity to retry on transient failures.
    """
    url = build_embeddings_url()

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_EMBEDDING_MODEL,
        "input": texts,
    }

    print(f"[EMBED] POST {url}  texts={len(texts)}  model={DEEPSEEK_EMBEDDING_MODEL!r}")

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
    except requests.RequestException as e:
        print(f"[EMBED] Network/requests error: {e}")
        raise TransientDeepSeekError(e)

    print(f"[EMBED] HTTP {resp.status_code}")
    # Log a small prefix of body for debugging
    body_preview = resp.text[:300].replace("\n", " ")
    print(f"[EMBED] Response preview: {body_preview!r}")

    if resp.status_code >= 500:
        # Server-side failures: retry
        raise TransientDeepSeekError(
            f"Server error {resp.status_code}: {body_preview}"
        )

    if resp.status_code != 200:
        # Non-retryable client error
        raise RuntimeError(
            f"DeepSeek embeddings error {resp.status_code}: {body_preview}"
        )

    data = resp.json()

    # data["data"] is a list of { "embedding": [...], "index": i }
    try:
        embeddings = [item["embedding"] for item in data["data"]]
    except Exception as e:
        print("[EMBED] Unexpected JSON structure:", data)
        raise RuntimeError(f"Unexpected embeddings payload") from e

    # Optionally sanity-check dimensionality
    if embeddings and len(embeddings[0]) != DEEPSEEK_EMBEDDING_DIM:
        raise RuntimeError(
            f"Unexpected embedding dimension: got {len(embeddings[0])}, "
            f"expected {DEEPSEEK_EMBEDDING_DIM}. "
            f"Check DEEPSEEK_EMBEDDING_MODEL and table schema."
        )

    return embeddings


# ------------------ Main ingestion loop ------------------

def main() -> None:
    log_env()

    df = pd.read_csv(CSV_PATH)
    print(f"Loaded CSV with {len(df)} rows from {CSV_PATH!r}")

    records = [row_to_doc(r) for _, r in df.iterrows()]

    BATCH = 100
    total = len(records)
    print(f"Preparing to embed & insert {total} rows into {TABLE_NAME} …")

    for i in range(0, total, BATCH):
        batch = records[i : i + BATCH]
        contents = [c for c, _ in batch]
        metas = [m for _, m in batch]

        print(f"\n[{i:05d}] Embedding {len(batch)} texts …")

        try:
            embs = embed_batch(contents)
        except Exception as e:
            print(f"[{i:05d}] ERROR embedding batch: {e}")
            continue

        payload = [
            {
                "content": c,
                "metadata": m,
                "embedding": e,  # JSON float array; pgvector will cast
            }
            for c, m, e in zip(contents, metas, embs)
        ]

        # Insert into Supabase / Postgres
        try:
            res = supabase.table(TABLE_NAME).insert(payload).execute()
            rows = getattr(res, "data", None)
            if rows:
                print(f"[{i:05d}] Inserted {len(rows)} rows (up to {i + len(rows) - 1}).")
            else:
                print(
                    f"[{i:05d}] Insert attempted; no 'data' returned. "
                    f"Check RLS, triggers, or PostgREST config."
                )
        except Exception as e:
            print(f"[{i:05d}] ERROR inserting batch: {e}")


if __name__ == "__main__":
    main()
