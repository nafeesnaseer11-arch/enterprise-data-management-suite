"""Data-quality checks executed in the warehouse after Silver/Gold are built."""
from __future__ import annotations

import re

from edms.etl.profiler import TableProfile

NON_NEGATIVE = re.compile(r"(price|amount|quantity|qty|salary|total|revenue|units|cost)")


def run_checks(wh, layer: str, table: str, profile: TableProfile | None, run_id: str) -> list[dict]:
    ref = wh.fq(layer, table)
    schema = [(n, t) for n, t in wh.table_schema(layer, table) if not n.startswith("_")]
    results: list[dict] = []

    def add(check, column, value, threshold, dimension):
        results.append({"run_id": run_id, "layer": layer, "table_name": table, "dimension": dimension,
                        "check_name": check, "column_name": column, "value": round(float(value), 4),
                        "threshold": threshold, "passed": bool(value >= threshold)})

    counts = wh.query("SELECT COUNT(*) AS _n, " + ", ".join(f"COUNT({n}) AS {n}" for n, _ in schema) + f" FROM {ref}")
    total = int(counts["_n"].iloc[0]) or 1
    add("row_count_positive", None, 1.0 if counts["_n"].iloc[0] > 0 else 0.0, 1.0, "volume")
    for n, _ in schema:
        add("completeness", n, counts[n].iloc[0] / total, 0.9, "completeness")

    pk = profile.primary_key if profile else None
    if pk and pk in dict(schema):
        u = wh.query(f"SELECT COUNT(DISTINCT {pk}) AS d, COUNT(*) AS n FROM {ref}")
        add("primary_key_unique", pk, u["d"].iloc[0] / max(int(u["n"].iloc[0]), 1), 1.0, "uniqueness")

    for n, t in schema:
        if t in ("INT64", "FLOAT64") and NON_NEGATIVE.search(n):
            v = wh.query(f"SELECT AVG(CASE WHEN {n} >= 0 OR {n} IS NULL THEN 1.0 ELSE 0.0 END) AS v FROM {ref}")
            add("non_negative", n, v["v"].iloc[0] or 0, 1.0, "validity")
        col = profile.column(n) if profile else None
        if col and col.pii_type == "EMAIL":
            m = wh.macro("regex_match", [n, r"^[^@ ]+@[^@ ]+\.[a-z]{2,}$"])
            v = wh.query(f"SELECT AVG(CASE WHEN {n} IS NULL OR {m} THEN 1.0 ELSE 0.0 END) AS v FROM {ref}")
            add("valid_email_format", n, v["v"].iloc[0] or 0, 0.98, "validity")
    return results


def score(results: list[dict]) -> float:
    return round(100 * sum(r["passed"] for r in results) / len(results), 1) if results else 0.0
