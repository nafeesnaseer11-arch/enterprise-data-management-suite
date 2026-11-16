"""Policy engine: role-based access, column masking, governed SQL and BigQuery policy export.

The same policies are enforced in two places:
  * in the application (chatbot / UI queries) via :meth:`PolicyEngine.governed_query`
  * natively in BigQuery via the DDL produced by :func:`bigquery_governance_script`
    (dataset grants, column descriptions, labels, policy tags, table expiration).
"""
from __future__ import annotations

import hashlib
import re

import pandas as pd

from edms.etl.profiler import LEVELS, detect_pii, PII_CLASSIFICATION
from edms.governance import store
from edms.governance.catalog import column_classifications

FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|create|alter|truncate|grant|revoke|call|export|copy|"
                       r"attach|detach|pragma|install|load|execute|vacuum|checkpoint)\b", re.I)
TABLE_REF = re.compile(r"(?<![\w.`])(bronze|silver|gold|governance)\.([A-Za-z_]\w*)\b")


class AccessDenied(Exception):
    pass


class PolicyEngine:
    def __init__(self, policies: dict):
        self.policies = policies
        self.roles: dict = policies["roles"]

    def role(self, name: str) -> dict:
        if name not in self.roles:
            raise AccessDenied(f"Unknown role {name}")
        return self.roles[name]

    def clearance(self, role: str) -> int:
        return LEVELS.index(self.role(role)["max_classification"])

    def can_access(self, role: str, layer: str, domain: str | None) -> bool:
        r = self.role(role)
        if layer not in r["layers"]:
            return False
        return "*" in r["domains"] or domain in r["domains"] or layer == "governance"

    def accessible(self, role: str, catalog_tables: pd.DataFrame) -> pd.DataFrame:
        if catalog_tables.empty:
            return catalog_tables
        ok = catalog_tables.apply(lambda r: self.can_access(role, r["layer"], r["domain"]), axis=1)
        return catalog_tables[ok]

    # ------------------------------------------------------------- masking
    def mask_value(self, v, classification: str):
        if v is None or (not isinstance(v, (list, dict)) and pd.isna(v)):
            return v
        how = self.policies.get("masking", {}).get(classification, "redact")
        if how == "tokenize":
            return "tok_" + hashlib.sha256(str(v).encode()).hexdigest()[:10]
        return "***REDACTED***"

    def mask_df(self, role: str, df: pd.DataFrame, col_class: dict[str, str]) -> tuple[pd.DataFrame, list[str]]:
        allowed = self.clearance(role)
        out, masked = df.copy(), []
        for c in df.columns:
            cls = col_class.get(c) or PII_CLASSIFICATION.get(detect_pii(c, pd.Series([], dtype=str)), "INTERNAL")
            if LEVELS.index(cls) > allowed:
                out[c] = df[c].map(lambda v, cls=cls: self.mask_value(v, cls)).astype("object")
                masked.append(f"{c} ({cls})")
        return out, masked

    # ------------------------------------------------------------- governed SQL
    @staticmethod
    def validate_sql(sql: str) -> str:
        s = sql.strip().rstrip(";").strip()
        no_strings = re.sub(r"'[^']*'", "''", s)
        if ";" in no_strings:
            raise AccessDenied("Only a single statement is allowed")
        if not re.match(r"^\s*(select|with)\b", s, re.I):
            raise AccessDenied("Only SELECT queries are allowed")
        if FORBIDDEN.search(no_strings):
            raise AccessDenied(f"Statement contains a forbidden keyword: {FORBIDDEN.search(no_strings).group(0)}")
        return s

    def governed_query(self, wh, role: str, sql: str, catalog_tables: pd.DataFrame,
                       catalog_columns: pd.DataFrame, limit: int = 200) -> tuple[pd.DataFrame, dict]:
        try:
            s = self.validate_sql(sql)
            refs = sorted({f"{l}.{t}" for l, t in TABLE_REF.findall(s)})
            if not refs:
                raise AccessDenied("Query must reference tables as layer.table (e.g. gold.customer_360)")
            known = set(catalog_tables["fq_name"]) if not catalog_tables.empty else set()
            for ref in refs:
                if ref not in known:
                    raise AccessDenied(f"Unknown table {ref}")
                row = catalog_tables[catalog_tables["fq_name"] == ref].iloc[0]
                if not self.can_access(role, row["layer"], row["domain"]):
                    raise AccessDenied(f"Role '{role}' is not allowed to read {ref} "
                                       f"(layer={row['layer']}, domain={row['domain']})")
            df = wh.query(f"SELECT * FROM (\n{wh.physicalize(s)}\n) AS q LIMIT {int(limit)}")
            df, masked = self.mask_df(role, df, column_classifications(catalog_columns, refs))
            info = {"tables": refs, "masked_columns": masked, "rows": len(df)}
            store.audit(wh, role, "QUERY", ", ".join(refs), "OK", {"sql": s, **info})
            return df, info
        except AccessDenied as e:
            store.audit(wh, role, "QUERY", "", "DENIED", {"sql": sql, "reason": str(e)})
            raise


