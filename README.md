# FeatureGen

A **contract-driven, banking-grade feature engineering platform**. A data scientist describes a
feature in plain English; the platform turns it into a **point-in-time-correct, policy-compliant,
reviewed, versioned, monitored** production feature — safely.

> **The one rule:** the LLM *suggests and structures*; the platform *validates and enforces*; the
> human *confirms business meaning*; the registry *governs the production lifecycle*. No actor does
> another's job. It is explicitly **not** `free text → LLM → SQL → feature store`.

**Banking-only by design** (see `docs/architecture`, §15.5): the platform builds *any banking
feature* but rejects out-of-banking requests.

## Status

| Component | Status |
|---|---|
| **SP-0 — Foundations** (event store, state machine, durable runtime, identity/governance) | ✅ **Implemented** (Python + PostgreSQL, 478 tests) |
| Architecture, roadmap, SP-0 spec, banking Domain Catalog | ✅ Documented (`docs/architecture/`) |
| SP-1 (Metadata Overlay) → SP-12 | ⏳ Planned (see roadmap) |

## Quick start

```bash
make setup        # uv sync --extra dev + git hooks
make test         # run the suite (uses an ephemeral Postgres, or set FEATUREGEN_TEST_DSN)
make ci           # lint + format-check + typecheck + test
```
Requires Python 3.11+ (via `uv`) and PostgreSQL 15+ binaries on `PATH` (or a server via `FEATUREGEN_TEST_DSN`). See [CONTRIBUTING.md](CONTRIBUTING.md).

## Repository layout

```
src/featuregen/        # the product package (SP-0 foundation; sub-projects add modules here)
  contracts/           # shared types (event/identity/provenance envelopes, commands, …)
  events/              # event store, schema registry, serde
  documents/           # immutable document DAG, draft schema, registry
  state_machine/       # declarative tables, guards, versioning
  runtime/             # outbox, timers, retries, external commands, dispatch
  aggregates/          # request/feature/run/version, lifecycle, activation saga
  authz/  commands/  gates/  identity/  security/   # authz, command API, human gates, audit
  governance/  privacy/  attempt_memory/  projections/
  db/migrations*       # canonical migrations (Python module + .sql files)
  config.py            # env-based settings
tests/                 # pytest suite
docs/architecture/     # reference architecture, roadmap, SP-0 spec, banking Domain Catalog
docs/plans/            # implementation plans (SP-0 phase plans)
```

## Architecture (the short version)

Seven layers: **0** metadata foundation (catalog + overlay + Domain Catalog) · **1** intake ·
**2** contract control (Human Gate #1) · **3** grounding (policy-aware, point-in-time) ·
**4** validation + implementation routing · **5** compilation + sandbox · **6** evaluation
(model-free scoring, leakage/fairness/overfitting) · **7** approval (Human Gate #2) + registry +
lifecycle.

Read the full design and decisions:
- **[Reference architecture & end-to-end flow →](docs/architecture/2026-06-29-reference-architecture.md)** — start here: diagrams, the agentic model, the complete flow.
- **[Full design spec →](docs/architecture/2026-06-27-feature-engineering-platform-design.md)** — detailed design (16 sections).
- **[Build roadmap →](docs/architecture/2026-06-27-feature-engineering-platform-roadmap.md)**
- **[SP-0 Foundations spec →](docs/architecture/2026-06-27-sp0-foundations-design.md)**
- **[SP-1 Metadata Overlay spec →](docs/architecture/2026-06-29-sp1-metadata-overlay-design.md)**
- **[Banking Domain Catalog →](docs/architecture/2026-06-29-banking-domain-catalog.md)**

## Roadmap

| Phase | Focus | Sub-projects |
|---|---|---|
| **A — Foundations** | Contract/state/runtime backbone; Metadata Overlay | SP-0 ✅, SP-1 |
| **B — Vertical slice** | One feature type, end-to-end | SP-2 … SP-5 |
| **C — Coverage** | LLM-SQL path, full validation, generation engine | SP-6, SP-7, SP-8, SP-12 |
| **D — Hardening** | Governance, lifecycle/monitoring, security | SP-9, SP-10, SP-11 |

## HTTP API

`make api` serves the FastAPI layer on `http://localhost:8000` (OpenAPI docs at `/docs`).

Environment:
- `FEATUREGEN_DSN` (required) — PostgreSQL 15+ DSN; run `uv run python -m featuregen migrate` first.
- `FEATUREGEN_AUDIT_HMAC_KEY` — required by security-audit paths elsewhere in the platform.
- `FEATUREGEN_LLM_PROVIDER=anthropic` (optional) — wires the config-gated Claude adapter seam.
  **Not yet operational**: the real-provider enrichment/assist plumb-through (SDK dependency,
  model + output-schema wiring) is a tracked follow-on; leave unset for now. Unset: uploads ingest
  un-enriched and `/features/recommend|recipe|leakage-check` return **503** (no fake AI — D5).

Auth is a development stub: send `X-User: <subject>` and `X-Roles: pii_reader,restricted_reader,…`.
Roles gate read-scope (PII-tagged columns are hidden without `pii_reader`). The stub is the
single seam (`featuregen.api.deps.get_identity`) to replace with real session auth. The stub mints
an unauthenticated dev principal (`user:<subject>`) through the fail-closed identity builder;
authenticated envelopes only ever come from a registered verifier once the real IdP lands.

## Frontend

`make frontend-dev` serves the React app on `http://localhost:5173`, proxying API calls to the
FastAPI server (`make api`). Dev loop: `make api` in one terminal, `make frontend-dev` in another,
then upload `docs/examples/deposits.csv` as source `deposits` and search for “balance”.
The session bar (top right) is stub auth — toggle `pii_reader` to see read-scope filtering live.
Feature-assist panels show a “not configured” notice unless the API deployment sets
`FEATUREGEN_LLM_PROVIDER=anthropic`.

## License
Proprietary — see [LICENSE](LICENSE).
