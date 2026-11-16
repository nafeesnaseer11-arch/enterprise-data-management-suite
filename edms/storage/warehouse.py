"""Warehouse abstraction: BigQuery in production, DuckDB as a local stand-in.

All SQL in the suite is written once against *logical* names (``silver.orders``) and
small macros, then compiled for the active dialect by :meth:`Warehouse.render`.
"""
from __future__ import annotations

import re
import threading
from abc import ABC, abstractmethod

import pandas as pd

from edms.config import resolve

_TEMPLATE = re.compile(r"\{\{\s*(\w+)\s*\((.*?)\)\s*\}\}", re.S)
_LOGICAL_REF = re.compile(r"(?<![\w.`])(bronze|silver|gold|governance)\.([A-Za-z_][\w]*)\b")

TYPE_MAP = {
    "duckdb": {"INT64": "BIGINT", "FLOAT64": "DOUBLE", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP",
               "BOOL": "BOOLEAN", "STRING": "VARCHAR"},
    "bigquery": {"INT64": "INT64", "FLOAT64": "FLOAT64", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP",
                 "BOOL": "BOOL", "STRING": "STRING"},
}


def _split_args(s: str) -> list[str]:
    args, depth, cur = [], 0, ""
    for ch in s:
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
            continue
        depth += ch == "("
        depth -= ch == ")"
        cur += ch
    if cur.strip():
        args.append(cur.strip())
    return [a.strip("'\"") for a in args]


class Warehouse(ABC):
    dialect: str

    def __init__(self, datasets: dict[str, str]):
        self.datasets = datasets

    # ---------- naming ----------
    @abstractmethod
    def fq(self, layer: str, table: str) -> str: ...

    def physicalize(self, sql: str) -> str:
        """Rewrite logical ``layer.table`` references to physical names."""
        return _LOGICAL_REF.sub(lambda m: self.fq(m.group(1), m.group(2)), sql)

    # ---------- macros ----------
    def macro(self, name: str, args: list[str]) -> str:
        d = self.dialect
        if name == "ref":
            return self.fq(args[0], args[1])
        if name == "safe_cast":
            t = TYPE_MAP[d][args[1]]
            return f"{'TRY_CAST' if d == 'duckdb' else 'SAFE_CAST'}({args[0]} AS {t})"
        if name == "strip_currency":
            return (f"regexp_replace({args[0]}, '[$,]', '', 'g')" if d == "duckdb"
                    else f"REGEXP_REPLACE({args[0]}, r'[$,]', '')")
        if name == "month":
            return (f"strftime(CAST({args[0]} AS DATE), '%Y-%m')" if d == "duckdb"
                    else f"FORMAT_DATE('%Y-%m', DATE({args[0]}))")
        if name == "hours_between":
            return (f"date_diff('minute', {args[0]}, {args[1]}) / 60.0" if d == "duckdb"
                    else f"TIMESTAMP_DIFF({args[1]}, {args[0]}, MINUTE) / 60.0")
        if name == "years_since":
            return (f"date_diff('day', {args[0]}, current_date) / 365.25" if d == "duckdb"
                    else f"DATE_DIFF(CURRENT_DATE(), {args[0]}, DAY) / 365.25")
        if name == "regex_match":
            return (f"regexp_matches({args[0]}, '{args[1]}')" if d == "duckdb"
                    else f"REGEXP_CONTAINS({args[0]}, r'{args[1]}')")
        raise ValueError(f"Unknown macro: {name}")

    def render(self, sql: str) -> str:
        prev = None
        while prev != sql:  # allow nested macros
            prev = sql
            sql = _TEMPLATE.sub(lambda m: self.macro(m.group(1), _split_args(m.group(2))), sql)
        return sql

    @staticmethod
    def refs(sql: str) -> list[tuple[str, str]]:
        out = []
        for m in _TEMPLATE.finditer(sql):
            if m.group(1) == "ref":
                a = _split_args(m.group(2))
                out.append((a[0], a[1]))
        return out

    # ---------- operations ----------
    @abstractmethod
    def ensure_datasets(self) -> None: ...

    @abstractmethod
    def query(self, sql: str, params: dict | None = None) -> pd.DataFrame: ...

    @abstractmethod
    def execute(self, sql: str) -> None: ...

    @abstractmethod
    def write_df(self, layer: str, table: str, df: pd.DataFrame, mode: str = "replace") -> None: ...

    @abstractmethod
    def list_tables(self, layer: str) -> list[str]: ...

    @abstractmethod
    def table_schema(self, layer: str, table: str) -> list[tuple[str, str]]: ...

    def table_exists(self, layer: str, table: str) -> bool:
        return table in self.list_tables(layer)

    def row_count(self, layer: str, table: str) -> int:
        return int(self.query(f"SELECT COUNT(*) AS n FROM {self.fq(layer, table)}")["n"].iloc[0])

    def read_table(self, layer: str, table: str, limit: int | None = None) -> pd.DataFrame:
        if not self.table_exists(layer, table):
            return pd.DataFrame()
        sql = f"SELECT * FROM {self.fq(layer, table)}"
        return self.query(sql + (f" LIMIT {int(limit)}" if limit else ""))

    def create_table_as(self, layer: str, table: str, select_sql: str) -> None:
        self.execute(f"CREATE OR REPLACE TABLE {self.fq(layer, table)} AS\n{select_sql}")


