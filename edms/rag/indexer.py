"""Meta RAG indexer.

Indexes three kinds of knowledge so the chatbot can answer both *"what data do we have?"*
and *"what does the data say?"*:
  1. table metadata cards (from the governance catalog + lineage + DQ)
  2. business glossary terms
  3. data rows from Gold tables and text-heavy Silver tables

Governance-aware: rows are masked at INTERNAL clearance *before* embedding, so personal
data never enters the vector database, and every document carries its source table so
retrieval can be filtered by the asking user's role.
"""
from __future__ import annotations

import pandas as pd

from edms.etl.profiler import LEVELS
from edms.governance import store

INDEX_CLEARANCE = "INTERNAL"


def table_card(t: dict, cols: pd.DataFrame) -> str:
    col_lines = "\n".join(f"  - {c.column_name} ({c.data_type}, {c.classification}): {c.description}"
                          for c in cols.itertuples() if not c.column_name.startswith("_"))
    return (f"TABLE {t['fq_name']} [{t['layer'].upper()} layer, domain={t['domain']}, owner={t['owner']}]\n"
            f"Description: {t['description']}\n"
            f"Rows: {t['row_count']:,} | Classification: {t['classification']} | DQ score: {t['dq_score']}\n"
            f"Source: {t['source_location']} | Upstream: {t['upstream'] or 'AWS source'}\n"
            f"Columns:\n{col_lines}")


def row_text(fq: str, row: dict) -> str:
    return f"{fq} record: " + "; ".join(f"{k}={v}" for k, v in row.items() if not k.startswith("_") and v is not None)


def build_documents(wh, cfg: dict, policy) -> list[dict]:
    tables = store.read(wh, "catalog_tables")
    columns = store.read(wh, "catalog_columns")
    docs: list[dict] = []
    if tables.empty:
        return docs
    for t in tables.to_dict("records"):
        if t["table_name"].startswith("quarantine__"):
            continue
        cols = columns[(columns["layer"] == t["layer"]) & (columns["table_name"] == t["table_name"])]
        docs.append({"id": f"table:{t['fq_name']}", "text": table_card(t, cols),
                     "meta": {"kind": "table", "layer": t["layer"], "fq": t["fq_name"], "domain": t["domain"]}})

    for g in cfg.get("glossary", []):
        docs.append({"id": f"glossary:{g['term']}",
                     "text": f"GLOSSARY {g['term']}: {g['definition']} Tables: {', '.join(g.get('tables', [])) or 'n/a'}",
                     "meta": {"kind": "glossary", "layer": "glossary", "fq": None, "domain": "all"}})

    max_rows = cfg["rag"]["max_rows_per_table"]
    clearance = LEVELS.index(INDEX_CLEARANCE)
    for t in tables.to_dict("records"):
        if t["table_name"].startswith("quarantine__"):
            continue
        cols = columns[(columns["layer"] == t["layer"]) & (columns["table_name"] == t["table_name"])]
        text_cols = cols[(cols["data_type"] == "STRING") & ~cols["column_name"].str.startswith("_")]
        is_text_heavy = False
        if t["layer"] == "silver" and not text_cols.empty:
            lens = wh.query("SELECT " + ", ".join(f"AVG(LENGTH({c})) AS {c}" for c in text_cols["column_name"])
                            + f" FROM {wh.fq('silver', t['table_name'])}").iloc[0]
            is_text_heavy = bool((lens.fillna(0) > 40).any())
        if not (t["layer"] == "gold" or is_text_heavy):
            continue
        df = wh.read_table(t["layer"], t["table_name"], limit=max_rows)
        df = df[[c for c in df.columns if not c.startswith("_")]]
        cls = dict(zip(cols["column_name"], cols["classification"]))
        drop = [c for c in df.columns if LEVELS.index(cls.get(c, "INTERNAL")) > clearance]
        df = df.drop(columns=drop)  # never embed personal data
        for i, row in enumerate(df.astype(object).where(df.notna(), None).to_dict("records")):
            docs.append({"id": f"row:{t['fq_name']}:{i}", "text": row_text(t["fq_name"], row),
                         "meta": {"kind": "row", "layer": t["layer"], "fq": t["fq_name"], "domain": t["domain"]}})
    return docs


def build_index(wh, cfg: dict, policy, embedder, vstore, on_event=None) -> int:
    emit = on_event or (lambda **e: print(f"[rag] {e['message']}"))
    docs = build_documents(wh, cfg, policy)
    emit(stage="rag", message=f"Embedding {len(docs):,} documents with {embedder.name} embedder", level="info", progress=None)
    vstore.reset()
    for i in range(0, len(docs), 500):
        batch = docs[i:i + 500]
        vstore.add(batch, embedder.embed([d["text"] for d in batch]))
    store.audit(wh, "system", "RAG_INDEX_BUILD", "vector_store", "OK", {"documents": len(docs)})
    emit(stage="rag", message=f"Vector index ready: {vstore.count():,} documents", level="info", progress=1.0)
    return len(docs)