def bigquery_governance_script(cfg: dict, catalog_tables: pd.DataFrame, catalog_columns: pd.DataFrame) -> str:
    """Native BigQuery enforcement of the same policies (run in gcp mode / by your CI)."""
    p = cfg["policies"]
    project = cfg["gcp"]["project_id"]
    ds = cfg["datasets"]
    loc = cfg["gcp"]["vertex_region"]
    out = ["-- ===== Enterprise Data Management Suite: BigQuery governance =====",
           "-- 1) Dataset-level access (BigQuery DCL)"]
    for role, r in p["roles"].items():
        for layer in r["layers"]:
            out.append(f'GRANT `roles/bigquery.dataViewer` ON SCHEMA `{project}.{ds[layer]}` TO "{r["principal"]}";  -- {role}')
    out.append("\n-- 2) Table expiration (retention) and labels")
    for t in catalog_tables.itertuples():
        days = p.get("retention_days", {}).get(t.layer)
        labels = f'[("domain", "{t.domain}"), ("classification", "{t.classification.lower()}"), ("layer", "{t.layer}")]'
        desc = str(t.description).replace('"', "'")
        out.append(f'ALTER TABLE `{project}.{ds[t.layer]}.{t.table_name}` SET OPTIONS (description = "{desc}", '
                   f'labels = {labels}' + (f', expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)'
                                          if t.layer == "bronze" and days else "") + ");")
    out.append("\n-- 3) Column descriptions")
    for c in catalog_columns.itertuples():
        desc = str(c.description).replace('"', "'")
        out.append(f'ALTER TABLE `{project}.{ds[c.layer]}.{c.table_name}` ALTER COLUMN {c.column_name} '
                   f'SET OPTIONS (description = "{desc} [{c.classification}]");')
    out.append("\n-- 4) Row-level security example: analysts only see their commercial domains")
    if "gold" in ds:
        out.append(f'CREATE OR REPLACE ROW ACCESS POLICY analyst_regions ON `{project}.{ds["gold"]}.revenue_by_month` '
                   f'GRANT TO ("{p["roles"]["business_analyst"]["principal"]}") FILTER USING (TRUE);')
    sens = catalog_columns[catalog_columns["classification"].isin(["CONFIDENTIAL", "RESTRICTED"])]
    out.append("\n# 5) Column-level security: policy-tag taxonomy (run with gcloud / Data Catalog API)")
    out.append(f"# gcloud data-catalog taxonomies create --location={loc} --display-name='EDMS Classification' "
               "--activated-policy-types=FINE_GRAINED_ACCESS_CONTROL")
    for lvl in ("CONFIDENTIAL", "RESTRICTED"):
        out.append(f"#   policy tag: {lvl} -> masking rule: {p.get('masking', {}).get(lvl)} "
                   f"(SHA256 for tokenize, ALWAYS_NULL/DEFAULT for redact)")
    out.append(f"# Attach policy tags to {len(sens)} sensitive columns:")
    for c in sens.itertuples():
        out.append(f"#   {project}.{ds[c.layer]}.{c.table_name}.{c.column_name} -> {c.classification}")
    return "\n".join(out)
