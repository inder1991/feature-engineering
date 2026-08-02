.PHONY: help setup lint format format-check typecheck test l0-gate ci api frontend-dev frontend-test clean

help:  ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
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

l0-gate:  ## Build-verify the golden Kedro project under BOTH supported kedro lines
	# hdfs + s3fs are GATE-environment dependencies (like Temurin 17), not artifact pins:
	# kedro-datasets 4.x's spark module hard-imports both at catalog construction, the lock
	# cannot carry them (DEFERRED-WORK A.32 — the [spark] extra conflicts with pyspark 3.5),
	# and without them the run-parameters hook proof would die before the hook fires.
	test -x .venv-artifact/bin/python || (uv venv .venv-artifact --python 3.11 --seed && \
		.venv-artifact/bin/python -m pip install --quiet \
			-r tests/featuregen/materialize/goldens/cif_daily/requirements.lock \
			hdfs s3fs)
	# hdfs again: kedro-datasets 9.5.0's spark_dataset.py hard-imports it, but the 9.x [spark]
	# extra (spark-local + spark-s3) does not install it — an upstream packaging gap (A.32).
	test -x .venv-l0-modern/bin/python || (uv venv .venv-l0-modern --python 3.11 --seed && \
		.venv-l0-modern/bin/python -m pip install --quiet \
			"kedro==1.5.0" "kedro-datasets[spark]==9.5.0" "pyspark==4.2.0" hdfs)
	FEATUREGEN_L0_PYTHON=$(CURDIR)/.venv-artifact/bin/python \
	PYSPARK_PYTHON=$(CURDIR)/.venv-artifact/bin/python \
	PYSPARK_DRIVER_PYTHON=$(CURDIR)/.venv-artifact/bin/python \
		uv run pytest tests/featuregen/materialize/l0_gate.py -q
	FEATUREGEN_L0_PYTHON=$(CURDIR)/.venv-artifact/bin/python \
	PYSPARK_PYTHON=$(CURDIR)/.venv-artifact/bin/python \
	PYSPARK_DRIVER_PYTHON=$(CURDIR)/.venv-artifact/bin/python \
		uv run pytest tests/featuregen/materialize/spark_semantics_gate.py -q
	FEATUREGEN_L0_PYTHON=$(CURDIR)/.venv-l0-modern/bin/python \
	PYSPARK_PYTHON=$(CURDIR)/.venv-l0-modern/bin/python \
	PYSPARK_DRIVER_PYTHON=$(CURDIR)/.venv-l0-modern/bin/python \
		uv run pytest tests/featuregen/materialize/l0_gate.py -q
	FEATUREGEN_L0_PYTHON=$(CURDIR)/.venv-l0-modern/bin/python \
	PYSPARK_PYTHON=$(CURDIR)/.venv-l0-modern/bin/python \
	PYSPARK_DRIVER_PYTHON=$(CURDIR)/.venv-l0-modern/bin/python \
		uv run pytest tests/featuregen/materialize/spark_semantics_gate.py -q

ci: lint format-check typecheck test  ## The fast CI jobs (l0-gate is separate: slow, needs JVM + artifact venvs)

api:  ## Serve the HTTP API on :8000 (needs FEATUREGEN_DSN)
	uv run uvicorn --factory featuregen.api.app:create_app_from_env --reload --port 8000

frontend-dev:  ## Vite dev server on :5173 (proxies API calls to :8000)
	cd frontend && npm run dev

frontend-test:  ## Frontend unit tests (vitest)
	cd frontend && npm test

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache src/*.egg-info build dist
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
