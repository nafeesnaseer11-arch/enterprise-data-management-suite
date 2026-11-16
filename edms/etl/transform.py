"""Silver SQL generation from a table profile.

The generated SQL runs *inside* the warehouse (BigQuery / DuckDB), so Silver scales to
the size of the warehouse rather than the memory of the pipeline worker.
"""
from __future__ import annotations

from edms.etl.profiler import NULL_TOKENS, TableProfile

META_COLS = ["_ingested_at", "_batch_id", "_source_uri"]
_NULLS = ", ".join(f"'{t}'" for t in sorted(NULL_TOKENS))


def clean_expr(col: str) -> str:
    return f"CASE WHEN LOWER(TRIM({col})) IN ({_NULLS}) THEN NULL ELSE TRIM({col}) END"


def typed_expr(wh, c) -> str:
    base = clean_expr(c.name)
    if c.inferred_type == "STRING":
        return f"LOWER({base})" if c.pii_type == "EMAIL" else base
    if c.inferred_type == "BOOL":
        return (f"CASE WHEN LOWER({base}) IN ('true','yes','y','t','1') THEN TRUE "
                f"WHEN LOWER({base}) IN ('false','no','n','f','0') THEN FALSE ELSE NULL END")
    inner = wh.macro("strip_currency", [base]) if c.strip_currency else base
    return wh.macro("safe_cast", [inner, c.inferred_type])


def typed_cte(wh, profile: TableProfile, bronze_ref: str) -> str:
    cols = [f"    {typed_expr(wh, c)} AS {c.name}" for c in profile.columns]
    cols += [f"    {m}" for m in META_COLS]
    return f"typed AS (\n  SELECT\n" + ",\n".join(cols) + f"\n  FROM {bronze_ref}\n)"


def silver_sql(wh, profile: TableProfile, bronze_ref: str) -> str:
    """Typed, trimmed, null-normalised, de-duplicated (latest record wins)."""
    pk = profile.primary_key
    business = [c.name for c in profile.columns]
    partition = pk if pk else ", ".join(business)
    where = f"{pk} IS NOT NULL" if pk else "TRUE"
    return (f"WITH {typed_cte(wh, profile, bronze_ref)}\n"
            f"SELECT * FROM typed\nWHERE {where}\n"
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY _ingested_at DESC) = 1")


def quarantine_sql(wh, profile: TableProfile, bronze_ref: str) -> str | None:
    if not profile.primary_key:
        return None
    return (f"WITH {typed_cte(wh, profile, bronze_ref)}\n"
            f"SELECT *, 'NULL_OR_INVALID_PRIMARY_KEY' AS _reason FROM typed WHERE {profile.primary_key} IS NULL")


def conformance_sql(wh, profile: TableProfile, bronze_ref: str) -> str | None:
    """Counts values present in Bronze that could not be converted to the inferred type."""
    checks = [c for c in profile.columns if c.inferred_type != "STRING"]
    if not checks:
        return None
    parts = [f"SUM(CASE WHEN {clean_expr(c.name)} IS NOT NULL AND ({typed_expr(wh, c)}) IS NULL THEN 1 ELSE 0 END) AS {c.name}"
             for c in checks]
    return "SELECT\n  " + ",\n  ".join(parts) + f"\nFROM {bronze_ref}"
