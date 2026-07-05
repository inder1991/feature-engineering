.PHONY: help setup lint format format-check typecheck test ci api clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup:  ## Install deps (dev) and git hooks
	uv sync --extra dev
	uv run pre-commit install

lint:  ## Ruff lint
	uv run ruff check .

format:  ## Ruff auto-format
	uv run ruff format .

format-check:  ## Ruff format check (CI)
	uv run ruff format --check .

typecheck:  ## Mypy type check
	uv run mypy

test:  ## Run the test suite (ephemeral Postgres, or set FEATUREGEN_TEST_DSN)
	uv run pytest -q

ci: lint format-check typecheck test  ## Everything CI runs

api:  ## Serve the HTTP API on :8000 (needs FEATUREGEN_DSN)
	uv run uvicorn --factory featuregen.api.app:create_app_from_env --reload --port 8000

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache src/*.egg-info
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
