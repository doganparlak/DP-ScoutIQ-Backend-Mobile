# chatbot_module/translate_retriever.py
from typing import List
from langchain_core.retrievers import BaseRetriever
#from langchain.schema import Document
from langchain_core.documents import Document
from langchain_openai import ChatOpenAI
from chatbot_module.prompts import TRANSLATE_PROMPT

class TranslateQueryRetriever(BaseRetriever):
    def __init__(self, base: BaseRetriever, translator: ChatOpenAI):
        super().__init__()
        # allow non-pydantic types
        object.__setattr__(self, "base", base)
        object.__setattr__(self, "translator", translator)

    def _to_english(self, q: str) -> str:
        q = (q or "").strip()
        if not q:
            return q
        # one-shot, deterministic-ish
        out = self.translator.invoke(TRANSLATE_PROMPT.format(q=q))
        return (getattr(out, "content", None) or str(out) or "").strip()

    def _get_relevant_documents(self, query: str) -> List[Document]:
        q_en = self._to_english(query)
        return self.base.get_relevant_documents(q_en or query)

    async def _aget_relevant_documents(self, query: str) -> List[Document]:
        return self._get_relevant_documents(query)
