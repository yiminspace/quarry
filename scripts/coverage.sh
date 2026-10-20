#!/usr/bin/env bash
# Coverage gate — unit + integration must cover >= THRESHOLD% of the package.
# The e2e/browser layers drive subprocesses / a real browser, so their coverage
# is not counted here on purpose: the gate proves the in-process suites alone
# exercise the code paths.
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$(pwd)/src"

THRESHOLD="${COV_THRESHOLD:-95}"

python3 -m pytest -m "unit or integration" \
  --cov=quarry --cov-report=term-missing --cov-report=html:htmlcov \
  --cov-fail-under="$THRESHOLD" "$@"
rc=$?

echo
if [[ $rc -eq 0 ]]; then
  echo "✓ coverage >= ${THRESHOLD}%  (HTML report: htmlcov/index.html)"
else
  echo "✗ coverage below ${THRESHOLD}% — see the 'Missing' column above (HTML: htmlcov/index.html)"
fi
exit $rc
