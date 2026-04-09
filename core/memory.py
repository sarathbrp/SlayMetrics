"""
Semantic memory for RCA Slay Metrics using ChromaDB.

Each run stores its full audit + benchmark text as an embedded document, keyed
by session_id. On future runs, the top-K most similar past cases are retrieved
and injected into the RCA prompt so the LLM benefits from historical evidence.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# ChromaDB requires sqlite3 >= 3.35.0. On older systems (e.g. RHEL 8/9),
# pysqlite3-binary ships a bundled newer version — swap it in before importing chromadb.
try:
    __import__("pysqlite3")
    sys.modules["sqlite3"] = sys.modules["pysqlite3"]
except ImportError:
    pass  # system sqlite3 is new enough, or pysqlite3-binary not installed

import chromadb
import httpx

logger = logging.getLogger("slayMetrics.memory")

_COLLECTION = "rca_cases"


def _build_doc(audit_output: str, benchmark_results: str) -> str:
    return f"{audit_output}\n\nBenchmark Results:\n{benchmark_results}"


def _format_cases(results: dict) -> str:
    """Format ChromaDB query results into a prompt-ready string."""
    docs      = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    if not docs:
        return ""

    lines = ["=== Similar Past Cases ==="]
    for i, (doc, meta) in enumerate(zip(docs, metadatas), 1):
        session  = meta.get("session_id", "?")
        pct      = meta.get("max_improvement_pct", 0.0)
        report   = meta.get("rca_report", "")
        applied  = json.loads(meta.get("applied_fixes", "[]"))
        rejected = json.loads(meta.get("rejected_fixes", "[]"))

        lines.append(f"\nCase {i} (session: {session[:8]}, improvement: {pct:+.1f}%):")
        lines.append(f"  Diagnosis:\n{report}")
        if applied:
            worked = ", ".join(d for d, _ in applied)
            lines.append(f"  Worked: {worked}")
        if rejected:
            failed = ", ".join(d for d, _ in rejected)
            lines.append(f"  Didn't work: {failed}")

    return "\n".join(lines)


class SemanticMemory:
    """ChromaDB-backed semantic memory keyed by session_id.

    Embeddings: uses ChromaDB's built-in all-MiniLM-L6-v2 (sentence-transformers)
    by default. If GPT_OSS_EMBED_MODEL is set AND the endpoint supports embeddings,
    the API is used instead — but local is the safe default.
    """

    def __init__(self, persist_dir: Path, base_url: str,
                 api_key: str, embed_model: str, top_k: int = 3):
        self.base_url    = base_url.rstrip("/")
        self.api_key     = api_key
        self.embed_model = embed_model
        self.top_k       = top_k

        # Probe API embedding on first init — fall back to local if unsupported
        self._use_api = self._probe_api()

        client = chromadb.PersistentClient(path=str(persist_dir))
        if self._use_api:
            # Manual embeddings — ChromaDB stores what we pass
            self.collection = client.get_or_create_collection(
                name=_COLLECTION, metadata={"hnsw:space": "cosine"}
            )
        else:
            # Let ChromaDB embed locally with all-MiniLM-L6-v2
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
            self.collection = client.get_or_create_collection(
                name=_COLLECTION,
                embedding_function=DefaultEmbeddingFunction(),
                metadata={"hnsw:space": "cosine"},
            )
        logger.info("Semantic memory: %d cases in store (embeddings: %s)",
                    self.collection.count(), "api" if self._use_api else "local")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, session_id: str, audit_output: str, benchmark_results: str,
            rca_report: str, applied_fixes: list, rejected_fixes: list) -> None:
        """Store a completed run in the vector store."""
        doc = _build_doc(audit_output, benchmark_results)
        emb = self._embed(doc) if self._use_api else None
        if self._use_api and not emb:
            logger.warning("Embedding failed — skipping memory store for session %s", session_id[:8])
            return

        all_pcts = [pct for _, pct in applied_fixes]
        max_pct  = max(all_pcts) if all_pcts else 0.0

        add_kwargs: dict = {"documents": [doc], "ids": [session_id]}
        if emb:
            add_kwargs["embeddings"] = [emb]
        self.collection.add(
            **add_kwargs,
            metadatas=[{
                "session_id":          session_id,
                "timestamp":           datetime.now().isoformat(),
                "rca_report":          rca_report,
                "applied_fixes":       json.dumps(applied_fixes),
                "rejected_fixes":      json.dumps(rejected_fixes),
                "max_improvement_pct": max_pct,
            }],
        )
        logger.info("Stored case in semantic memory (session: %s, total: %d)",
                    session_id[:8], self.collection.count())

    def retrieve(self, audit_output: str, benchmark_results: str) -> str:
        """Return a formatted prompt block of the top-K similar past cases."""
        if self.collection.count() == 0:
            return ""

        doc = _build_doc(audit_output, benchmark_results)
        n = min(self.top_k, self.collection.count())
        if self._use_api:
            emb = self._embed(doc)
            if not emb:
                logger.warning("Embedding failed — skipping memory retrieval")
                return ""
            results = self.collection.query(query_embeddings=[emb], n_results=n)
        else:
            results = self.collection.query(query_texts=[doc], n_results=n)
        sessions = [m.get("session_id", "?")[:8]
                    for m in (results.get("metadatas") or [[]])[0]]
        logger.info("Retrieved %d similar cases from memory (sessions: %s)", n, sessions)
        return _format_cases(results)

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def _probe_api(self) -> bool:
        """Return True if the API embedding endpoint works."""
        try:
            resp = httpx.post(
                f"{self.base_url}/embeddings",
                json={"model": self.embed_model, "input": "test"},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10,
            )
            body = resp.json()
            if "data" in body or "embedding" in body:
                logger.info("Embedding API available — using API embeddings")
                return True
            logger.info("Embedding API unsupported — using local embeddings (all-MiniLM-L6-v2)")
            return False
        except Exception:
            logger.info("Embedding API unreachable — using local embeddings (all-MiniLM-L6-v2)")
            return False

    def _embed(self, text: str) -> list[float]:
        """Call the OpenAI-compatible /embeddings endpoint."""
        try:
            resp = httpx.post(
                f"{self.base_url}/embeddings",
                json={"model": self.embed_model, "input": text},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()

            # Standard OpenAI format: {"data": [{"embedding": [...]}]}
            if "data" in body:
                return body["data"][0]["embedding"]

            # Some endpoints return embedding directly: {"embedding": [...]}
            if "embedding" in body:
                return body["embedding"]

            # Unknown format — log it so we can adapt
            logger.warning("Embedding API returned unexpected format: %s",
                           str(body)[:300])
            return []
        except Exception as e:
            logger.warning("Embedding API error: %s", e)
            return []
