#!/usr/bin/env bash
# QueryLux — full end-to-end test runner
# Runs against already-running Docker containers and local server.
# Usage:  ./run_tests.sh [--server-url http://host:port]

set -euo pipefail

SERVER_URL="${TEST_BASE_URL:-http://127.0.0.1:8766}"

# Allow override via arg
while [[ $# -gt 0 ]]; do
  case $1 in
    --server-url) SERVER_URL="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

export TEST_BASE_URL="$SERVER_URL"

# ── PostgreSQL (sql-agent-postgres :5432) ────────────────────────────────────
export PG_HOST=127.0.0.1
export PG_PORT=5432
export PG_DB=testdb
export PG_USER=testuser
export PG_PASS=testpass

# ── MySQL (sql-agent-mysql :3306) ─────────────────────────────────────────────
export MY_HOST=127.0.0.1
export MY_PORT=3306
export MY_DB=testdb
export MY_USER=testuser
export MY_PASS=testpass

# ── MariaDB (ql-test-mariadb :3307 if running) ───────────────────────────────
export MARIA_HOST=127.0.0.1
export MARIA_PORT=3307
export MARIA_DB=testdb
export MARIA_USER=testuser
export MARIA_PASS=testpass

echo ""
echo "========================================================"
echo "  QueryLux E2E Test Suite"
echo "  Server : $SERVER_URL"
echo "========================================================"

python3 "$(dirname "$0")/test_all_databases.py"
