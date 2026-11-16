"""Helpers to persist governance metadata inside the warehouse governance dataset."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def read(wh, table: str) -> pd.DataFrame:
    try:
        return wh.read_table("governance", table)
    except Exception:
        return pd.DataFrame()


def append(wh, table: str, rows: list[dict] | pd.DataFrame) -> None:
    df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    if not df.empty:
        wh.write_df("governance", table, df, mode="append")


def upsert(wh, table: str, rows: list[dict] | pd.DataFrame, keys: list[str]) -> None:
    """Replace rows whose key columns match the new rows; keep everything else."""
    new = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    if new.empty:
        return
    old = read(wh, table)
    if not old.empty and all(k in old.columns for k in keys):
        new_keys = set(map(tuple, new[keys].astype(str).values))
        mask = old[keys].astype(str).apply(tuple, axis=1).isin(new_keys)
        new = pd.concat([old[~mask], new], ignore_index=True)
    wh.write_df("governance", table, new, mode="replace")


def audit(wh, role: str, action: str, target: str = "", status: str = "OK", details: dict | None = None) -> None:
    append(wh, "audit_log", [{"event_time": now(), "role": role, "action": action, "target": target,
                              "status": status, "details": json.dumps(details or {}, default=str)[:4000]}])
