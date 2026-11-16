"""Intelligent ETL: AWS -> GCS landing -> BigQuery Bronze -> Silver -> Gold.

Every stage is driven by metadata (discovery + profiling), so onboarding a new S3 prefix
or RDS table requires no code: the next run discovers, lands, profiles, types, cleans,
classifies and catalogs it automatically.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import pandas as pd

from edms.config import LAYERS
from edms.etl import quality, transform
from edms.etl.profiler import TableProfile, normalize_columns, profile_table
from edms.governance import catalog, store
from edms.storage.sources import discover_all


@dataclass
class RunResult:
    run_id: str
    status: str = "RUNNING"
    tables: dict = field(default_factory=lambda: {"bronze": [], "silver": [], "gold": []})
    reconciliation: list = field(default_factory=list)
    dq: list = field(default_factory=list)
    drift: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def dq_score(self) -> float:
        return quality.score(self.dq)


class IntelligentETL:
    def __init__(self, cfg: dict, wh, lake, sources: list, on_event: Callable | None = None):
        self.cfg, self.wh, self.lake, self.sources = cfg, wh, lake, sources
        self.on_event = on_event or (lambda **e: print(f"[{e['stage']}] {e['message']}"))
        self.profiles: dict[str, TableProfile] = {}
        self.lineage: list[dict] = []

    def emit(self, stage: str, message: str, level: str = "info", progress: float | None = None):
        self.on_event(stage=stage, message=message, level=level, progress=progress)

    # ------------------------------------------------------------------ discovery
    def discover(self):
        pairs = discover_all(self.sources)
        self.emit("discovery", f"Discovered {len(pairs)} datasets across {len(self.sources)} AWS sources")
        return pairs

    # ------------------------------------------------------------------ bronze
    def _bronze(self, src, ds, run: RunResult, batch_id: str):
        ingested_at = store.now()
        frames, landed, src_rows = [], [], 0
        for key, raw, df in src.extract(ds):
            path = f"raw/{ds.system}/{ds.container}/ingest_date={date.today()}/{key}"
            uri = self.lake.put(path, raw)
            landed.append(uri)
            df.columns = normalize_columns(list(df.columns))
            df["_source_uri"] = f"{ds.location.rstrip('/')}/{key.split('/')[-1]}" if ds.system == "s3" else ds.location
            frames.append(df)
            src_rows += len(df)
        if not frames:
            return
        bronze = pd.concat(frames, ignore_index=True)
        biz = [c for c in bronze.columns if not c.startswith("_")]
        bronze[biz] = bronze[biz].astype("string")
        bronze["_ingested_at"] = ingested_at
        bronze["_batch_id"] = batch_id

        # schema drift detection against the previous load
        if self.wh.table_exists("bronze", ds.name):
            prev = {c for c, _ in self.wh.table_schema("bronze", ds.name)}
            added, removed = set(bronze.columns) - prev, prev - set(bronze.columns)
            if added or removed:
                run.drift.append({"run_id": run.run_id, "table_name": ds.name, "detected_at": ingested_at,
                                  "added_columns": ", ".join(sorted(added)), "removed_columns": ", ".join(sorted(removed))})
                self.emit("bronze", f"Schema drift on {ds.name}: +{sorted(added)} -{sorted(removed)}", "warning")

        self.wh.write_df("bronze", ds.name, bronze, mode="replace")
        bronze_rows = self.wh.row_count("bronze", ds.name)
        run.tables["bronze"].append(ds.name)
        run.reconciliation.append({"run_id": run.run_id, "table_name": ds.name, "source_location": ds.location,
                                   "source_objects": len(ds.objects), "source_rows": src_rows, "bronze_rows": bronze_rows})
        self.lineage += [
            {"from_node": ds.location, "to_node": f"landing:{ds.container}/{ds.name}", "from_layer": f"aws_{ds.system}",
             "to_layer": "landing", "transformation": "byte-for-byte copy (S3/RDS -> GCS)"},
            {"from_node": f"landing:{ds.container}/{ds.name}", "to_node": f"bronze.{ds.name}", "from_layer": "landing",
             "to_layer": "bronze", "transformation": "load as text + ingestion metadata"},
        ]
        self.emit("bronze", f"{ds.location} -> bronze.{ds.name}: {src_rows:,} source rows, {bronze_rows:,} loaded, "
                            f"{len(landed)} object(s) landed in GCS")

    # ------------------------------------------------------------------ silver
    def _silver(self, table: str, run: RunResult):
        wh = self.wh
        sample = wh.read_table("bronze", table, limit=self.cfg["etl"]["profile_sample_rows"])
        prof = profile_table(table, sample, self.cfg["etl"]["type_inference_threshold"])
        self.profiles[table] = prof
        bref = wh.fq("bronze", table)

        wh.create_table_as("silver", table, transform.silver_sql(wh, prof, bref))
        q_rows = 0
        qsql = transform.quarantine_sql(wh, prof, bref)
        if qsql:
            wh.create_table_as("silver", f"quarantine__{table}", qsql)
            q_rows = wh.row_count("silver", f"quarantine__{table}")
            if q_rows == 0:  # keep the catalog clean: only materialise quarantine when needed
                wh.execute(f"DROP TABLE IF EXISTS {wh.fq('silver', f'quarantine__{table}')}")
        conf = {}
        csql = transform.conformance_sql(wh, prof, bref)
        if csql:
            conf = {k: int(v or 0) for k, v in wh.query(csql).iloc[0].to_dict().items()}
        bronze_rows = wh.row_count("bronze", table)
        silver_rows = wh.row_count("silver", table)
        for r in run.reconciliation:
            if r["table_name"] == table:
                r.update(silver_rows=silver_rows, quarantined_rows=q_rows,
                         duplicates_removed=bronze_rows - silver_rows - q_rows,
                         type_conversion_failures=sum(conf.values()))
        if not any(r["table_name"] == table for r in run.reconciliation):
            run.reconciliation.append({"run_id": run.run_id, "table_name": table, "bronze_rows": bronze_rows,
                                       "silver_rows": silver_rows, "quarantined_rows": q_rows,
                                       "duplicates_removed": bronze_rows - silver_rows - q_rows,
                                       "type_conversion_failures": sum(conf.values())})
        run.tables["silver"].append(table)
        self.lineage.append({"from_node": f"bronze.{table}", "to_node": f"silver.{table}", "from_layer": "bronze",
                             "to_layer": "silver", "transformation": "type inference, trim, null-normalise, dedupe on "
                             + (prof.primary_key or "all columns")})
        if q_rows:
            self.lineage.append({"from_node": f"bronze.{table}", "to_node": f"silver.quarantine__{table}",
                                 "from_layer": "bronze", "to_layer": "silver", "transformation": "rejected rows"})
        pii = [f"{c.name}({c.pii_type})" for c in prof.columns if c.pii_type]
        typed = sum(c.inferred_type != "STRING" for c in prof.columns)
        self.emit("silver", f"silver.{table}: {silver_rows:,} rows | key={prof.primary_key} | {typed} typed cols | "
                            f"{bronze_rows - silver_rows - q_rows} dupes removed | {q_rows} quarantined"
                            + (f" | PII: {', '.join(pii)}" if pii else ""))

    # ------------------------------------------------------------------ gold
    def _gold(self, model: dict, run: RunResult):
        deps = self.wh.refs(model["sql"])
        missing = [f"{l}.{t}" for l, t in deps if not self.wh.table_exists(l, t)]
        if missing:
            self.emit("gold", f"Skipping gold.{model['name']}: missing {missing}", "warning")
            return
        self.wh.create_table_as("gold", model["name"], self.wh.render(model["sql"]))
        n = self.wh.row_count("gold", model["name"])
        run.tables["gold"].append(model["name"])
        self.lineage += [{"from_node": f"{l}.{t}", "to_node": f"gold.{model['name']}", "from_layer": l,
                          "to_layer": "gold", "transformation": "business model (SQL)"} for l, t in deps]
        self.emit("gold", f"gold.{model['name']}: {n:,} rows from {', '.join(f'{l}.{t}' for l, t in deps)}")

    # ------------------------------------------------------------------ orchestration
    def run(self, layers=LAYERS, actor: str = "pipeline") -> RunResult:
        run = RunResult(run_id=f"run_{store.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}")
        started = store.now()
        self.lineage = []
        self.wh.ensure_datasets()
        self.emit("start", f"Run {run.run_id} | mode={self.cfg['mode']} | layers={list(layers)}")
        steps = 0
        try:
            if "bronze" in layers:
                pairs = self.discover()
                store.upsert(self.wh, "source_inventory",
                             [{**ds.as_dict(), "discovered_at": started} for _, ds in pairs], ["dataset"])
                for i, (src, ds) in enumerate(pairs):
                    self.emit("bronze", f"Migrating {ds.location}", progress=(i + 1) / max(len(pairs), 1))
                    try:
                        self._bronze(src, ds, run, run.run_id)
                    except Exception as e:  # keep migrating the rest of the estate
                        run.errors.append(f"bronze.{ds.name}: {e}")
                        self.emit("bronze", f"FAILED {ds.location}: {e}", "error")
                steps += 1
            if "silver" in layers:
                tables = [t for t in self.wh.list_tables("bronze")]
                for i, t in enumerate(tables):
                    self.emit("silver", f"Profiling & refining {t}", progress=(i + 1) / max(len(tables), 1))
                    try:
                        self._silver(t, run)
                    except Exception as e:
                        run.errors.append(f"silver.{t}: {e}")
                        self.emit("silver", f"FAILED {t}: {e}", "error")
                if self.profiles:
                    recs = [r for p in self.profiles.values() for r in p.to_records()]
                    store.upsert(self.wh, "column_profiles", recs, ["table_name"])
                steps += 1
            if "gold" in layers:
                for m in self.cfg["gold_models"]:
                    try:
                        self._gold(m, run)
                    except Exception as e:
                        run.errors.append(f"gold.{m['name']}: {e}")
                        self.emit("gold", f"FAILED gold.{m['name']}: {e}", "error")
                steps += 1

            # ---------------- data quality
            self.emit("quality", "Running data-quality checks")
            profiles = self.profiles or catalog.load_profiles(self.wh)
            for layer in ("silver", "gold"):
                for t in run.tables[layer]:
                    run.dq += quality.run_checks(self.wh, layer, t, profiles.get(t), run.run_id)
            self.emit("quality", f"DQ score {run.dq_score}% across {len(run.dq)} checks")

            # ---------------- governance metadata
            for r in self.lineage:
                r["run_id"], r["created_at"] = run.run_id, started
            store.upsert(self.wh, "lineage", self.lineage, ["to_node", "from_node"])
            store.append(self.wh, "reconciliation", run.reconciliation)
            store.append(self.wh, "dq_results", run.dq)
            store.append(self.wh, "schema_drift", run.drift)
            catalog.build_catalog(self.wh, self.cfg)
            self.emit("governance", "Catalog, lineage, classifications and DQ results published")
            run.status = "SUCCESS" if not run.errors else "PARTIAL_SUCCESS"
        except Exception as e:
            run.status = "FAILED"
            run.errors.append(str(e))
            self.emit("error", f"Run failed: {e}", "error")
            raise
        finally:
            store.append(self.wh, "pipeline_runs", [{
                "run_id": run.run_id, "started_at": started, "finished_at": store.now(), "status": run.status,
                "layers": ",".join(layers), "bronze_tables": len(run.tables["bronze"]),
                "silver_tables": len(run.tables["silver"]), "gold_tables": len(run.tables["gold"]),
                "dq_score": run.dq_score, "errors": " | ".join(run.errors)[:2000]}])
            store.audit(self.wh, actor, "PIPELINE_RUN", run.run_id, run.status, {"layers": list(layers)})
        self.emit("done", f"Run {run.run_id} finished: {run.status}")
        return run
