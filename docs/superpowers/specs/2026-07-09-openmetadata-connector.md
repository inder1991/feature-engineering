# OpenMetadata Connector — Design Spec

Date: 2026-07-09 (restructured to two tiers 2026-07-10). Status: BUILT (two-tier).
Purpose: bring tables and columns into the FeatureGen catalog directly from an OpenMetadata
instance, without file uploads — while preserving every guarantee the upload path provides.

## Two tiers, grounded in OpenMetadata's own model

OpenMetadata's hierarchy is `DatabaseService -> Database -> Schema -> Table -> Column`, with a
fully-qualified name `service.database.schema.table` (e.g.
`postgres_prod.ecommerce.public.customers`). A bot JWT token authenticates to the WHOLE INSTANCE:
it sees EVERY `DatabaseService` (high privilege, no per-service scoping). Services are listed via
`GET /api/v1/services/databaseServices` (name, `serviceType` e.g. Snowflake/Mysql/BigQuery,
`fullyQualifiedName`); tables are filtered via `GET /api/v1/tables?...&database=<fqn>` or by
service.

The connection therefore splits into two tiers, and the per-source binding is the child:

- **INTEGRATION** = one OpenMetadata instance: `name`, `base_url`, `token_env` (a sealed env-var
  reference), and a default `tag_map`. Generic; sees all services; RBAC-managed. One row per
  instance. Rotate the token in one place.
- **SYNC** (child of an integration) = one `DatabaseService` (optionally narrowed by
  database/schema) mapped to one FeatureGen catalog source, with a `tag_map` override and a table
  naming choice. Many per integration — add as many source syncs as you have services.

Ingest pulls from a **SYNC** (by `sync_id`), never a flat connector. The v1 flat connector (which
bundled url + token + scope + one target source in a single `connector_config` row) is retired in
favor of this split: the connection (url + token) is configured once per instance, and each
catalog source is a sync under it. Discovery (`GET /integrations/{id}/services`) is a convenience
layered on top; it never gates sync creation (a `service_name` can be typed by hand), so an OM
outage degrades gracefully.

## Principle: a new mouth, same stomach

`ingest_upload(conn, source, rows, actor, now, client)` takes **rows**, not files. The CSV and
Excel readers are translators into `CanonicalRow`; the connector is a third translator:

```
OpenMetadata REST API ──> read_openmetadata(...) ──> list[CanonicalRow] ──> ingest_upload(...)
```

Validation, the large-change brake, quarantine, fact assertion, drift watermarks, and graph
build all run unchanged. The connector adds NO new write path into the catalog.

## Non-goals (v1)

- No live federation: FeatureGen never queries OM at search/serve time. Freshness stays vouched
  by ingest events only.
- No data movement: metadata only, like every other FeatureGen path.
- No OM write-back (pushing our features/lineage into OM) — a named later phase.
- No automatic semantics: the connector never invents as-of basis, additivity, unit, or currency.

## What OpenMetadata provides, and the mapping

Source API: `GET /api/v1/tables?fields=columns,tags,tableConstraints&limit=…&after=…`
(cursor-paginated), scoped by service/database/schema filters. Auth: `Authorization: Bearer
<bot JWT>`.

| OpenMetadata | CanonicalRow | Notes |
|---|---|---|
| service or database FQN part | `source` | one FeatureGen catalog source per import scope; explicit in config |
| table name | `table` | schema prefix folded per config (`schema_table` or `table`) |
| column name | `column` | verbatim |
| column dataType | `type` | lowercased OM type token (`BIGINT`→`bigint`); non-empty is all our validator requires; string-equality drives type-conflict detection as today |
| column/table description | `definition` | advisory field, imported verbatim |
| PII/classification tags | `sensitivity` | via an explicit, editable **tag map** (e.g. `PII.Sensitive`→`pii`); an UNMAPPED tag produces the literal tag string, which fails our sensitivity whitelist and lands in **quarantine** — imports cannot silently weaken read-scope |
| tableConstraints PRIMARY_KEY | `is_grain` | on the constraint's column(s) |
| tableConstraints FOREIGN_KEY | `joins_to` (+`cardinality=null`) | target as `table.column`; unknown cardinality stays null (UI already renders "cardinality unknown" honestly) |
| partition / time-column hints | **not mapped** to `as_of` | recorded as a *suggestion* for the review queue; a human confirms as-of + basis |
| — | `additivity`, `unit`, `currency`, `entity` | OM has no equivalents; imported blank → "semantics pending" |

