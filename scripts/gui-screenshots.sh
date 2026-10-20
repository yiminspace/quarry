#!/usr/bin/env bash
# Dual-theme GUI screenshots into $RE_SNAPSHOT_DIR (PNGs for PR review).
#
# Builds the web bundle from THIS checkout, serves it against a seeded
# throwaway workspace, and drives a headless Chromium through connection →
# table → grid in both themes (scripts/gui_screenshots.py).
#
# Preconditions (exits non-zero with the reason when missing — callers treat
# screenshots as advisory, not a gate):
#   - node/npm on PATH
#   - python Playwright + Chromium installed (pip install -e ".[e2e]")
#   - the quarry_test Postgres from TESTING.md (QUARRY_TEST_DB_URL to override)
set -euo pipefail

cd "$(dirname "$0")/.."

command -v npm >/dev/null || { echo "npm not on PATH"; exit 3; }
python3 -c "import playwright" 2>/dev/null || { echo "python playwright not installed"; exit 3; }
DB_URL="${QUARRY_TEST_DB_URL:-postgresql://localhost:5432/quarry_test}"
psql "$DB_URL" -tAc "SELECT 1" >/dev/null 2>&1 || { echo "test database unreachable: $DB_URL"; exit 3; }

(cd web && npm ci --no-audit --no-fund --silent && npm run build --silent)

exec python3 scripts/gui_screenshots.py
