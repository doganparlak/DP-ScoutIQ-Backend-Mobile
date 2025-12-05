# backend/vectorstore.py

from __future__ import annotations

import os
from typing import List, Optional, Dict, Any

from dotenv import load_dotenv

load_dotenv()

from supabase import create_client, Client
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_openai import OpenAIEmbeddings

# -------------------------------------------------------------------
# Environment / clients
# -------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_ANON_KEY"]  # anon key is fine for read-only
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]   # used implicitly by langchain_openai

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Cost helpers for embeddings ---
EMBEDDING_PRICE_PER_TOKEN = 0.02 / 1_000_000.0  # $0.02 / 1M tokens

def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)

# Smaller, cheaper embedding model – must match documents_v4 + SQL function
emb = OpenAIEmbeddings(
    model="text-embedding-3-small",
    dimensions=1536,
)

# -------------------------------------------------------------------
# Retriever implementation
# -------------------------------------------------------------------
class SupabaseRPCRetriever(BaseRetriever):
    client: Client
    k: int = 5
    metadata_filter: Optional[Dict[str, Any]] = None

    # allow non-pydantic types like Client
    model_config = {"arbitrary_types_allowed": True}

    def _get_relevant_documents(self, query: str) -> List[Document]:
        q = (query or "").strip()
        if not q:
            return []

        try:
            # --- Approximate embedding cost for search query ---
            q_tokens = _estimate_tokens(q)
            q_cost = q_tokens * EMBEDDING_PRICE_PER_TOKEN
            print(
                "[COST] Search embeddings (text-embedding-3-small) approx: "
                f"tokens={q_tokens}, cost≈${q_cost:.8f}, query={q[:80]!r}"
            )
            # 1) embed query
            q_vec = emb.embed_query(q)

            # 2) call Postgres function on documents_v4
            resp = self.client.rpc(
                "match_documents_v4",
                {
                    "query_embedding": q_vec,
                    "match_count": self.k,
                    "filter": self.metadata_filter or {},
                },
            ).execute()
        except Exception as e:
            print(f"[SupabaseRPCRetriever] RPC error: {e}")
            return []

        rows = getattr(resp, "data", None) or []
        docs: List[Document] = []

        for r in rows:
            if not r:
                continue

            distance = r.get("distance")
            # turn distance into a similarity score (1 - normalized distance) if you like
            similarity = None
            if isinstance(distance, (int, float)):
                try:
                    similarity = 1.0 - float(distance)
                except Exception:
                    similarity = None

            md: Dict[str, Any] = (r.get("metadata") or {}) | {
                "id": r.get("id"),
                "distance": distance,
                "similarity": similarity,
            }

            docs.append(
                Document(
                    page_content=r.get("content") or "",
                    metadata=md,
                )
            )

        return docs

    async def _aget_relevant_documents(self, query: str) -> List[Document]:
        # simple async passthrough
        return self._get_relevant_documents(query)


def get_retriever(
    k: int = 6,
    filter: Optional[Dict[str, Any]] = None,
) -> BaseRetriever:
    """
    Public factory to get a retriever instance.
    `filter` is a JSON-like dict that will be passed as the `filter` argument
    to the match_documents_v4 SQL function, applied on metadata.
    """
    return SupabaseRPCRetriever(
        client=supabase,
        k=k,
        metadata_filter=filter,
    )
