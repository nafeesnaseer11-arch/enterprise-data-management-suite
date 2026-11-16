"""Wires the three modules together: Intelligent ETL -> Data Governance -> Meta RAG."""
from __future__ import annotations

from typing import Callable

from edms.config import LAYERS, load_settings, resolve
from edms.etl.pipeline import IntelligentETL
from edms.governance import store
from edms.governance.policies import PolicyEngine
from edms.rag.chatbot import MetaRAGChatbot
from edms.rag.indexer import build_index
from edms.rag.vectors import make_embedder, make_vector_store
from edms.storage.lake import make_lake
from edms.storage.sources import discover_all, make_sources
from edms.storage.warehouse import make_warehouse


class Suite:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_settings()
        self.wh = make_warehouse(self.cfg)
        self.wh.ensure_datasets()
        self.lake = make_lake(self.cfg)
        self.sources = make_sources(self.cfg)
        self.policy = PolicyEngine(self.cfg["policies"])
        self.embedder = make_embedder(self.cfg)
        self.vstore = make_vector_store(self.cfg, self.wh)
        self.chatbot = MetaRAGChatbot(self.cfg, self.wh, self.policy, self.embedder, self.vstore)

    # ------------------------------------------------------------ modules
    def etl(self, on_event: Callable | None = None) -> IntelligentETL:
        return IntelligentETL(self.cfg, self.wh, self.lake, self.sources, on_event)

    def discover(self):
        return [ds.as_dict() for _, ds in discover_all(self.sources)]

    def run_pipeline(self, layers=LAYERS, on_event=None, actor="data_engineer"):
        return self.etl(on_event).run(layers, actor=actor)

    def build_index(self, on_event=None) -> int:
        return build_index(self.wh, self.cfg, self.policy, self.embedder, self.vstore, on_event)

    def has_source_data(self) -> bool:
        return bool(self.discover())

    def generate_sample_data(self):
        if self.cfg["mode"] != "local":
            raise RuntimeError("Sample data is only generated in local mode")
        from scripts.generate_sample_data import generate
        generate(resolve(self.cfg["local"]["aws_s3_root"]), resolve(self.cfg["local"]["aws_rds_root"]))

    def run_end_to_end(self, on_event: Callable | None = None, actor: str = "data_admin"):
        emit = on_event or (lambda **e: print(f"[{e['stage']}] {e['message']}"))
        if self.cfg["mode"] == "local" and not self.has_source_data():
            emit(stage="setup", message="No AWS data found - generating a sample organisation estate", level="info", progress=None)
            self.generate_sample_data()
        run = self.run_pipeline(LAYERS, on_event, actor)
        n = self.build_index(on_event)
        emit(stage="done", message=f"End-to-end complete: run {run.run_id} ({run.status}), DQ {run.dq_score}%, "
                                   f"{n:,} documents indexed for Meta RAG", level="info", progress=1.0)
        return run, n

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        tables = store.read(self.wh, "catalog_tables")
        runs = store.read(self.wh, "pipeline_runs")
        out = {"mode": self.cfg["mode"], "dialect": self.wh.dialect, "llm": self.chatbot.llm_enabled,
               "embedder": self.embedder.name, "documents": self.vstore.count(), "last_run": None}
        for layer in LAYERS:
            sub = tables[tables["layer"] == layer] if not tables.empty else tables
            out[f"{layer}_tables"] = len(sub)
            out[f"{layer}_rows"] = int(sub["row_count"].sum()) if not sub.empty else 0
        if not runs.empty:
            out["last_run"] = runs.sort_values("started_at").iloc[-1].to_dict()
        return out
