#!/usr/bin/env bash
# Deploy the Enterprise Data Management Suite to Google Cloud (Cloud Run).
#
# Run from the repository root in Cloud Shell (already authenticated as you):
#   ./deploy/deploy.sh --project MY_PROJECT                 # demo: sample AWS estate, self-contained
#   ./deploy/deploy.sh --project MY_PROJECT --mode gcp      # production: real AWS -> GCS -> BigQuery
#
# Options:
#   --project ID        GCP project (required)
#   --region R          Cloud Run / Vertex region            (default: us-central1)
#   --bq-location L     BigQuery + landing bucket location   (default: US)
#   --mode demo|gcp     demo  = UI with built-in sample data (DuckDB inside the container, resets on restart)
#                       gcp   = BigQuery medallion, GCS landing, Vertex embeddings, nightly pipeline job
#   --public            allow unauthenticated access to the UI (default: private, IAM-protected)
#   --schedule CRON     nightly pipeline schedule in gcp mode  (default: "17 2 * * *")
#   --skip-secrets      don't prompt for API keys (reuse the existing Secret Manager values)
set -euo pipefail

PROJECT="" REGION="us-central1" BQ_LOCATION="US" MODE="demo" PUBLIC=false SCHEDULE="17 2 * * *" SKIP_SECRETS=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --bq-location) BQ_LOCATION="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --public) PUBLIC=true; shift ;;
    --schedule) SCHEDULE="$2"; shift 2 ;;
    --skip-secrets) SKIP_SECRETS=true; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown option $1"; exit 1 ;;
  esac
done
[[ -n "$PROJECT" ]] || { echo "--project is required"; exit 1; }
[[ "$MODE" == demo || "$MODE" == gcp ]] || { echo "--mode must be demo or gcp"; exit 1; }
cd "$(dirname "$0")/.."

SA_NAME="edms-app"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
REPO="edms"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/edms:$(date +%Y%m%d-%H%M%S)"
BUCKET="${PROJECT}-edms-landing"
SERVICE="edms"
JOB="edms-pipeline"
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

gcloud config set project "$PROJECT" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

if [[ "$MODE" == gcp ]] && grep -q "name: acme-" config/settings.yaml; then
  echo "WARNING: config/settings.yaml still lists the sample 'acme-*' S3 buckets."
  echo "Edit aws.s3_buckets / aws.rds_databases to your real estate first (Ctrl-C to abort, Enter to continue)."
  read -r _
fi

step "Enabling APIs"
APIS=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com iam.googleapis.com)
[[ "$MODE" == gcp ]] && APIS+=(bigquery.googleapis.com storage.googleapis.com aiplatform.googleapis.com cloudscheduler.googleapis.com)
gcloud services enable "${APIS[@]}"

step "Service account ${SA}"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" --display-name "Enterprise Data Management Suite"
if [[ "$MODE" == gcp ]]; then
  for role in roles/bigquery.dataEditor roles/bigquery.jobUser roles/aiplatform.user roles/run.invoker; do
    gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:${SA}" --role "$role" --condition=None >/dev/null
  done
fi
# Cloud Build in newer projects runs as the Compute default SA; make sure it can build and push images.
for role in roles/cloudbuild.builds.builder roles/artifactregistry.writer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:${COMPUTE_SA}" --role "$role" \
    --condition=None >/dev/null 2>&1 || true
done

step "Secrets (values are typed here and stored in Secret Manager, never in files)"
ensure_secret() {  # name, prompt
  local name="$1" prompt="$2" value
  gcloud secrets describe "$name" >/dev/null 2>&1 || gcloud secrets create "$name" --replication-policy=automatic >/dev/null
  if [[ "$SKIP_SECRETS" == false ]]; then
    read -r -s -p "$prompt (leave empty to keep the current value): " value; echo
    if [[ -n "$value" ]]; then printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null; fi
  fi
  gcloud secrets add-iam-policy-binding "$name" --member "serviceAccount:${SA}" \
    --role roles/secretmanager.secretAccessor >/dev/null
}
has_version() { [[ -n "$(gcloud secrets versions list "$1" --filter='state=ENABLED' --limit=1 --format='value(name)' 2>/dev/null)" ]]; }

