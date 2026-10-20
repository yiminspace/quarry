.PHONY: test test-unit test-integration test-e2e test-browser cov seed install browser-install

# Full layered run with an at-a-glance per-layer summary.
test:
	@bash scripts/test.sh

test-unit:
	@bash scripts/test.sh unit

test-integration:
	@bash scripts/test.sh integration

test-e2e:
	@bash scripts/test.sh e2e

# Browser-driven GUI frontend tests (needs playwright + chromium + a DB).
test-browser:
	@bash scripts/test.sh browser

# Coverage gate: unit + integration must stay >= 95%.
cov:
	@bash scripts/coverage.sh

# One-time: download the headless browser for the e2e/browser suite.
browser-install:
	python -m playwright install chromium

# One-time local setup: create + seed the test database.
seed:
	createdb quarry_test 2>/dev/null || true
	psql "$${QUARRY_TEST_DB_URL:-postgresql://localhost:5432/quarry_test}" -f tests/seed.sql

# Re-run this after removing the git worktree/checkout you last installed
# from — an editable install points at that exact path (see CONTRIBUTING.md
# "Reinstalling after a worktree is removed").
install:
	pip install -e ".[dev]"
