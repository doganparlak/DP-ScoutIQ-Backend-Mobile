# backend/vectorstore.py

from __future__ import annotations

import os
from typing import List, Optional, Dict, Any

from dotenv import load_dotenv

load_dotenv()

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

# -------------------------------------------------------------------
# Environment / clients
# -------------------------------------------------------------------
_supabase_client = None
_embeddings = None


def _get_supabase_client():
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client

        supabase_url = os.environ["SUPABASE_URL"]
        supabase_key = os.environ["SUPABASE_ANON_KEY"]  # anon key is fine for read-only
        _supabase_client = create_client(supabase_url, supabase_key)
    return _supabase_client


def _get_embeddings():
    global _embeddings
    if _embeddings is None:
        from langchain_openai import OpenAIEmbeddings

        os.environ["OPENAI_API_KEY"]  # used implicitly by langchain_openai
        # Smaller, cheaper embedding model - must match documents + SQL function.
        _embeddings = OpenAIEmbeddings(
            model="text-embedding-3-small",
            dimensions=1536,
        )
    return _embeddings

import logging
log = logging.getLogger(__name__)




# -------------------------------------------------------------------
# Retriever implementation
# -------------------------------------------------------------------
class SupabaseRPCRetriever(BaseRetriever):
    k: int = 5
    metadata_filter: Optional[Dict[str, Any]] = None

    # allow non-pydantic types if the retriever gains local clients later.
    model_config = {"arbitrary_types_allowed": True}

    def _get_relevant_documents(self, query: str) -> List[Document]:
        q = (query or "").strip()
        if not q:
            return []

        try:
            # 1) embed query
            q_vec = _get_embeddings().embed_query(q)

            # 2) call Postgres function on documents
            resp = _get_supabase_client().rpc(
                "find_player",
                {
                    "query_embedding": q_vec,
                    "match_count": self.k,
                    "metadata_filter": self.metadata_filter,
                },
            ).execute()
        except Exception as e:
            log.exception("Retriever failed: %s", e)
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
                    similarity = 1.0 / (1.0 + float(distance))
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
    to the find_player SQL function, applied on metadata.
    """
    return SupabaseRPCRetriever(
        k=k,
        metadata_filter=filter,
    )
