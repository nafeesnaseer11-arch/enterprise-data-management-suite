# Enterprise Data Management Suite - GCP foundation
# terraform init && terraform apply -var project_id=... -var aws_access_key_id=... -var aws_secret_access_key=...

terraform {
  required_providers {
    google = { source = "hashicorp/google", version = ">= 5.0" }
  }
}

variable "project_id" { type = string }
variable "region" { default = "us-central1" }
variable "bq_location" { default = "US" }
variable "aws_s3_buckets" {
  type    = list(string)
  default = ["acme-sales-prod", "acme-support-prod", "acme-finance-prod"]
}
variable "aws_access_key_id" {
  type      = string
  sensitive = true
}
variable "aws_secret_access_key" {
  type      = string
  sensitive = true
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  services = ["bigquery.googleapis.com", "storage.googleapis.com", "storagetransfer.googleapis.com",
    "aiplatform.googleapis.com", "datacatalog.googleapis.com", "bigquerydatapolicy.googleapis.com",
  "run.googleapis.com", "secretmanager.googleapis.com", "cloudscheduler.googleapis.com"]
  layers = { bronze = "edms_bronze", silver = "edms_silver", gold = "edms_gold", governance = "edms_governance" }
  retention_days = { bronze = 90, silver = 730, gold = 1825, governance = 2555 }
}

resource "google_project_service" "apis" {
  for_each           = toset(local.services)
  service            = each.value
  disable_on_destroy = false
}

# ---------------- Landing zone (raw, immutable copy of AWS)
resource "google_storage_bucket" "landing" {
  name                        = "${var.project_id}-edms-landing"
  location                    = var.bq_location
  uniform_bucket_level_access = true
  versioning { enabled = true }
  lifecycle_rule {
    condition { age = 30 }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
}

# ---------------- Medallion datasets
resource "google_bigquery_dataset" "layer" {
  for_each   = local.layers
  dataset_id = each.value
  location   = var.bq_location
  labels     = { suite = "edms", layer = each.key }
  default_table_expiration_ms = each.key == "bronze" ? local.retention_days[each.key] * 86400000 : null
  depends_on = [google_project_service.apis]
}

# ---------------- Pipeline identity (least privilege)
resource "google_service_account" "pipeline" {
  account_id   = "edms-pipeline"
  display_name = "EDMS pipeline"
}

resource "google_project_iam_member" "pipeline_roles" {
  for_each = toset(["roles/bigquery.jobUser", "roles/aiplatform.user", "roles/datacatalog.categoryAdmin"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_bigquery_dataset_iam_member" "pipeline_editor" {
  for_each   = local.layers
  dataset_id = google_bigquery_dataset.layer[each.key].dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_storage_bucket_iam_member" "pipeline_landing" {
  bucket = google_storage_bucket.landing.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.pipeline.email}"
}

# ---------------- Bulk S3 -> GCS migration with Storage Transfer Service (daily incremental)
data "google_storage_transfer_project_service_account" "sts" {}

resource "google_storage_bucket_iam_member" "sts_writer" {
  bucket = google_storage_bucket.landing.name
  role   = "roles/storage.admin"
  member = "serviceAccount:${data.google_storage_transfer_project_service_account.sts.email}"
}

resource "google_storage_transfer_job" "s3_to_gcs" {
  for_each    = toset(var.aws_s3_buckets)
  description = "EDMS migration ${each.value} -> GCS landing"
  transfer_spec {
    aws_s3_data_source {
      bucket_name = each.value
      aws_access_key {
        access_key_id     = var.aws_access_key_id
        secret_access_key = var.aws_secret_access_key
      }
    }
    gcs_data_sink {
      bucket_name = google_storage_bucket.landing.name
      path        = "sts/${each.value}/"
    }
    transfer_options { overwrite_objects_already_existing_in_sink = false }
  }
  schedule {
    schedule_start_date {
      year  = 2026
      month = 1
      day   = 1
    }
    start_time_of_day {
      hours   = 1
      minutes = 0
      seconds = 0
      nanos   = 0
    }
  }
  depends_on = [google_storage_bucket_iam_member.sts_writer]
}

# ---------------- Secrets
resource "google_secret_manager_secret" "anthropic" {
  secret_id = "edms-anthropic-api-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "pipeline_secret" {
  secret_id = google_secret_manager_secret.anthropic.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.pipeline.email}"
}

output "landing_bucket" { value = google_storage_bucket.landing.name }
output "pipeline_service_account" { value = google_service_account.pipeline.email }
