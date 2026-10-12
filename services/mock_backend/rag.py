"""
RAG Knowledge Base ("Knowledge Base") retrieval: local Chroma vector store + sentence-transformers embeddings.

    documents (fixtures.KNOWLEDGE_BASE) → chunks → all-MiniLM-L6-v2 embeddings → Chroma (data/chroma)
    query → embedding → cosine top-k → chunks above KB_MIN_SCORE, grouped back into whole documents

The index is rebuilt automatically when the documents change (content hash). Retrieval only: the
coordinator (Voice Live model) writes the spoken answer from the returned document text.

Production swap: Azure AI Search (hybrid vector + keyword) with an Azure OpenAI embedding deployment,
behind the same /kb/search endpoint.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("rag")

ROOT = Path(__file__).resolve().parents[2]
MODEL = os.getenv("KB_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
DB_DIR = Path(os.getenv("KB_CHROMA_DIR", str(ROOT / "data" / "chroma")))
MIN_SCORE = float(os.getenv("KB_MIN_SCORE", "0.30"))
CHUNK_CHARS = 700


def _chunks(doc: dict) -> list[str]:
    """Title-prefixed chunks on sentence boundaries (the demo documents are short: usually one chunk)."""
    sentences = [s.strip() for s in doc["body"].replace("\n", " ").split(". ") if s.strip()]
    out, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) > CHUNK_CHARS:
            out.append(cur)
            cur = ""
        cur = f"{cur} {s}." if cur else f"{s}."
    if cur:
        out.append(cur)
    return [f"{doc['title']}. {c}" for c in out] or [doc["title"]]


class VectorKB:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None
        self._col = None
        self._docs: dict[str, dict] = {}

    def build(self, docs: list[dict]) -> None:
        """Load the embedding model and make sure the Chroma index matches `docs`."""
        with self._lock:
            import chromadb
            from sentence_transformers import SentenceTransformer

            if self._model is None:
                try:  # use the local cache first: no network round trips on a weak connection
                    self._model = SentenceTransformer(MODEL, device="cpu", local_files_only=True)
                except Exception:  # noqa: BLE001 — first run: download once
                    self._model = SentenceTransformer(MODEL, device="cpu")
            self._docs = {d["doc_id"]: d for d in docs}
            digest = hashlib.sha256(json.dumps([MODEL, docs], sort_keys=True).encode()).hexdigest()[:16]
            DB_DIR.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(DB_DIR))
            name = f"kb_{digest}"
            existing = {c.name if hasattr(c, "name") else c for c in client.list_collections()}
            if name in existing:
                self._col = client.get_collection(name)
                log.info("RAG index %s loaded (%d chunks)", name, self._col.count())
                return
            for old in existing:
                if str(old).startswith("kb_"):
                    client.delete_collection(str(old))
            self._col = client.create_collection(name, metadata={"hnsw:space": "cosine"})
            ids, texts, metas = [], [], []
            for d in docs:
                for i, text in enumerate(_chunks(d)):
                    ids.append(f"{d['doc_id']}#{i}")
                    texts.append(text)
                    metas.append({"doc_id": d["doc_id"], "tags": ",".join(d.get("tags", []))})
            vectors = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()
            self._col.add(ids=ids, documents=texts, embeddings=vectors, metadatas=metas)
            log.info("RAG index %s built: %d documents, %d chunks", name, len(docs), len(ids))

    def search(self, query: str, limit: int = 3) -> list[dict]:
        if self._col is None:
            raise RuntimeError("knowledge index not built")
        vec = self._model.encode([query], normalize_embeddings=True, show_progress_bar=False).tolist()
        res = self._col.query(query_embeddings=vec, n_results=min(limit * 3, self._col.count()))
        best: dict[str, float] = {}
        for meta, dist in zip(res["metadatas"][0], res["distances"][0]):
            score = 1.0 - float(dist)             # cosine similarity
            if score >= MIN_SCORE and score > best.get(meta["doc_id"], -1):
                best[meta["doc_id"]] = score
        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [{**self._docs[doc_id], "score": round(score, 3)} for doc_id, score in ranked]


kb = VectorKB()