class DuckDBWarehouse(Warehouse):
    """Local BigQuery stand-in. Each medallion dataset is a DuckDB schema."""

    dialect = "duckdb"

    def __init__(self, path: str, datasets: dict[str, str]):
        super().__init__(datasets)
        import duckdb

        self.path = str(resolve(path))
        resolve(path).parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(self.path)
        self._lock = threading.RLock()

    def _cur(self):
        return self._con.cursor()

    def fq(self, layer: str, table: str) -> str:
        return f'{self.datasets.get(layer, layer)}."{table}"'

    def ensure_datasets(self) -> None:
        with self._lock:
            for ds in self.datasets.values():
                self._cur().execute(f"CREATE SCHEMA IF NOT EXISTS {ds}")

    def query(self, sql: str, params: dict | None = None) -> pd.DataFrame:
        with self._lock:
            cur = self._cur()
            return cur.execute(sql, params or None).df() if params else cur.execute(sql).df()

    def execute(self, sql: str) -> None:
        with self._lock:
            self._cur().execute(sql)

    def write_df(self, layer: str, table: str, df: pd.DataFrame, mode: str = "replace") -> None:
        with self._lock:
            cur = self._cur()
            cur.register("_edms_df", df)
            try:
                if mode == "append" and self.table_exists(layer, table):
                    cur.execute(f"INSERT INTO {self.fq(layer, table)} BY NAME SELECT * FROM _edms_df")
                else:
                    cur.execute(f"CREATE OR REPLACE TABLE {self.fq(layer, table)} AS SELECT * FROM _edms_df")
            finally:
                cur.unregister("_edms_df")

    def list_tables(self, layer: str) -> list[str]:
        df = self.query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = $s ORDER BY 1",
            {"s": self.datasets.get(layer, layer)},
        )
        return df["table_name"].tolist()

    def table_schema(self, layer: str, table: str) -> list[tuple[str, str]]:
        df = self.query(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = $s AND table_name = $t ORDER BY ordinal_position",
            {"s": self.datasets.get(layer, layer), "t": table},
        )
        rev = {v: k for k, v in TYPE_MAP["duckdb"].items()}
        return [(r.column_name, rev.get(r.data_type, r.data_type)) for r in df.itertuples()]


class BigQueryWarehouse(Warehouse):
    dialect = "bigquery"

    def __init__(self, project: str, location: str, datasets: dict[str, str]):
        super().__init__(datasets)
        from google.cloud import bigquery

        self.bq = bigquery
        self.project = project
        self.location = location
        self.client = bigquery.Client(project=project, location=location)

    def fq(self, layer: str, table: str) -> str:
        return f"`{self.project}.{self.datasets.get(layer, layer)}.{table}`"

    def ensure_datasets(self) -> None:
        for layer, ds in self.datasets.items():
            d = self.bq.Dataset(f"{self.project}.{ds}")
            d.location = self.location
            d.labels = {"suite": "edms", "layer": layer}
            self.client.create_dataset(d, exists_ok=True)

    def query(self, sql: str, params: dict | None = None) -> pd.DataFrame:
        job_config = None
        if params:
            qp = []
            for k, v in params.items():
                if isinstance(v, (list, tuple)):
                    qp.append(self.bq.ArrayQueryParameter(k, "FLOAT64" if v and isinstance(v[0], float) else "STRING", list(v)))
                else:
                    qp.append(self.bq.ScalarQueryParameter(k, "INT64" if isinstance(v, int) else "STRING", v))
            job_config = self.bq.QueryJobConfig(query_parameters=qp)
            sql = re.sub(r"\$(\w+)", r"@\1", sql)
        return self.client.query(sql, job_config=job_config).to_dataframe()

    def execute(self, sql: str) -> None:
        self.client.query(sql).result()

    def write_df(self, layer: str, table: str, df: pd.DataFrame, mode: str = "replace") -> None:
        cfg = self.bq.LoadJobConfig(write_disposition="WRITE_APPEND" if mode == "append" else "WRITE_TRUNCATE")
        if mode == "append":
            cfg.schema_update_options = ["ALLOW_FIELD_ADDITION"]
        ref = f"{self.project}.{self.datasets.get(layer, layer)}.{table}"
        self.client.load_table_from_dataframe(df, ref, job_config=cfg).result()

    def list_tables(self, layer: str) -> list[str]:
        ds = f"{self.project}.{self.datasets.get(layer, layer)}"
        return sorted(t.table_id for t in self.client.list_tables(ds))

    def table_schema(self, layer: str, table: str) -> list[tuple[str, str]]:
        t = self.client.get_table(f"{self.project}.{self.datasets.get(layer, layer)}.{table}")
        alias = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}
        return [(f.name, alias.get(f.field_type, f.field_type)) for f in t.schema]


def make_warehouse(cfg: dict) -> Warehouse:
    if cfg["mode"] == "gcp":
        return BigQueryWarehouse(cfg["gcp"]["project_id"], cfg["gcp"]["location"], cfg["datasets"])
    return DuckDBWarehouse(cfg["local"]["warehouse_path"], cfg["datasets"])
