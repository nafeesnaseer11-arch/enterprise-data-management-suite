"""Embeddings and vector stores.

* HashingEmbedder  - dependency-free, deterministic embeddings for offline/local use.
* VertexEmbedder   - Vertex AI text embeddings (text-embedding-005) for production.
* LocalVectorStore - NumPy cosine-similarity store persisted to disk.
* BigQueryVectorStore - embeddings stored in BigQuery and searched with VECTOR_SEARCH,
  so retrieval runs next to the governed data (no extra vector infrastructure).
"""
from __future__ import annotations

import hashlib
import json
import re

import numpy as np
import pandas as pd

from edms.config import resolve

_TOKEN = re.compile(r"[a-z0-9]+")
SYNONYMS = {  # light query expansion for the offline embedder
    "sales": "revenue", "turnover": "revenue", "income": "revenue", "spend": "lifetime_value",
    "staff": "employees", "employee": "employees", "headcount": "employees", "people": "employees",
    "ticket": "support", "tickets": "support", "complaint": "support", "client": "customer",
    "clients": "customer", "customers": "customer", "orders": "order", "products": "product",
    "invoices": "invoice", "payments": "invoice", "pay": "salary", "ltv": "lifetime_value",
}


class HashingEmbedder:
    name = "hashing"

    def __init__(self, dim: int = 768):
        self.dim = dim

    def _tokens(self, text: str) -> list[str]:
        words = _TOKEN.findall(text.lower().replace("_", " "))
        words += [SYNONYMS[w] for w in words if w in SYNONYMS]
        return words + [f"{a}_{b}" for a, b in zip(words, words[1:])]

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            counts: dict[int, float] = {}
            for tok in self._tokens(t):
                h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:8], "little")
                idx, sign = h % self.dim, 1.0 if (h >> 63) & 1 else -1.0
                counts[idx] = counts.get(idx, 0.0) + sign
            for idx, v in counts.items():
                out[i, idx] = np.sign(v) * (1 + np.log(abs(v))) if v else 0
            n = np.linalg.norm(out[i])
            if n:
                out[i] /= n
        return out


class VertexEmbedder:
    """Vertex AI text embeddings through the google-genai SDK (uses the runtime's ADC)."""
    name = "vertex"

    def __init__(self, project: str, region: str, model: str = "text-embedding-005", dim: int = 768):
        from google import genai
        from google.genai import types

        self._types = types
        self.client = genai.Client(vertexai=True, project=project, location=region)
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str], task: str = "RETRIEVAL_DOCUMENT") -> np.ndarray:
        cfg = self._types.EmbedContentConfig(task_type=task, output_dimensionality=self.dim, auto_truncate=True)
        vecs = []
        for i in range(0, len(texts), 100):  # stay under the per-request instance limit
            resp = self.client.models.embed_content(model=self.model, contents=texts[i:i + 100], config=cfg)
            vecs += [e.values for e in resp.embeddings]
        arr = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.where(norms == 0, 1, norms)


class LocalVectorStore:
    def __init__(self, path: str):
        self.dir = resolve(path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        vp, mp = self.dir / "vectors.npy", self.dir / "docs.jsonl"
        self.vectors = np.load(vp) if vp.exists() else np.zeros((0, 0), dtype=np.float32)
        self.docs = [json.loads(l) for l in mp.read_text().splitlines()] if mp.exists() else []

    def reset(self):
        self.vectors, self.docs = np.zeros((0, 0), dtype=np.float32), []
        self._save()

    def _save(self):
        np.save(self.dir / "vectors.npy", self.vectors)
        (self.dir / "docs.jsonl").write_text("\n".join(json.dumps(d, default=str) for d in self.docs))

    def add(self, docs: list[dict], vectors: np.ndarray):
        self.vectors = vectors if self.vectors.size == 0 else np.vstack([self.vectors, vectors])
        self.docs += docs
        self._save()

    def count(self) -> int:
        return len(self.docs)

    def search(self, qvec: np.ndarray, k: int, allowed: set[str] | None = None) -> list[dict]:
        if not self.docs:
            return []
        scores = self.vectors @ qvec
        order = np.argsort(-scores)
        out = []
        for i in order:
            d = self.docs[i]
            if allowed is not None and d["meta"].get("fq") and d["meta"]["fq"] not in allowed:
                continue
            out.append({**d, "score": float(scores[i])})
            if len(out) >= k:
                break
        return out

    def stats(self) -> pd.DataFrame:
        if not self.docs:
            return pd.DataFrame()
        return pd.DataFrame([d["meta"] for d in self.docs]).groupby(["kind", "layer"], dropna=False).size().reset_index(name="documents")


class BigQueryVectorStore:
    TABLE = "rag_embeddings"

    def __init__(self, wh):
        self.wh = wh

    def reset(self):
        self.wh.execute(f"DROP TABLE IF EXISTS {self.wh.fq('governance', self.TABLE)}")

    def add(self, docs: list[dict], vectors: np.ndarray):
        df = pd.DataFrame({"id": [d["id"] for d in docs], "text": [d["text"] for d in docs],
                           "meta": [json.dumps(d["meta"]) for d in docs],
                           "fq": [d["meta"].get("fq") or "" for d in docs],
                           "embedding": [v.astype(float).tolist() for v in vectors]})
        self.wh.write_df("governance", self.TABLE, df, mode="append")

    def count(self) -> int:
        return self.wh.row_count("governance", self.TABLE) if self.wh.table_exists("governance", self.TABLE) else 0

    def search(self, qvec: np.ndarray, k: int, allowed: set[str] | None = None) -> list[dict]:
        allowed_list = sorted(allowed) if allowed is not None else []
        flt = "WHERE base.fq = '' OR base.fq IN UNNEST(@allowed)" if allowed is not None else ""
        sql = f"""
            SELECT base.id, base.text, base.meta, 1 - distance AS score
            FROM VECTOR_SEARCH(TABLE {self.wh.fq('governance', self.TABLE)}, 'embedding',
                               (SELECT @q AS embedding), top_k => {int(k) * 4}, distance_type => 'COSINE')
            {flt} ORDER BY score DESC LIMIT {int(k)}"""
        from google.cloud import bigquery
        params = [bigquery.ArrayQueryParameter("q", "FLOAT64", qvec.astype(float).tolist())]
        if allowed is not None:
            params.append(bigquery.ArrayQueryParameter("allowed", "STRING", allowed_list))
        df = self.wh.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).to_dataframe()
        return [{"id": r.id, "text": r.text, "meta": json.loads(r.meta), "score": float(r.score)} for r in df.itertuples()]

    def stats(self) -> pd.DataFrame:
        if not self.count():
            return pd.DataFrame()
        return self.wh.query(f"SELECT JSON_VALUE(meta, '$.kind') AS kind, JSON_VALUE(meta, '$.layer') AS layer, "
                             f"COUNT(*) AS documents FROM {self.wh.fq('governance', self.TABLE)} GROUP BY 1, 2")


def make_embedder(cfg: dict):
    r = cfg["rag"]
    if r["embedder"] == "vertex":
        return VertexEmbedder(cfg["gcp"]["project_id"], cfg["gcp"]["vertex_region"], cfg["gcp"]["embedding_model"], r["dim"])
    return HashingEmbedder(r["dim"])


def make_vector_store(cfg: dict, wh):
    if cfg["mode"] == "gcp":
        return BigQueryVectorStore(wh)
    return LocalVectorStore(cfg["local"]["vector_store_path"])