## Structure vouched, semantics pending

An OM import lands tables/columns live and searchable (structure), but the safety facts the
gauntlet depends on (as-of basis, additivity, unit/currency, entity) arrive blank. These are
flagged **semantics pending** and routed to the review queue as confirmation work for the data
owner. Feature generation over semantics-pending columns degrades exactly as it does today for
blank facts (checks that need a fact skip it; nothing pretends). The import summary states the
count plainly: "N columns imported; M need owner confirmation before they carry safety facts."

## Trust model

- **Identity (as-built deviation).** The connector has no mintable `service:openmetadata-connector`
  envelope: an authenticated service envelope is only mintable via the sealed trust capability,
  whose call sites are frozen by a grep-guard test, so the API layer cannot mint one. Imports
  therefore ingest under the **approving human's** session identity (the sanctioned path every
  upload uses), and the import record names the connector as the **vehicle**
  (`vehicle='openmetadata-connector'`).
- **The brake still gates**: a sync that would remove >30% of a source's objects (or overlap
  <60%) is held exactly like a hostile upload; a human resolves it.
- **Freshness = sync recency**: each successful import advances the source's drift watermark, and
  the sync's `last_import_at` is stamped. A source whose sync stops importing goes honestly stale
  in ≤24h, catalog-wide.
- **RBAC** (permissions, never role strings): creating/patching/deleting integrations and syncs
  and confirming imports require `catalog:write` (data_owner / platform_admin); listing, getting,
  service discovery, and preview require `catalog:read` (catalog_viewer and up). Denials are
  audited by `require_permission`.

## Config and secrets

Three tables. `integration` (tier 1): `integration_id` (`intg_<ulid>`), `name`, `base_url`,
`token_env`, default `tag_map`, `created_by`, `created_at`. `integration_sync` (tier 2, FK to
integration `ON DELETE CASCADE`): `sync_id` (`sync_<ulid>`), `integration_id`, `service_name`,
`database_filter`, `schema_filter`, `target_source`, `tag_map_override`, `table_naming`,
`created_by`, `created_at`, `last_import_at`. `integration_import` (audit trail, plain-text ids —
**no FK**, so the history outlives a deleted sync/integration): `import_id` (`omimp_<ulid>`),
`sync_id`, `integration_id`, `target_source`, `snapshot_hash`, `approved_by`,
`vehicle='openmetadata-connector'`, `result`.

The **effective tag map** for a pull is `integration.tag_map` merged with `sync.tag_map_override`
(the override wins per tag; a NULL override inherits the integration map wholesale). One sync per
`(integration, service_name)` — the default binding; a duplicate is a 409.

The bot token is **never stored in plaintext**. `featuregen.privacy.kms` exposes only a
destroy/rotate `KeyManager` Protocol (no envelope seal/unseal API to reuse), so the token is an
ENVIRONMENT REFERENCE (`token_env`, e.g. `FEATUREGEN_OM_TOKEN__CORP`); rows hold only the
reference, the request models REJECT a plaintext token field (`extra='forbid'` → 422), and no
response ever carries the token value — only `token_present` (whether the referenced env var is
set on the server).

**Egress + token namespace (fail-closed, security).** Two guards keep a merely-`catalog:write`
user from turning the connector into a secret-exfiltration or SSRF primitive:

- `token_env` is constrained to the connector-token namespace (`^FEATUREGEN_OM_TOKEN__[A-Z0-9_]+$`);
  anything else is rejected 400. An integration row can therefore only ever reference a bot-token
  env var, never an arbitrary process secret (a DSN, a cloud/KMS key), so nothing else can egress
  as a Bearer header. `token_present` then reveals only whether a connector-token var is set.
  Enforced on integration CREATE **and PATCH** (each provided field is merged over the current row
  and the whole result re-validated, so a patch can never leave a row off-namespace).
