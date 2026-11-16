"""Intelligent profiling: type inference, primary-key detection, PII discovery and
classification. Runs on a sample of each Bronze table and drives Silver SQL generation
and governance tagging - no hand-written mappings per table.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

import pandas as pd

# note: "NA" is deliberately NOT a null token (it is a valid code, e.g. North America)
NULL_TOKENS = {"", "null", "none", "n/a", "nan", "-", "nil", "<na>"}

RX = {
    "int": re.compile(r"^[+-]?\d+$"),
    "float": re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$"),
    "date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "timestamp": re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$"),
    "bool": re.compile(r"^(true|false|yes|no|y|n|t|f)$", re.I),
    "email": re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$"),
    "ssn": re.compile(r"^\d{3}-\d{2}-\d{4}$"),
    "card": re.compile(r"^(?:\d[ -]?){12,18}\d$"),
    "phone": re.compile(r"^\+?[\d\s().-]{7,}$"),
    "ip": re.compile(r"^(\d{1,3}\.){3}\d{1,3}$"),
}

NAME_HINTS = [  # (regex on column name, pii_type)
    (r"(^|_)(ssn|social_security|tax_id|national_id|passport)($|_)", "GOV_ID"),
    (r"(card_number|credit_card|^pan$|card_no)", "CREDIT_CARD"),
    (r"(dob|date_of_birth|birth_date|birthdate)", "DATE_OF_BIRTH"),
    (r"email", "EMAIL"),
    (r"(phone|mobile|msisdn)", "PHONE"),
    (r"(^|_)(first_name|last_name|full_name|surname|given_name)($|_)", "PERSON_NAME"),
    (r"(address|street|postcode|postal_code|zip)", "ADDRESS"),
    (r"(salary|compensation|wage|bonus)", "COMPENSATION"),
    (r"(^|_)ip(_address)?$", "IP_ADDRESS"),
]

PII_CLASSIFICATION = {
    "GOV_ID": "RESTRICTED", "CREDIT_CARD": "RESTRICTED", "DATE_OF_BIRTH": "RESTRICTED",
    "EMAIL": "CONFIDENTIAL", "PHONE": "CONFIDENTIAL", "PERSON_NAME": "CONFIDENTIAL",
    "ADDRESS": "CONFIDENTIAL", "COMPENSATION": "CONFIDENTIAL", "IP_ADDRESS": "CONFIDENTIAL",
}
LEVELS = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]


@dataclass
class ColumnProfile:
    name: str
    inferred_type: str = "STRING"
    null_ratio: float = 0.0
    distinct_ratio: float = 0.0
    sample_values: list = field(default_factory=list)
    strip_currency: bool = False
    pii_type: str | None = None
    classification: str = "INTERNAL"
    avg_length: float = 0.0
    description: str = ""


@dataclass
class TableProfile:
    table: str
    rows_sampled: int
    primary_key: str | None
    columns: list[ColumnProfile]

    def column(self, name: str) -> ColumnProfile | None:
        return next((c for c in self.columns if c.name == name), None)

    def to_records(self) -> list[dict]:
        return [{"table_name": self.table, "primary_key": self.primary_key, **asdict(c),
                 "sample_values": ", ".join(map(str, c.sample_values))} for c in self.columns]


def normalize_name(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name).strip())
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower()
    return f"c_{s}" if not s or s[0].isdigit() else s


def normalize_columns(cols: list[str]) -> list[str]:
    out, seen = [], {}
    for c in cols:
        n = normalize_name(c)
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 0
        out.append(n)
    return out


def _share(values: pd.Series, rx: re.Pattern) -> float:
    return float(values.map(lambda v: bool(rx.match(v))).mean()) if len(values) else 0.0


def detect_pii(name: str, values: pd.Series) -> str | None:
    for pattern, kind in NAME_HINTS:
        if re.search(pattern, name):
            return kind
    if not len(values):
        return None
    if _share(values, RX["email"]) > 0.5:
        return "EMAIL"
    if _share(values, RX["ssn"]) > 0.5:
        return "GOV_ID"
    digits = values.map(lambda v: sum(ch.isdigit() for ch in v))
    if _share(values, RX["card"]) > 0.5 and digits.median() >= 13:
        return "CREDIT_CARD"
    if (_share(values, RX["phone"]) > 0.5 and 9 <= digits.median() <= 15
            and values.str.contains(r"[\s()+-]", regex=True).mean() > 0.5 and _share(values, RX["date"]) < 0.5):
        return "PHONE"
    if _share(values, RX["ip"]) > 0.5:
        return "IP_ADDRESS"
    return None


def infer_type(values: pd.Series, threshold: float) -> tuple[str, bool]:
    if not len(values):
        return "STRING", False
    if _share(values, RX["int"]) >= threshold:
        # identifiers with leading zeros (zip codes, account numbers) stay strings
        if values.str.match(r"^0\d+").mean() > 0.05:
            return "STRING", False
        return "INT64", False
    if _share(values, RX["float"]) >= threshold:
        return "FLOAT64", False
    cleaned = values.str.replace(r"[$,]", "", regex=True)
    if values.str.contains(r"[$,]", regex=True).any() and _share(cleaned, RX["float"]) >= threshold:
        return "FLOAT64", True
    if _share(values, RX["date"]) >= threshold:
        return "DATE", False
    if _share(values, RX["timestamp"]) + _share(values, RX["date"]) >= threshold and _share(values, RX["timestamp"]) > 0:
        return "TIMESTAMP", False
    if _share(values, RX["bool"]) >= threshold:
        return "BOOL", False
    return "STRING", False


def describe_column(table: str, name: str, ctype: str, pii: str | None) -> str:
    words = name.replace("_", " ")
    if name.endswith("_id"):
        ref = name[:-3].replace("_", " ")
        return f"Identifier of the {ref}" + ("" if ref.rstrip("s") in table else f" (references {ref})")
    if pii:
        return f"{words.capitalize()} - personal data ({pii.replace('_', ' ').lower()})"
    if ctype in ("DATE", "TIMESTAMP"):
        return f"{words.capitalize()} ({ctype.lower()})"
    if ctype in ("INT64", "FLOAT64"):
        return f"{words.capitalize()} (numeric measure)"
    return words.capitalize()


def detect_primary_key(table: str, df: pd.DataFrame, cols: list[ColumnProfile]) -> str | None:
    singular = table[:-1] if table.endswith("s") else table
    candidates = [f"{singular}_id", f"{table}_id", "id"]
    for c in candidates:
        if c in df.columns:
            return c
    best, best_ratio = None, 0.0
    for c in cols:
        if c.name.endswith("_id") and c.distinct_ratio > best_ratio:
            best, best_ratio = c.name, c.distinct_ratio
    return best if best_ratio >= 0.9 else None


def profile_table(table: str, df: pd.DataFrame, threshold: float = 0.95) -> TableProfile:
    meta = {"_ingested_at", "_batch_id", "_source_uri"}
    cols: list[ColumnProfile] = []
    n = len(df)
    for name in [c for c in df.columns if c not in meta]:
        s = df[name].astype("string").str.strip()
        valid = s[~s.isna() & ~s.str.lower().isin(NULL_TOKENS)].astype(str)
        ctype, currency = infer_type(valid, threshold)
        pii = detect_pii(name, valid)
        cols.append(ColumnProfile(
            name=name, inferred_type=ctype, strip_currency=currency,
            null_ratio=round(1 - len(valid) / n, 4) if n else 0.0,
            distinct_ratio=round(valid.nunique() / len(valid), 4) if len(valid) else 0.0,
            sample_values=[] if pii else valid.drop_duplicates().head(3).tolist(),
            pii_type=pii, classification=PII_CLASSIFICATION.get(pii, "INTERNAL"),
            avg_length=round(float(valid.str.len().mean()), 1) if len(valid) else 0.0,
            description=describe_column(table, name, ctype, pii)))
    return TableProfile(table=table, rows_sampled=n, primary_key=detect_primary_key(table, df, cols), columns=cols)


def max_level(levels) -> str:
    levels = [lv for lv in levels if lv in LEVELS] or ["INTERNAL"]
    return max(levels, key=LEVELS.index)
