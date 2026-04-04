#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-sql-agent-5b660}"
SERVICE_NAME="${SERVICE_NAME:-sql-agent-api}"
REGION="${REGION:-us-central1}"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud CLI is required." >&2
  exit 1
fi

if [[ -z "${GROQ_API_KEY:-}" ]]; then
  echo "GROQ_API_KEY must be set in the environment." >&2
  exit 1
fi

gcloud config set project "${PROJECT_ID}" >/dev/null

gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --allow-unauthenticated \
  --set-env-vars "GROQ_API_KEY=${GROQ_API_KEY},FIREBASE_PROJECT_ID=${PROJECT_ID},DATA_DIR=/tmp"

gcloud run services describe "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format='value(status.url)'

