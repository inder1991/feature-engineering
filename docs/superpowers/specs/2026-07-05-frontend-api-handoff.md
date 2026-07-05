# Frontend + API — Handoff Spec

Date: 2026-07-05. Purpose: make a **separate session** turnkey for building the API layer + frontend on
top of the finished, fixed backend. Open this, pick a stack, and start — the backend surface, the
endpoint contracts, the screens, and the honest guardrails are all here.

## Current state (what you're building on)
- The backend is **pure Python functions that take a DB `conn`** — there is **no HTTP layer** and **no
  frontend**. Everything lives under `src/featuregen/overlay/upload/` (+ the feature layer). Full suite
  1227 green; the deep-dive review's BLOCKER + 9 MAJORs are fixed (see
  `2026-07-05-upload-catalog-review-findings.md`).
- One **UI mockup** exists as the visual anchor for the review-queue screen (the `quarantine-review`
  Artifact — standalone HTML, fake data; a design reference, not wired to anything).
- **Enrichment now works against a real provider** (via `enrich_llm.audited_enrich_call` — attached
  output-schema + reserved input keys + egress guard + audit record). It runs when a real `LLMClient` is
  configured and passed in; with `client=None` it's simply skipped (concept/domain/definition absent).
  So the UI can surface enrichment as real, but must still design for its **absence** when no provider is
  configured, and treat every LLM output as **advisory** (suggestion, not fact).

## Two layers to build
1. **API layer** (recommend **FastAPI** — same language, Pydantic contracts map 1:1 to the dataclasses,
   async). Each endpoint opens a **request-scoped transaction** and calls the backend function. Ingest is
   all-or-nothing (one tx); reads are read-only.
2. **Frontend** (React or your preference). Four screens (below).

## Backend surface → endpoints

Signatures are the source of truth; wrap each in an endpoint. `conn` and `now` are injected by the API
(request tx + server clock); **`roles` come from the authenticated session, never the client** (this is
the read-scope contract — M6).

| Endpoint | Backend call | Notes |
|----------|--------------|-------|
| `POST /uploads` (multipart: file, source) | `read_csv_rows`/`read_excel_rows` → `ingest_upload(conn, source, rows, actor, now, client=None)` | Returns `IngestResult{status, reason, asserted, staled, quarantined, flagged}`. `status ∈ ingested\|held\|rejected`. Pass `client=None` in prod (no enrichment) until the real-provider follow-on. |
| `GET /search?q=&domain=&limit=` | `search(conn, q, now, roles=session.roles, limit)` | Returns `[SearchHit]` (object_ref, table, column, type, definition, is_grain, is_as_of, concept, domain, sensitivity, additivity, unit, currency, entity, score). Freshness + read-scope already applied server-side. |
| `GET /sources/{source}/quarantine` | `list_quarantine(conn, source)` | `[QuarantineItem{row_index, raw, reason}]` — powers the review-queue screen. |
| `GET /columns/{object_ref}/joins` | `column_joins(conn, source, object_ref)` | `[JoinEdge{from_ref, to_ref, cardinality, resolved}]` (resolved=false ⇒ pending/cross-source). |
| `GET /join-path?source=&from=&to=` | `find_join_path(conn, source, from, to)` | `[JoinStep]` oriented to traversal (cardinality reads src→dst) or `null` if unreachable. |
| `POST /features` | `register_feature(conn, FeatureSpec)` | Body = FeatureSpec; returns `feature_id`. **Only on explicit user confirm.** |
| `GET /features/{id}/freshness` | `feature_freshness(conn, id, now)` | `{fresh, stale_sources}` — stalest-source lineage. |
| `GET /columns/{object_ref}/feature-impact` | `features_affected_by(conn, source, object_ref)` | `[feature_id]` — "what breaks if this column drifts." |
| `POST /features/recommend` | `recommend_features(conn, objective, client, catalog_source, roles=session.roles)` | **Suggestions only** — render as proposals, register on confirm. FakeLLM-limited today. |
| `POST /features/recipe` | `feature_recipe(conn, query, client, catalog_source, roles=session.roles)` | NL→recipe: LLM intent + **deterministic** join path. Suggestions only. |
| `POST /features/leakage-check` | `leakage_check(conn, derives_from, target_ref, client)` | `[LeakageWarning]` — advisory. |

