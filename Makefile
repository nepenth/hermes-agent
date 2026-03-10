.DEFAULT_GOAL := help
SHELL := /bin/bash
VENV := .venv
UV := uv

SRC := run_agent.py model_tools.py toolsets.py cli.py hermes_state.py batch_runner.py \
       tools/ hermes_cli/ gateway/ agent/ cron/

# ─── Setup ──────────────────────────────────────────────────────────────────────

.PHONY: setup sync clean

setup: ## Full dev setup (venv + deps + pre-commit)
	$(UV) venv $(VENV) --python 3.11
	. $(VENV)/bin/activate && $(UV) pip install -e ".[all,dev]"
	. $(VENV)/bin/activate && $(UV) pip install -e "./mini-swe-agent"
	. $(VENV)/bin/activate && pre-commit install
	@echo "\n✅ Setup complete. Run: source $(VENV)/bin/activate"

sync: ## Reinstall deps into existing venv
	. $(VENV)/bin/activate && $(UV) pip install -e ".[all,dev]"

clean: ## Remove build artifacts and caches
	rm -rf .ruff_cache .mypy_cache .pytest_cache dist build *.egg-info
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +

# ─── Quality ────────────────────────────────────────────────────────────────────

.PHONY: lint fmt check

lint: ## Check lint + formatting (no changes)
	. $(VENV)/bin/activate && ruff check $(SRC)
	. $(VENV)/bin/activate && ruff format --check $(SRC)

fmt: ## Auto-fix lint + format
	. $(VENV)/bin/activate && ruff format $(SRC)
	. $(VENV)/bin/activate && ruff check --fix $(SRC)

check: lint test ## Lint + test (mirrors CI)

# ─── Test ───────────────────────────────────────────────────────────────────────

.PHONY: test test-fast test-watch

test: ## Run full test suite
	. $(VENV)/bin/activate && python -m pytest tests/ -q --ignore=tests/integration --tb=short

test-fast: ## Run tests with fail-fast
	. $(VENV)/bin/activate && python -m pytest tests/ -q --ignore=tests/integration --tb=short -x

test-watch: ## Rerun tests on file changes
	. $(VENV)/bin/activate && python -m watchfiles "python -m pytest tests/ -q --ignore=tests/integration --tb=short -x" $(SRC) tests/

# ─── Dev Servers ────────────────────────────────────────────────────────────────

.PHONY: dev-cli dev-gateway

dev-cli: ## Auto-restart CLI on file changes
	. $(VENV)/bin/activate && python -m watchfiles "python -m hermes_cli.main" $(SRC)

dev-gateway: ## Auto-restart gateway on file changes
	. $(VENV)/bin/activate && python -m watchfiles "python -m gateway.run" $(SRC)

# ─── Misc ───────────────────────────────────────────────────────────────────────

.PHONY: help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