ensure_secret edms-anthropic-api-key "Anthropic API key for the Meta RAG chatbot"
SECRETS=()
has_version edms-anthropic-api-key && SECRETS+=("ANTHROPIC_API_KEY=edms-anthropic-api-key:latest")
if [[ "$MODE" == gcp ]]; then
  ensure_secret edms-aws-access-key-id "AWS access key ID (read-only on the source buckets / RDS)"
  ensure_secret edms-aws-secret-access-key "AWS secret access key"
  has_version edms-aws-access-key-id && SECRETS+=("AWS_ACCESS_KEY_ID=edms-aws-access-key-id:latest")
  has_version edms-aws-secret-access-key && SECRETS+=("AWS_SECRET_ACCESS_KEY=edms-aws-secret-access-key:latest")
fi
SECRET_FLAG=()
if [[ ${#SECRETS[@]} -gt 0 ]]; then SECRET_FLAG=(--set-secrets "$(IFS=,; echo "${SECRETS[*]}")"); fi
has_version edms-anthropic-api-key || echo "No Anthropic key stored: the chatbot will run in offline mode."

if [[ "$MODE" == gcp ]]; then
  step "Landing bucket gs://${BUCKET}"
  gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1 \
    || gcloud storage buckets create "gs://${BUCKET}" --location "$BQ_LOCATION" --uniform-bucket-level-access
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member "serviceAccount:${SA}" \
    --role roles/storage.objectAdmin >/dev/null
fi

step "Building the container image with Cloud Build"
gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION"
gcloud builds submit --tag "$IMAGE" .

if [[ "$MODE" == gcp ]]; then
  ENV_VARS="EDMS_MODE=gcp,EDMS_GCP_PROJECT=${PROJECT},EDMS_BQ_LOCATION=${BQ_LOCATION},EDMS_LANDING_BUCKET=${BUCKET},EDMS_VERTEX_REGION=${REGION},EDMS_EMBEDDER=vertex"
  SCALE=(--min-instances 0 --max-instances 3)
else
  # demo: DuckDB lives inside the container, so keep a single instance and bootstrap sample data on start
  ENV_VARS="EDMS_MODE=local,EDMS_AUTO_BOOTSTRAP=1"
  SCALE=(--min-instances 0 --max-instances 1)
fi

step "Deploying the UI to Cloud Run service '${SERVICE}'"
AUTH_FLAG=(--no-allow-unauthenticated)
[[ "$PUBLIC" == true ]] && AUTH_FLAG=(--allow-unauthenticated)
gcloud run deploy "$SERVICE" --image "$IMAGE" --region "$REGION" --service-account "$SA" \
  --set-env-vars "$ENV_VARS" "${SECRET_FLAG[@]}" --memory 2Gi --cpu 2 --timeout 3600 \
  --session-affinity "${SCALE[@]}" "${AUTH_FLAG[@]}"

if [[ "$MODE" == gcp ]]; then
  step "Deploying the pipeline as Cloud Run job '${JOB}'"
  gcloud run jobs deploy "$JOB" --image "$IMAGE" --region "$REGION" --service-account "$SA" \
    --set-env-vars "$ENV_VARS" "${SECRET_FLAG[@]}" --memory 4Gi --cpu 2 --task-timeout 3600 --max-retries 1 \
    --command python --args="-m,scripts.run_pipeline"

  step "Scheduling the pipeline (${SCHEDULE}, UTC)"
  URI="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run"
  if gcloud scheduler jobs describe edms-nightly --location "$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs update http edms-nightly --location "$REGION" --schedule "$SCHEDULE" --uri "$URI" \
      --http-method POST --oauth-service-account-email "$SA"
  else
    gcloud scheduler jobs create http edms-nightly --location "$REGION" --schedule "$SCHEDULE" --uri "$URI" \
      --http-method POST --oauth-service-account-email "$SA"
  fi
fi

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
step "Done"
echo "UI: ${URL}"
if [[ "$PUBLIC" == false ]]; then
  echo "The UI is private. To open it from Cloud Shell:"
  echo "  gcloud run services proxy ${SERVICE} --region ${REGION} --port 8080   # then Web Preview -> port 8080"
fi
[[ "$MODE" == gcp ]] && echo "Run the migration now: gcloud run jobs execute ${JOB} --region ${REGION} --wait"
exit 0
