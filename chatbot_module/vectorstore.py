# backend/vectorstore.py
import os
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv

load_dotenv()
from supabase import create_client, Client
from langchain.schema import Document
from langchain_core.retrievers import BaseRetriever
from langchain_openai import OpenAIEmbeddings

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_ANON_KEY"]  # safe for read-only; or service key on server
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
emb = OpenAIEmbeddings(model="text-embedding-3-large", dimensions=3072)

class SupabaseRPCRetriever(BaseRetriever):
    # Declare fields for Pydantic v2
    client: Client
    k: int = 5
    metadata_filter: Optional[Dict[str, Any]] = None

    # Allow non-pydantic types like Client
    model_config = {"arbitrary_types_allowed": True}

    def _get_relevant_documents(self, query: str) -> List[Document]:
        try:
            q = (query or "").strip()
            q_vec = emb.embed_query(q)
            resp = self.client.rpc(
                "match_documents_v3",
                {
                    "query_embedding": q_vec,
                    "match_count": self.k,
                    "filter": self.metadata_filter or {},
                },
            ).execute()
        except Exception as e:
            print(f"[SupabaseRPCRetriever] RPC error: {e}")
            return []

        rows = getattr(resp, "data", []) or []
        docs: List[Document] = []
        for r in rows:
            md = (r.get("metadata") or {}) | {
                "id": r.get("id"),
                "similarity": r.get("similarity"),
            }
            docs.append(Document(page_content=r.get("content") or "", metadata=md))
        return docs

    async def _aget_relevant_documents(self, query: str) -> List[Document]:
        # simple async passthrough
        return self._get_relevant_documents(query)

def get_retriever(k: int = 10, filter: Optional[Dict[str, Any]] = None) -> BaseRetriever:
    return SupabaseRPCRetriever(client=supabase, k=k, metadata_filter=filter)

