"""Landing zone (GCS in production, a local folder otherwise).

Raw objects are copied byte-for-byte from AWS into the landing bucket before anything
is parsed, so the migration always keeps an immutable copy of the source.
For multi-TB buckets use Storage Transfer Service (see infra/terraform) and run the
pipeline with the files already in the landing bucket.
"""
from __future__ import annotations

from edms.config import resolve


class LocalLake:
    def __init__(self, root: str):
        self.root = resolve(root)

    def put(self, path: str, data: bytes) -> str:
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return f"gs://local-landing/{path}"


class GCSLake:
    def __init__(self, project: str, bucket: str):
        from google.cloud import storage

        self.bucket_name = bucket
        self.bucket = storage.Client(project=project).bucket(bucket)

    def put(self, path: str, data: bytes) -> str:
        self.bucket.blob(path).upload_from_string(data)
        return f"gs://{self.bucket_name}/{path}"


def make_lake(cfg: dict):
    if cfg["mode"] == "gcp":
        return GCSLake(cfg["gcp"]["project_id"], cfg["gcp"]["landing_bucket"])
    return LocalLake(cfg["local"]["gcs_landing_root"])