## The four screens
1. **Upload** — a file drop (CSV/Excel) + a source name (or auto-resolve later). On submit, show the
   `IngestResult`: *"142 ingested · 3 quarantined · held?"*. Surface **`held`** (brake — "confirm large
   change"), **`rejected`** (structural), and **`flagged`** (first-upload — review recommended) as
   distinct states.
2. **Search** — the headline screen. A query box → ranked results, each card carrying the context from
   `SearchHit`: grain / as-of / concept / domain / joinable / **additivity + unit** (so a builder
   aggregates correctly) / a **freshness** indicator. PII columns are already filtered by the session's
   roles — the UI shows "restricted, requires role" only if you choose to hint at hidden results.
3. **Review queue** — build against the **`quarantine-review` mockup**. `GET …/quarantine` → cards with
   the raw row + reason; the "fix inline → revalidate → re-upload" and "systematic rule" flows are
   designed there. (The revalidate action re-ingests; the backend clears quarantine on a clean re-upload.)
4. **Feature workbench** — objective box → `recommend_features` proposals; NL box → `feature_recipe`
   (show the **real join path + cardinality** it found); a **leakage** warning banner; a **confirm** that
   calls `POST /features`. Everything here is a suggestion until the user confirms.

## Cross-cutting concerns (get these right)
- **Auth & read-scope (M6):** the session must resolve the user's **roles** and pass them to `search`,
  `recommend_features`, `feature_recipe`. Never accept roles from the client. This is what keeps the PII
  map from leaking — it's enforced in the backend, but only if the API supplies real roles.
- **Freshness / fail-closed:** `resolve_fact` and `search` fail closed on stale sources — a served fact
  may be *absent* (not an error). The UI shows "not currently vouched — re-upload `<source>`," never a
  500. Surface staleness, don't hide it.
- **Advisory vs load-bearing editability:** a user may freely edit *advisory* fields (definition,
  concept, domain) — a wrong value only worsens search. *Load-bearing* fields (grain, joins, sensitivity,
  additivity) drive correctness — edits to these go through **confirm** (and, for facts, re-ingest), never
  silent auto-apply. Reflect this in which fields are directly editable vs which open a confirm dialog.
- **LLM = suggestion-then-confirm:** recommend/recipe/leakage return **proposals**. The UI presents them
  as such (with confidence where available) and only mutates state on explicit user action. A wrong
  suggestion must never become a registered feature without a click.
- **Enrichment:** the upload endpoint enriches when passed a configured `LLMClient` (governed + audited),
  or skips enrichment with `client=None`. Concept/domain/definition may be absent (no provider) — design
  for that gracefully; and every LLM output stays advisory (correctable, never a load-bearing fact).
- **Transactions:** ingest is one tx (all-or-nothing — a bad upload rolls back cleanly). Reads are
  read-only. Give each request its own connection/tx (a FastAPI dependency).

## Guardrails the review baked in (don't regress them)
- Search/feature endpoints **must** apply `roles` (M6). A missing role → PII hidden, not shown.
- Multi-source files are quarantined per-row, not crashed (M5) — surface the quarantine, don't retry.
- Held/rejected/flagged/quarantined are first-class response states, not errors.
- The enrichment path is not production-LLM-ready — the API should not advertise it as such.

## Suggested build sequence (frontend session)
1. FastAPI skeleton + a request-tx dependency + a stub auth (roles from a header/session), no real IdP yet.
2. `POST /uploads` + `GET /search` (+ read-scope roles) → the Upload and Search screens (the core loop).
3. `GET …/quarantine` → the Review queue (against the mockup).
4. The feature endpoints → the Feature workbench (suggestion-then-confirm).
5. Real auth/IdP + the enrichment real-provider follow-on (separate, larger).
