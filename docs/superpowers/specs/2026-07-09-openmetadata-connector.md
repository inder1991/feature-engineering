# OpenMetadata Connector — Design Spec

Date: 2026-07-09. Status: DESIGNED (mockup approved separately; not yet built).
Purpose: bring tables and columns into the FeatureGen catalog directly from an OpenMetadata
instance, without file uploads — while preserving every guarantee the upload path provides.

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

- The connector runs as a **service identity** (`service:openmetadata-connector`, attested per
  the SP-0.5 identity rules); every ingest event is attributed to it, with the human who clicked
  Confirm recorded as the approving actor on the import record.
- **The brake still gates**: a sync that would remove >30% of a source's objects (or overlap
  <60%) is held exactly like a hostile upload; a human resolves it.
- **Freshness = sync recency**: each successful sync advances the source's drift watermark. A
  source whose connector stops syncing goes honestly stale in ≤24h, catalog-wide.
- **RBAC** (post-2026-07-07 model): configuring a connector and confirming imports requires
  `data_owner` on the target source (or `platform_admin`); preview requires `catalog_viewer`.

## Config and secrets

`connector_config` row per configured connection: base URL, scope filters (service / database /
schema patterns), tag map, target source name, schedule (v2). The bot token is **never stored
in plaintext**: sealed via the existing KMS envelope (`featuregen.privacy.kms`) or referenced
from environment (`FEATUREGEN_OM_TOKEN__<name>`) per deployment preference; config rows store
only the reference + key id.

## API surface (additive)

| Endpoint | Behavior |
|---|---|
| `POST /connectors/openmetadata/preview` | body: config (or configured connector id). Pulls + translates WITHOUT ingesting; returns the dry-run: `{summary:{tables,columns,new,changed,unchanged,would_quarantine,semantics_pending}, tag_map:[{om_tag,mapped_to,unmapped:bool,count}], tables:[{table,status:new|changed|unchanged,columns,quarantine:[...],changes:[...]}], brake:{would_hold:bool,reason?}}` |
| `POST /connectors/openmetadata/import` | body: connector id + the previewed snapshot hash (stale-preview protection: if OM moved since preview, 409 with re-preview guidance). Runs the translation into `ingest_upload` in ONE transaction per source. Returns the standard `IngestResult` + import record id. |
| `GET /connectors` / `POST /connectors` / `DELETE /connectors/{id}` | manage configured connections (RBAC-gated). |

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
translation table above (each row), tag-map fallback→quarantine, PK/FK mapping, pagination
assembly, preview/import snapshot-hash mismatch 409, brake hold on a shrunken pull, RBAC
denials, secrets never serialized in config responses.

## UI (mockup approved separately)

The Upload screen becomes **Ingest** with two peer paths (same pattern as the Workbench hero):
*Upload a file* (today's flow, unchanged) and *Connect a metadata service*. The connector path:
configure → **Preview import** (summary tiles, per-table diff list, tag-map panel with unmapped
tags flagged, quarantine preview, semantics-pending count, brake warning when applicable) →
**Approve import** with the platform's approval vocabulary → standard ingest result + review
queue handoff for pending semantics. Human gates strip mirrors the generation screen: you
configure → connector previews → you review mappings → you approve.
