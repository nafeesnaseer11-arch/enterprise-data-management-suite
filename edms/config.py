"""Configuration loading for the Enterprise Data Management Suite."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LAYERS = ("bronze", "silver", "gold")
ALL_LAYERS = ("bronze", "silver", "gold", "governance")


def resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def load_yaml(path: str | Path) -> dict:
    with open(resolve(path)) as f:
        return yaml.safe_load(f) or {}


def load_settings(path: str | Path | None = None) -> dict:
    cfg = load_yaml(path or os.getenv("EDMS_CONFIG", "config/settings.yaml"))
    cfg["mode"] = os.getenv("EDMS_MODE", cfg.get("mode", "local")).lower()
    # environment overrides used by the Cloud Run deployment (see deploy/deploy.sh)
    overrides = {
        "EDMS_GCP_PROJECT": ("gcp", "project_id"),
        "EDMS_BQ_LOCATION": ("gcp", "location"),
        "EDMS_LANDING_BUCKET": ("gcp", "landing_bucket"),
        "EDMS_VERTEX_REGION": ("gcp", "vertex_region"),
        "EDMS_EMBEDDER": ("rag", "embedder"),
    }
    for env, (section, key) in overrides.items():
        if os.getenv(env):
            cfg[section][key] = os.environ[env]
    if os.getenv("EDMS_LLM_PROVIDER"):
        cfg["rag"]["llm"]["provider"] = os.environ["EDMS_LLM_PROVIDER"]
    cfg["policies"] = load_yaml(cfg["governance"]["policies"])
    cfg["gold_models"] = load_yaml(cfg["etl"]["gold_models"]).get("models", [])
    cfg["glossary"] = load_yaml(cfg["rag"]["glossary"]).get("terms", [])
    return cfg
