# backend/core.py
import pandas as pd
from dotenv import load_dotenv
from langchain.docstore.document import Document
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS

load_dotenv()

df = pd.read_csv("data_module/player_level_stats.csv")

docs = []
for _, row in df.iterrows():
    non_zero_info = []
    numeric_metadata = {}

    for col, val in row.items():
        # Skip NaNs
        if pd.isna(val):
            continue
        # Keep all string fields (like name, position, etc.)
        if isinstance(val, str) and val.strip() and val.lower() != 'nan':
            non_zero_info.append(f"{col}: {val}")
        # Keep numeric fields only if non-zero
        elif isinstance(val, (int, float)) and val != 0:
            non_zero_info.append(f"{col}: {val}")
            # Store numeric fields separately in metadata as numbers
            numeric_metadata[col] = val

    player_text = "\n".join(non_zero_info)

    # Construct metadata with numeric fields + id and name
    metadata = {"id": row["player_id"], "name": row["player_name"]}
    metadata.update(numeric_metadata)

    doc = Document(
        page_content=player_text,
        metadata=metadata
    )
    docs.append(doc)

# Initialize OpenAI embedding model
embedding = OpenAIEmbeddings(model = "text-embedding-3-large")
vectorstore = None
batch_size = 100 

for i in range(0, len(docs), batch_size):
    batch_docs = docs[i:i+batch_size]
    if vectorstore is None:
        vectorstore = FAISS.from_documents(batch_docs, embedding)
    else:
        vectorstore.add_documents(batch_docs)



"""
# VECTOR DB TEST
query = "report me the best 5 players with the best passing accuracy"
query_answer = vectorstore.similarity_search(query, k=10)
print(query_answer[0].page_content)
"""
#Persist to disk
vectorstore.save_local("data_module/faiss_index")
