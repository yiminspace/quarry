#!/usr/bin/env bash
# Layered test runner — runs the suite in three passes (unit → integration → e2e)
# and prints a clear, at-a-glance summary. Any pass failing fails the whole run.
#
# Usage:
#   scripts/test.sh                # all layers
#   scripts/test.sh unit           # one layer (unit | integration | e2e)
#   scripts/test.sh -- -k safety   # pass extra args through to pytest (after --)
set -uo pipefail

cd "$(dirname "$0")/.."

# make `import quarry` work whether or not the package is pip-installed
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$(pwd)/src"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
hr()   { printf '%s\n' "------------------------------------------------------------"; }

LAYERS=(unit integration e2e)
PYTEST_EXTRA=()
if [[ "${1:-}" =~ ^(unit|integration|e2e|browser)$ ]]; then
  LAYERS=("$1"); shift
fi
if [[ "${1:-}" == "--" ]]; then shift; PYTEST_EXTRA=("$@"); fi

RESULT=()
overall=0
for layer in "${LAYERS[@]}"; do
  hr; bold "▶ ${layer} tests"; hr
  if python3 -m pytest -m "$layer" "${PYTEST_EXTRA[@]}"; then
    RESULT+=("PASS")
  else
    rc=$?
    # pytest exit 5 = "no tests collected" for this marker — treat as skip, not fail
    if [[ $rc -eq 5 ]]; then RESULT+=("none"); else RESULT+=("FAIL"); overall=1; fi
  fi
  echo
done

hr; bold "Summary"; hr
for ((i=0; i<${#LAYERS[@]}; i++)); do
  printf '  %-12s %s\n' "${LAYERS[$i]}" "${RESULT[$i]}"
done
hr
if [[ $overall -eq 0 ]]; then bold "✓ all layers green"; else bold "✗ failures above"; fi
exit $overall
