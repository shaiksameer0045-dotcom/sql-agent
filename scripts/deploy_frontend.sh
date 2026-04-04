#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-sql-agent-5b660}"
BACKEND_URL="${BACKEND_URL:-}"
CONFIG_FILE="static/firebase-config.js"

if ! command -v firebase >/dev/null 2>&1; then
  echo "firebase CLI is required." >&2
  exit 1
fi

if [[ -z "${BACKEND_URL}" ]]; then
  echo "BACKEND_URL must be set to your deployed Cloud Run service URL." >&2
  exit 1
fi

python3 - <<'PY'
from pathlib import Path
import os
import re

path = Path("static/firebase-config.js")
text = path.read_text()
backend = os.environ["BACKEND_URL"].rstrip("/")
text = re.sub(r'apiBaseUrl: "[^"]*"', f'apiBaseUrl: "{backend}"', text)
text = re.sub(r'wsBaseUrl: "[^"]*"', f'wsBaseUrl: "{backend}"', text)
path.write_text(text)
PY

firebase use "${PROJECT_ID}"
firebase deploy --only hosting

