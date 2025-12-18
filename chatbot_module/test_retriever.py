# scripts/test_retriever.py
from dotenv import load_dotenv
load_dotenv()

from chatbot_module.vectorstore_small import supabase, emb  # adjust import path to your file

q = "Mauro Icardi"
q_vec = emb.embed_query(q)

resp = supabase.rpc("find_player", {
    "query_embedding": q_vec,
    "match_count": 6,
    "metadata_filter": None,
}).execute()

print("RPC data:", resp.data)