- `base_url` must resolve to a host on an ops-controlled allowlist, `FEATUREGEN_OM_ALLOWED_HOSTS`
  (comma-separated `host` / `host:port` entries; a bare host matches only the scheme's default
  port). Ops names the legitimate internal OM hosts, so private-IP targets are fine **when
  allowlisted** and SSRF-by-config is dead for everyone below ops. Enforced on integration
  CREATE/PATCH **and on every live OM call** (service discovery, preview, import) — a row that
  predates the allowlist still cannot pull off it. When the env is unset/empty, every check fails
  400 with `no OpenMetadata hosts are allowlisted: set FEATUREGEN_OM_ALLOWED_HOSTS`. The HTTP
  transport does not follow redirects, so a 3xx to an off-allowlist host cannot slip the guard.

## API surface (two-tier)

Integration shape (no token value, ever): `{integration_id:"intg_<ulid>", name, base_url,
token_env, tag_map, created_by, created_at, token_present:bool}`. Sync shape: `{sync_id:"sync_<ulid>",
integration_id, service_name, database_filter|null, schema_filter|null, target_source,
tag_map_override|null, table_naming, created_by, created_at, last_import_at|null}`.

**Integrations (tier 1)** — `catalog:read` to list/get, `catalog:write` to mutate.

| Endpoint | Behavior |
|---|---|
| `GET /integrations`, `GET /integrations/{id}` | list / get integrations (integration shape). |
| `POST /integrations` | body (`extra='forbid'`): `{name, base_url, tag_map={}, token_env?}`. 409 dup name; 400 bad name / base_url / token_env-namespace / tag_map / off-allowlist / no-allowlist; 422 plaintext token field. |
| `PATCH /integrations/{id}` | each field optional, merged over current then whole result re-validated. 409 name collision; 400 re-validation failures. |
| `DELETE /integrations/{id}` | `{deleted:true}` (cascades syncs; import history survives); 404. |

**Discovery** — `catalog:read`.

| Endpoint | Behavior |
|---|---|
| `GET /integrations/{id}/services` | live `GET /api/v1/services/databaseServices` with the sealed token → `[{service_name, service_type, fqn, synced:bool, sync_id|null}]`. 400 off/no-allowlist or missing token (names the env var), 401 OM auth, 502 OM unreachable, 404 unknown integration. A convenience: the sync-create path never depends on it. |

**Syncs (tier 2)** — `catalog:read` to list/get, `catalog:write` to mutate.

| Endpoint | Behavior |
|---|---|
| `GET /integrations/{id}/syncs`, `GET /integrations/{id}/syncs/{sid}` | list / get syncs (sync shape). |
| `POST /integrations/{id}/syncs` | body (`extra='forbid'`): `{service_name, target_source, database_filter=null, schema_filter=null, tag_map_override=null, table_naming="table"}`. Does NOT contact OM. 409 one-per-(integration,service); 400 empty service_name/target_source or bad tag_map_override; 404 unknown integration. |
| `PATCH /integrations/{id}/syncs/{sid}` | all-optional merge + re-validate; 409 service collision. |
| `DELETE /integrations/{id}/syncs/{sid}` | `{deleted:true}`. |

**Preview / import (by `sync_id`).**

