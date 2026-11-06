.PHONY: test test-unit test-integration test-e2e test-browser cov seed install browser-install preview-hosted

# Manual hosted GUI preview with disposable demo data; no Console login needed.
preview-hosted:
	cd web && npm run build
	python3 scripts/preview_hosted.py $(PREVIEW_ARGS)

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

# Install dependencies for the Python test baseline and browser harness.
install:
	python -m pip install pytest pytest-cov playwright "pymysql[rsa]"
