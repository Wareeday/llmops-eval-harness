.DEFAULT_GOAL := help
PY ?= python
.PHONY: help install run test cov lint format typecheck check eval eval-live validate demo-good demo-leaky docker-build docker-up docker-down constraints clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install runtime + dev dependencies and git hooks
	$(PY) -m pip install -r requirements-dev.txt
	pre-commit install

run:  ## Start the dashboard + API with auto-reload on :7860
	$(PY) -m uvicorn app.main:app --reload --port 7860

test:  ## Run the test suite
	$(PY) -m pytest -q

cov:  ## Tests with coverage (fails under 90%)
	$(PY) -m pytest -q --cov=app --cov-report=term-missing --cov-fail-under=90

lint:  ## Ruff lint + format check
	ruff check .
	ruff format --check .

format:  ## Auto-format and auto-fix
	ruff format .
	ruff check . --fix

typecheck:  ## mypy (strict)
	mypy

check: lint typecheck cov  ## Everything CI runs

validate:  ## Validate the dataset
	$(PY) -m app.cli --validate-only

eval:  ## Self-test quality gate (built-in reference agent)
	$(PY) -m app.cli --no-cache --fail-on-gate --junit reports/junit.xml

eval-live:  ## Quality gate against your agent (set TARGET_*_ENDPOINT_URL)
	$(PY) -m app.cli --target-mode live --fail-on-gate --junit reports/junit.xml

demo-good:  ## Start the example agent in "good" mode on :9000
	AGENT_MODE=good $(PY) -m uvicorn examples.reference_agent:app --port 9000

demo-leaky:  ## Start the example agent in vulnerable "leaky" mode on :9000
	AGENT_MODE=leaky $(PY) -m uvicorn examples.reference_agent:app --port 9000

docker-build:  ## Build the image
	docker build --build-arg GIT_SHA=$$(git rev-parse --short HEAD 2>/dev/null || echo dev) -t llmops-agent-eval-harness:local .

docker-up:  ## Start harness + Redis
	docker compose up --build -d

docker-down:  ## Stop everything
	docker compose --profile demo --profile monitoring down

constraints:  ## Regenerate constraints.txt from a clean venv
	$(PY) -m venv .constraints-venv && . .constraints-venv/bin/activate && pip install -q -r requirements.txt && \
	{ echo "# Exact dependency versions this release was tested with."; echo "# Use: pip install -r requirements.txt -c constraints.txt"; echo "# Regenerate with: make constraints"; pip freeze --exclude-editable | sort -f; } > constraints.txt && rm -rf .constraints-venv

clean:  ## Remove caches and local data
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov data_store reports build *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