| Endpoint | Behavior |
|---|---|
| `POST /syncs/{sid}/preview` (`catalog:read`, no body) | pulls + translates the sync's scope with the effective tag map WITHOUT ingesting; returns the dry-run: `{summary:{tables,columns,new,changed,unchanged,removed,would_quarantine,semantics_pending}, tag_map:[{om_tag,mapped_to,unmapped:bool,count}], tables:[{table,status:new\|changed\|unchanged\|removed,columns,quarantine:[{column,reason}],changes:[]}], brake:{would_hold:bool,reason\|null}, as_of_suggestions:[{table,column,hint}], snapshot_hash}` (`removed` = a table in the current catalog the pull no longer includes; import DELETE-then-rebuilds the source, so it is surfaced, never silently dropped). 404 unknown sync; 400 no/off-allowlist or missing token; 401/502 OM. |
| `POST /syncs/{sid}/import` (`catalog:write`) | body: `{snapshot_hash}` (stale-preview protection: re-pull, re-translate, and if OM moved since preview → 409 with re-preview guidance, nothing ingested). Runs the translation into `ingest_upload` in ONE transaction. Returns `{result:{...IngestResult, status, quarantined}, import_id:"omimp_<ulid>", review_queue:{quarantined, semantics_pending}}`; records `integration_import` and stamps the sync's `last_import_at`. |

Preview-then-confirm is mandatory: there is no direct-import path. Suggestion is never
ingestion.

## Failure modes

- OM unreachable / auth rejected → clean 502/401 surface on preview; nothing touched.
- Pagination interrupted mid-pull → preview fails whole; import never sees partial pulls.
- Import is all-or-nothing per source (same transaction discipline as uploads).
- OM entity shapes drift (new dataType tokens, tag taxonomies) → unknown values follow the
  quarantine path, never a crash; connector version pins the OM API version it understands.

## Phases

1. **v1 (this spec)**: manual preview → confirm import per configured connection.
2. **v2**: scheduled re-sync per source (drift + brake + watermark do the rest); sync history.
3. **v3**: OM webhook/events for incremental updates; optional write-back of FeatureGen
   features + lineage into OM so they appear in enterprise-wide lineage.

## Testing strategy

Recorded OM API fixtures (JSON pages) checked into tests — no network in CI. Contract tests:
translation table above (each row), tag-map fallback→quarantine and the integration/override
merge, PK/FK mapping, pagination assembly, preview/import snapshot-hash mismatch 409, brake hold
on a shrunken pull, RBAC denials, integration+sync CRUD (409 dup name / one-per-service,
PATCH-then-re-validate), discovery `synced` flags + the 400/401/502 surface, fail-closed egress on
CREATE/PATCH and every live call, and secrets never serialized in any response.

The frontend pins the wire byte-for-byte (URLs, request bodies, `token` never present) with a
mocked `fetch`, and drives the screens with a mocked api module: the Integrations screen (list,
add integration, discovery render with synced flags, add sync, OM-down retry fallback, token never
in the DOM) and the Ingest sync picker (grouped options, preview by `sync_id`, empty state, the
approve/stale/remap flow).

## UI (mockup approved separately)

Two screens, per the approved two-tier mockup.

**Integrations** (new nav item, after Ingest, before Review queue; route `#/integrations`). Tier 1
is a list of instance cards — each shows the URL, the sealed `token_env` reference + a sealed/not-set
chip + a host-allowlisted chip, and Edit/Remove. The add form captures name / URL / `token_env`
only (never a plaintext token; the copy says the token stays sealed on the server and the host must
be allowlisted). Tier 2 lives inside each card: the services the token can see (live discovery),
each row either **synced** (→ its catalog source, with Edit sync) or not (Add sync). The add/edit
sync form is target source + optional db/schema filters + tag-map override + table naming. When
OpenMetadata is unreachable the services section says so with a Retry, and the user can still add a
sync by typing a service name.

**Ingest** stays two peer paths: *Upload a file* (today's flow, unchanged) and *Pull from a
metadata service*. The metadata-service path is now a single **sync picker** (a dropdown grouped by
integration via `optgroup`, so each pull's instance is visible at a glance) plus **Preview import**;
its empty state links to Integrations. The preview/approve flow below is unchanged — summary tiles,
per-table diff, tag-map panel with unmapped tags flagged (a remap PATCHes the sync's override and
re-previews), quarantine preview, semantics-pending count, brake warning, then **Approve import**
with the platform's approval vocabulary → standard ingest result + review-queue handoff. The gates
strip tracks: you pick a sync → connector previews → you review mappings → you approve
(configuration itself moved upstream to Integrations).
