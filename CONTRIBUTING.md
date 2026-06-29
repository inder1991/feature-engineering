# Contributing

## Prerequisites
- **Python 3.11+** (managed via [`uv`](https://github.com/astral-sh/uv))
- **PostgreSQL 15+** binaries on `PATH` (tests spin up an ephemeral cluster), *or* a reachable server via `FEATUREGEN_TEST_DSN`

## Setup
```bash
make setup        # uv sync --extra dev + install pre-commit hooks
cp .env.example .env
```

## Day-to-day
```bash
make test         # run the suite (pytest; ephemeral Postgres unless FEATUREGEN_TEST_DSN is set)
make lint         # ruff check
make format       # ruff format
make typecheck    # mypy (gradual; informational in CI for now)
make ci           # lint + format-check + typecheck + test (what CI runs)
```

## Code standards
- **Style/lint:** `ruff` (config in `pyproject.toml`); enforced by pre-commit and CI.
- **Types:** `mypy`; we are adopting typing gradually — keep new code typed.
- **Tests:** TDD. Every change ships with tests; the suite must stay green.
- **Commits:** conventional prefixes (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`).

## Project shape
This is **one product** (`featuregen`), built sub-project by sub-project. Each sub-project
(SP-0 foundation, SP-1 overlay, …) adds **modules** under `src/featuregen/`, never a new
top-level package. Each goes through `brainstorm (spec) → plan → implementation`:
- specs/architecture live in [`docs/architecture/`](docs/architecture/)
- implementation plans live in [`docs/plans/`](docs/plans/)

## Layout
- `src/featuregen/` — the package (see [README](README.md#repository-layout))
- `tests/` — pytest suite (mirrors the package areas)
- `src/featuregen/db/migrations*` — canonical DB migrations (Python module + `.sql` files)
