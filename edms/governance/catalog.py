"""Data catalog: tables, columns, owners, domains, classifications and descriptions.

Classifications discovered during profiling are propagated downstream through lineage,
so a Gold column built from a CONFIDENTIAL Silver column stays CONFIDENTIAL.
"""
from __future__ import annotations

import pandas as pd

from edms.config import LAYERS
from edms.etl.profiler import (PII_CLASSIFICATION, ColumnProfile, TableProfile, describe_column,
                               detect_pii, max_level)
from edms.governance import store


def load_profiles(wh) -> dict[str, TableProfile]:
    df = store.read(wh, "column_profiles")
    out: dict[str, TableProfile] = {}
    for table, g in (df.groupby("table_name") if not df.empty else []):
        cols = [ColumnProfile(name=r["name"], inferred_type=r["inferred_type"], null_ratio=r["null_ratio"],
                              distinct_ratio=r["distinct_ratio"], pii_type=r["pii_type"] if pd.notna(r["pii_type"]) else None,
                              classification=r["classification"], description=r["description"],
                              strip_currency=bool(r["strip_currency"]), avg_length=r["avg_length"])
                for _, r in g.iterrows()]
        pk = g["primary_key"].iloc[0]
        out[table] = TableProfile(table=table, rows_sampled=0, primary_key=pk if pd.notna(pk) else None, columns=cols)
    return out


def _upstream(lineage: pd.DataFrame, node: str) -> list[str]:
    if lineage.empty:
        return []
    return lineage.loc[lineage["to_node"] == node, "from_node"].tolist()


def build_catalog(wh, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    profiles = load_profiles(wh)
    lineage = store.read(wh, "lineage")
    inventory = store.read(wh, "source_inventory")
    dq = store.read(wh, "dq_results")
    models = {m["name"]: m for m in cfg["gold_models"]}
    inv = {r["dataset"]: r for r in inventory.to_dict("records")} if not inventory.empty else {}
    latest_dq = {}
    if not dq.empty:
        last = dq[dq["run_id"] == dq["run_id"].max()]
        latest_dq = (last.groupby(["layer", "table_name"])["passed"].mean() * 100).round(1).to_dict()

    tables, columns = [], []
    col_class: dict[str, dict[str, str]] = {}  # node -> column -> classification (for propagation)
    for layer in LAYERS:
        for t in wh.list_tables(layer):
            base = t.removeprefix("quarantine__")
            node = f"{layer}.{t}"
            src = inv.get(base, {})
            model = models.get(t, {}) if layer == "gold" else {}
            prof = profiles.get(base)
            schema = wh.table_schema(layer, t)
            upstream = _upstream(lineage, node)
            cmap = {}
            for name, dtype in schema:
                if name.startswith("_"):
                    cls, pii, desc = "INTERNAL", None, {"_ingested_at": "Ingestion timestamp (UTC)",
                                                         "_batch_id": "Pipeline run that loaded the row",
                                                         "_source_uri": "Original AWS object / table",
                                                         "_reason": "Why the row was quarantined"}.get(name, name)
                else:
                    cp = prof.column(name) if prof else None
                    pii = cp.pii_type if cp else detect_pii(name, pd.Series([], dtype=str))
                    cls = cp.classification if cp else PII_CLASSIFICATION.get(pii, "INTERNAL")
                    inherited = [col_class.get(u, {}).get(name) for u in upstream]
                    cls = max_level([cls, *[c for c in inherited if c]])
                    desc = cp.description if cp else describe_column(t, name, dtype, pii)
                cmap[name] = cls
                columns.append({"layer": layer, "table_name": t, "column_name": name, "data_type": dtype,
                                "classification": cls, "pii_type": pii, "description": desc})
            col_class[node] = cmap
            if layer == "bronze":
                desc = (f"Raw copy of {src.get('location', base)} migrated from AWS {src.get('system', '').upper()} "
                        f"- all values kept as text plus ingestion metadata.")
            elif layer == "silver" and t.startswith("quarantine__"):
                desc = f"Rows of {base} rejected by Silver validation (missing/invalid primary key)."
            elif layer == "silver":
                desc = (f"Cleaned, typed and de-duplicated {base.replace('_', ' ')} data. "
                        f"Primary key: {prof.primary_key if prof else 'n/a'}.")
            else:
                desc = model.get("description", "Business model")
            tables.append({
                "layer": layer, "table_name": t, "fq_name": f"{layer}.{t}", "physical_name": wh.fq(layer, t),
                "description": desc, "domain": model.get("domain") or src.get("domain", "general"),
                "owner": model.get("owner") or src.get("owner", ""), "source_system": src.get("system", "derived"),
                "source_location": src.get("location", ", ".join(upstream)),
                "primary_key": prof.primary_key if prof and layer != "gold" else None,
                "row_count": wh.row_count(layer, t), "column_count": len(schema),
                "classification": max_level(cmap.values()),
                "pii_columns": ", ".join(c for c, v in cmap.items() if v in ("CONFIDENTIAL", "RESTRICTED")),
                "dq_score": latest_dq.get((layer, t)), "upstream": ", ".join(upstream),
                "retention_days": cfg["policies"].get("retention_days", {}).get(layer),
                "updated_at": store.now(),
            })
    tdf, cdf = pd.DataFrame(tables), pd.DataFrame(columns)
    if not tdf.empty:
        wh.write_df("governance", "catalog_tables", tdf.astype({"primary_key": "string", "dq_score": "float64"}))
        wh.write_df("governance", "catalog_columns", cdf.astype({"pii_type": "string"}))
    return tdf, cdf


def column_classifications(catalog_columns: pd.DataFrame, tables: list[str] | None = None) -> dict[str, str]:
    """column name -> highest classification among the given fq tables."""
    if catalog_columns.empty:
        return {}
    df = catalog_columns
    if tables:
        fq = df["layer"] + "." + df["table_name"]
        df = df[fq.isin(tables)]
    return df.groupby("column_name")["classification"].agg(max_level).to_dict()
