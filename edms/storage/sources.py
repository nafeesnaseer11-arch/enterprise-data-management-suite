"""AWS source connectors with automatic discovery.

Discovery turns the whole AWS estate into a list of :class:`SourceDataset` objects:
every top-level prefix in an S3 bucket and every table in an RDS database becomes
one dataset. In ``local`` mode the same logic runs against folders / SQLite files.
"""
from __future__ import annotations

import io
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from edms.config import resolve

FORMATS = {".csv": "csv", ".json": "json", ".jsonl": "json", ".ndjson": "json", ".parquet": "parquet"}


@dataclass
class SourceDataset:
    name: str                  # target table name
    system: str                # s3 | rds
    container: str             # bucket or database name
    location: str              # s3://bucket/prefix/ or rds://db/table
    format: str                # csv | json | parquet | table
    domain: str
    owner: str
    objects: list[str] = field(default_factory=list)   # object keys / table name
    size_bytes: int = 0

    def as_dict(self) -> dict:
        return {"dataset": self.name, "system": self.system, "container": self.container,
                "location": self.location, "format": self.format, "domain": self.domain,
                "owner": self.owner, "objects": len(self.objects), "size_kb": round(self.size_bytes / 1024, 1)}


def _to_strings(df: pd.DataFrame) -> pd.DataFrame:
    """Bronze keeps everything as text, exactly as found in the source."""
    def conv(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        if isinstance(v, (dict, list)):
            return json.dumps(v)
        return str(v)
    return pd.DataFrame({c: df[c].map(conv) for c in df.columns}, columns=df.columns).astype("string")


def _read_bytes(data: bytes, fmt: str) -> pd.DataFrame:
    if fmt == "csv":
        return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
    if fmt == "json":
        text = data.decode("utf-8").strip()
        if text.startswith("["):
            return pd.DataFrame(json.loads(text))
        return pd.DataFrame([json.loads(line) for line in text.splitlines() if line.strip()])
    if fmt == "parquet":
        return pd.read_parquet(io.BytesIO(data))
    raise ValueError(fmt)


class S3Source:
    """Real S3 via boto3 (gcp mode) or a local folder per bucket (local mode)."""

    def __init__(self, bucket_cfg: dict, local_root: str | None = None, region: str | None = None):
        self.cfg = bucket_cfg
        self.bucket = bucket_cfg["name"]
        self.local_root = resolve(local_root) / self.bucket if local_root else None
        self.region = region
        self._s3 = None

    @property
    def s3(self):
        if self._s3 is None:
            import boto3
            self._s3 = boto3.client("s3", region_name=self.region)
        return self._s3

    def _list(self) -> list[tuple[str, int]]:
        if self.local_root is not None:
            if not self.local_root.exists():
                return []
            return [(str(p.relative_to(self.local_root)), p.stat().st_size)
                    for p in sorted(self.local_root.rglob("*")) if p.is_file()]
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
            out += [(o["Key"], o["Size"]) for o in page.get("Contents", [])]
        return out

    def discover(self) -> list[SourceDataset]:
        groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for key, size in self._list():
            ext = Path(key).suffix.lower()
            if ext not in FORMATS or "/" not in key:
                continue
            groups[key.split("/")[0]].append((key, size))
        out = []
        for prefix, objs in groups.items():
            fmts = {FORMATS[Path(k).suffix.lower()] for k, _ in objs}
            fmt = fmts.pop() if len(fmts) == 1 else "mixed"
            out.append(SourceDataset(
                name=prefix.replace("-", "_").lower(), system="s3", container=self.bucket,
                location=f"s3://{self.bucket}/{prefix}/", format=fmt,
                domain=self.cfg.get("domain", "general"), owner=self.cfg.get("owner", ""),
                objects=[k for k, _ in objs], size_bytes=sum(s for _, s in objs)))
        return out

    def read_object(self, key: str) -> bytes:
        if self.local_root is not None:
            return (self.local_root / key).read_bytes()
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def extract(self, ds: SourceDataset):
        """Yield (object_key, raw_bytes, dataframe_as_strings) per object."""
        for key in ds.objects:
            raw = self.read_object(key)
            df = _read_bytes(raw, FORMATS[Path(key).suffix.lower()])
            yield key, raw, _to_strings(df)


class RDSSource:
    """Relational source (RDS PostgreSQL / MySQL / Aurora via SQLAlchemy, SQLite locally)."""

    def __init__(self, db_cfg: dict):
        self.cfg = db_cfg
        self.name = db_cfg["name"]
        self.url = db_cfg["url"]

    def _connect(self):
        if self.url.startswith("sqlite:///"):
            return sqlite3.connect(resolve(self.url.removeprefix("sqlite:///")))
        from sqlalchemy import create_engine
        return create_engine(self.url).connect()

    def _tables(self) -> list[str]:
        if self.url.startswith("sqlite:///"):
            if not resolve(self.url.removeprefix("sqlite:///")).exists():
                return []
            with self._connect() as con:
                rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1").fetchall()
            return [r[0] for r in rows]
        from sqlalchemy import create_engine, inspect
        return inspect(create_engine(self.url)).get_table_names()

    def discover(self) -> list[SourceDataset]:
        return [SourceDataset(name=t.lower(), system="rds", container=self.name,
                              location=f"rds://{self.name}/{t}", format="table",
                              domain=self.cfg.get("domain", "general"), owner=self.cfg.get("owner", ""),
                              objects=[t]) for t in self._tables()]

    def extract(self, ds: SourceDataset):
        con = self._connect()
        try:
            for table in ds.objects:
                # chunked reads keep memory flat for large tables
                for i, chunk in enumerate(pd.read_sql(f'SELECT * FROM "{table}"', con, chunksize=100_000)):
                    df = _to_strings(chunk)
                    buf = io.BytesIO()
                    df.to_parquet(buf, index=False)
                    yield f"{table}/part-{i:05d}.parquet", buf.getvalue(), df
        finally:
            con.close()


def make_sources(cfg: dict) -> list:
    local = cfg["mode"] == "local"
    root = cfg["local"]["aws_s3_root"] if local else None
    srcs: list = [S3Source(b, root, cfg["aws"].get("region")) for b in cfg["aws"].get("s3_buckets", [])]
    srcs += [RDSSource(d) for d in cfg["aws"].get("rds_databases", [])]
    return srcs


def discover_all(sources: list) -> list[tuple[object, SourceDataset]]:
    seen, out = set(), []
    for src in sources:
        for ds in src.discover():
            if ds.name in seen:  # name collision across buckets -> qualify with container
                ds.name = f"{ds.container.replace('-', '_')}__{ds.name}"
            seen.add(ds.name)
            out.append((src, ds))
    return out
