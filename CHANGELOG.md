# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **SP-0 (Foundations)** implemented: event store + schema registry + projections; immutable
  document DAG; declarative state machines; durable runtime (transactional outbox, idempotent
  handlers, durable timers, classified retries, external-command outbox, blob GC); the four
  aggregates + approval→activation saga; identity/authz/SoD + security-audit stream; governance
  attributes; privacy/retention (crypto-shred, replay modes). 478 tests passing.
- Architecture spec, build roadmap, SP-0 design spec, and the banking Domain/Use-Case Catalog
  (closed boundary, open use-case set) under `docs/architecture/`.
- Generation design (§14): LLM-FE multi-candidate + model-free scoring + overfitting guard;
  LLM-SR symbolic synthesis; FeatLLM reason→rules→code, scorecard form, few-shot cold-start;
  MALMAS feature-strategy Router + conceptual memory.
- Project tooling: GitHub Actions CI, ruff, mypy, pre-commit, Makefile, `.editorconfig`,
  `.env.example`, `LICENSE`, `CONTRIBUTING.md`.

### Changed
- Renamed the package `sp0` → `featuregen` (the platform is one product).
- Reorganized docs from `docs/superpowers/{specs,plans}` → `docs/architecture` + `docs/plans`.
