// Typed client for the FeatureGen API. Session headers come from the dev-session store —
// the API resolves roles server-side from them (stub for real session auth, M6 seam).
import { getSession } from './session'

export class ApiError extends Error {
  // Explicit fields + assignment instead of constructor parameter properties: the scaffold's
  // tsconfig sets erasableSyntaxOnly, which forbids the `public x` shorthand. Same public shape.
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const { user, roles } = getSession()
  // X-User is free text from the session bar. Header values must be ISO-8859-1, so a
  // non-Latin-1 name would make fetch throw before any request is sent. Percent-encode at
  // the boundary; the server sees the encoded name, which is acceptable for the dev stub.
  const res = await fetch(path, {
    ...init,
    headers: {
      'X-User': encodeURIComponent(user),
      'X-Roles': roles.join(','),
      ...(init?.headers ?? {}),
    },
  })
  if (!res.ok) {
    // statusText is empty under HTTP/2, so never let the message end up blank.
    let detail = res.statusText || `HTTP ${res.status}`
    try {
      const body = await res.json()
      if (typeof body.detail === 'string') {
        detail = body.detail
      } else if (Array.isArray(body.detail) && body.detail.length > 0) {
        // FastAPI 422 validation shape: detail is [{loc, msg, type}, ...]
        detail = body.detail
          .map((e: { loc?: unknown[]; msg?: string }) => `${(e.loc ?? []).join('.')}: ${e.msg}`)
          .join('; ')
      }
    } catch {
      // non-JSON error body (proxy HTML page and the like): keep the status fallback
    }
    throw new ApiError(res.status, detail)
  }
  return res.json() as Promise<T>
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

function patch<T>(path: string, body: unknown): Promise<T> {
  // JSON.stringify drops undefined keys, so a partial patch carries exactly the fields the
  // caller set — the server merges each over the current row and re-validates the whole result.
  return request(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export interface IngestResult {
  status: 'ingested' | 'held' | 'rejected'
  reason: string | null
  asserted: number
  staled: number
  quarantined: number
  flagged: string | null
}

export interface SearchHit {
  object_ref: string
  table: string
  column: string | null
  kind: string
  data_type: string | null
  definition: string | null
  is_grain: boolean
  is_as_of: boolean
  catalog_source: string
  concept: string | null
  domain: string | null
  sensitivity: string | null
  additivity: string | null
  unit: string | null
  currency: string | null
  entity: string | null
  score: number
}

// One faceted value with its live count over the read-scoped, freshness-gated set. The count is
// exclude-own-facet (what you would get if you added this value), computed by the backend; NULL
// facet values arrive as value:"(none)". sensitivity never lists a class the caller cannot read.
export interface FacetBucket {
  value: string
  count: number
}

// The repeated-value facet groups, in the order they ride the /search query string. AND across
// groups, OR within one. grain/as_of are boolean flags carried separately (=true restricts).
export const SEARCH_FACET_KEYS = [
  'source', 'domain', 'sensitivity', 'additivity', 'entity', 'kind',
] as const
export type SearchFacetKey = (typeof SEARCH_FACET_KEYS)[number]

// The selected filter state a search carries. Each facet is a repeated param; grain/as_of ride
// only when true.
export type SearchFilters = {
  [K in SearchFacetKey]?: string[]
} & {
  grain?: boolean
  as_of?: boolean
}

// GET /search response. `facets` is keyed by group name (the six above plus grain/as_of, which
// always emit a single "true" bucket that may be count 0); each list is capped 50, count desc.
// `total` counts tables AND columns (kind is a facet), so render honest "N result(s)" copy.
export interface SearchResult {
  hits: SearchHit[]
  facets: Record<string, FacetBucket[]>
  total: number
}

export interface QuarantineItem {
  row_index: number
  raw: Record<string, unknown>
  reason: string
}

export interface JoinEdge {
  from_ref: string
  to_ref: string
  cardinality: string | null
  resolved: boolean
}

export interface JoinStep {
  from_ref: string
  to_ref: string
  cardinality: string | null
}

export interface FeatureIdea {
  name: string
  description: string
  derives_from: string[]
  aggregation: string | null
  grain_table: string | null
  // (catalog_source, object_ref) pairs the backend resolves at recommend time. Registration
  // lineage MUST come from these, never from client-side source context: re-deriving the
  // catalog on the client would corrupt freshness and drift-impact for cross-catalog ideas.
  derives_pairs: [string, string][]
  // Honest verification stamp (currently "DESIGN-CHECKED"): structurally safe against leakage,
  // staleness, additivity, and point-in-time errors. Predictive value stays unverified until a
  // downstream backtest, so this is never a production-ready claim.
  verification: string
  // One-line causal WHY this feature operationalizes the goal; "" when the LLM omitted it.
  rationale: string
  // The critic's dissent note when it flagged but did not block the idea; "" when clean.
  critic_note: string
}

// One gauntlet rejection, shown to the human, never hidden. `code` carries the backend's
// RejectCode vocabulary (UNGROUNDED, AMBIGUOUS_CATALOG, UNKNOWN_COLUMN, LEAKAGE, STALE,
// ADDITIVITY, MIXED_UNITS, MIXED_CURRENCY, NO_POINT_IN_TIME, REDUNDANT, ALREADY_REGISTERED,
// CRITIC, NO_REVISION) but stays a plain string: an unknown code from a newer backend must
// still render, never break the client.
export interface Rejection {
  name: string
  reason: string
  code: string
}

export interface RecommendResult {
  proposals: FeatureIdea[]
  rejections: Rejection[]
}

// One validated set per strategy lens from the backend's deterministic router (subset of:
// unary, ratio, aggregation, temporal, distributional). Every feature ran the same gauntlet.
export interface FeatureSet {
  lens: string
  features: FeatureIdea[]
}

// ADVISORY set pick: a fit/coverage judgment over the metadata, never a performance claim.
// The caveat arrives from the backend and renders verbatim.
export interface SetRecommendation {
  recommended_lens: string
  reasoning: string
  caveat: string
}

export interface FeatureSetsResult {
  sets: FeatureSet[]
  // null when every set came back empty: the backend offers no recommendation over nothing.
  recommendation: SetRecommendation | null
  rejections: Rejection[]
}

// The candidate fields the backend's refine fix-hint needs, as the UI holds them.
export interface RefineCandidate {
  name: string
  description?: string
  derives_from?: string[]
  aggregation?: string | null
  grain_table?: string | null
}

export interface RefineRejection {
  reason: string
  code: string
}

// Both refine outcomes arrive as 200 data: a gauntlet rejection of the revision is something
// the reviewer acts on, not a transport error. Narrow with `'revised' in result`.
export type RefineResult = { revised: FeatureIdea } | { rejected: RefineRejection }

export interface Recipe {
  intent: string
  grain_table: string | null
  derives_from: string[]
  aggregation: string | null
  as_of_column: string | null
  join_path: JoinStep[]
}

export interface LeakageWarning {
  object_ref: string
  reason: string
}

export interface FeatureFreshness {
  fresh: boolean
  stale_sources: string[]
}

export interface FeatureSpecIn {
  name: string
  description: string
  grain_table: string | null
  aggregation: string | null
  as_of_column: string | null
  derives_from: { catalog_source: string; object_ref: string }[]
}

export function uploadFile(file: File, source: string): Promise<IngestResult> {
  const form = new FormData()
  form.append('file', file)
  form.append('source', source)
  return request('/uploads', { method: 'POST', body: form })
}

export function searchCatalog(
  q: string,
  filters: SearchFilters = {},
  limit = 20,
): Promise<SearchResult> {
  // Repeated params per multi-value facet (?source=deposits&source=cards): AND across groups,
  // OR within one. grain/as_of ride only when true, as the backend reads =true as restrict-to-
  // flag. A filtered search is therefore a shareable URL; the empty q browses the whole set.
  const params = new URLSearchParams()
  params.set('q', q)
  for (const key of SEARCH_FACET_KEYS) {
    for (const value of filters[key] ?? []) params.append(key, value)
  }
  if (filters.grain) params.append('grain', 'true')
  if (filters.as_of) params.append('as_of', 'true')
  params.set('limit', String(limit))
  return request(`/search?${params}`)
}

export function listQuarantine(source: string): Promise<QuarantineItem[]> {
  return request(`/sources/${encodeURIComponent(source)}/quarantine`)
}

// A server-side quarantine fix. `resolved: false` + a `reason` means the corrected row still fails the
// backend's authoritative validation (the browser preview is only a hint). A resolved row has left the
// queue and entered the catalog; a dismissed one has left the queue. Both hold until the next re-upload.
export interface QuarantineResolution {
  resolved: boolean
  reason: string
}

export function resolveQuarantineRow(
  source: string,
  rowIndex: number,
  edits: Record<string, string>,
): Promise<QuarantineResolution> {
  return post(`/sources/${encodeURIComponent(source)}/quarantine/${rowIndex}/resolve`, { edits })
}

export function dismissQuarantineRow(
  source: string,
  rowIndex: number,
): Promise<{ dismissed: boolean }> {
  return post(`/sources/${encodeURIComponent(source)}/quarantine/${rowIndex}/dismiss`, {})
}

export function columnJoins(objectRef: string, source: string): Promise<JoinEdge[]> {
  return request(
    `/columns/${encodeURIComponent(objectRef)}/joins?source=${encodeURIComponent(source)}`)
}

export function joinPath(source: string, from: string, to: string): Promise<JoinStep[] | null> {
  const qs = new URLSearchParams({ source, from, to })
  return request(`/join-path?${qs}`)
}

export async function registerFeature(spec: FeatureSpecIn): Promise<string> {
  const body = await post<{ feature_id: string }>('/features', spec)
  return body.feature_id
}

export function featureFreshness(featureId: string): Promise<FeatureFreshness> {
  return request(`/features/${encodeURIComponent(featureId)}/freshness`)
}

export async function featureImpact(objectRef: string, source: string): Promise<string[]> {
  const body = await request<{ feature_ids: string[] }>(
    `/columns/${encodeURIComponent(objectRef)}/feature-impact?source=${encodeURIComponent(source)}`)
  return body.feature_ids
}

// ---- catalog lineage graph (GET /graph/lineage) ------------------------------------------

export type LineageLayer = 'joins' | 'entity' | 'features'
export type LineageDirection = 'up' | 'down' | 'both'

export const LINEAGE_LAYERS: readonly LineageLayer[] = ['joins', 'entity', 'features']

// One node of the lineage map. Optional keys are OMITTED by the wire when absent, never null:
// a pending stub (resolved=false) carries NO catalog_source (its declaring source is only the
// id prefix), and feature/consumer nodes carry name/feature_id instead of object_ref/table.
// Node ids: "{catalog_source}:{object_ref}" | "feature:{feature_id}" | "consumer:{model_ref}".
export interface LineageNode {
  id: string
  kind: 'table' | 'column' | 'feature' | 'consumer'
  object_ref?: string
  table?: string
  column?: string
  catalog_source?: string
  feature_id?: string
  name?: string
  grain: boolean
  as_of: boolean
  sensitivity?: string
  entity?: string
  // column enrichment (omitted when null): controlled concept, business domain, and — only on the
  // table's as-of column — the availability basis (posted_at | ingested_at) from its as-of fact.
  concept?: string
  domain?: string
  as_of_basis?: string
  // feature stamps (omitted when absent): the honest verification stamp (e.g. DESIGN-CHECKED) and
  // the causal WHY it was born (its hypothesis); rationale is absent for directly-registered features.
  verification?: string
  rationale?: string
  // table provenance: ISO8601 of the source's last drift-vouch (omitted when never scanned) and the
  // count of this table's rows still in the review queue (omitted when zero).
  last_vouched_at?: string
  quarantine_pending?: number
  stale: boolean
  resolved: boolean
}

// Edge orientation for symmetric kinds (join, entity_bridge) points away from the anchor.
// `cardinality` is omitted when the declared edge has none; entity bridges never carry one.
// kind 'contains' (table -> column) is structural and always emitted regardless of layers.
export interface LineageEdge {
  from: string
  to: string
  layer: LineageLayer
  kind: 'contains' | 'join' | 'entity_bridge' | 'derives' | 'consumes'
  cardinality?: string
  resolved: boolean
}

export interface LineageGraph {
  nodes: LineageNode[]
  edges: LineageEdge[]
  truncated: boolean
}

export function lineageGraph(
  ref: string,
  source: string,
  opts: {
    direction?: LineageDirection
    depth?: number
    layers?: readonly LineageLayer[]
    // Aborted by the view when the anchor changes or the component unmounts, so a superseded
    // or orphaned fetch is cancelled at the transport instead of running to completion.
    signal?: AbortSignal
  } = {},
): Promise<LineageGraph> {
  const direction = opts.direction ?? 'both'
  const depth = opts.depth ?? 1
  const layers = opts.layers ?? LINEAGE_LAYERS
  // Hand-built query string: URLSearchParams would percent-encode the commas in `layers`,
  // and the wire contract pins the exact URL shape (layers=joins,entity,features).
  return request(
    `/graph/lineage?ref=${encodeURIComponent(ref)}&source=${encodeURIComponent(source)}` +
      `&direction=${direction}&depth=${depth}&layers=${layers.join(',')}`,
    opts.signal ? { signal: opts.signal } : undefined,
  )
}

// One row of the registry inventory (GET /features).
export interface FeatureListItem {
  feature_id: string
  name: string
  grain_table: string | null
  aggregation: string | null
  as_of_column: string | null
  verification: string
  created_at: string
}

// One model/consumer registered against a feature.
export interface FeatureConsumer {
  model_ref: string
  purpose: string
  environment: string
  registered_at: string
}

// The Feature 360 (GET /features/{id}): definition + verification + lineage + the HYPOTHESIS it was
// born from + the models that consume it. `contract` and `hypothesis` are null for a feature that was
// registered directly (not through the hypothesis-driven flow) — an honest absence, not an error.
export interface FeatureDetail {
  feature_id: string
  name: string
  description: string
  grain_table: string | null
  aggregation: string | null
  as_of_column: string | null
  verification: string
  created_at: string
  derives_from: { catalog_source: string; object_ref: string }[]
  contract: {
    contract_id: string
    definition: string
    version: number
    verification: string
    join_path: { from?: string; to?: string; kind?: string; cardinality?: string | null; via?: string }[]
  } | null
  hypothesis: {
    hypothesis: string
    definition: string
    intake_mode: string
    target_ref: string | null
  } | null
  consumers: FeatureConsumer[]
}

export function listFeatures(limit = 50): Promise<FeatureListItem[]> {
  return request(`/features?limit=${limit}`)
}

export function featureDetail(featureId: string): Promise<FeatureDetail> {
  return request(`/features/${encodeURIComponent(featureId)}`)
}

// ---- OpenMetadata connector, two-tier (integration + sync + discovery + preview/import) ----
//
// Grounded in OpenMetadata's own model — hierarchy DatabaseService -> Database -> Schema ->
// Table, and one bot JWT authenticates to the WHOLE instance (it sees every DatabaseService) —
// the connection splits in two:
//
//   INTEGRATION = one OpenMetadata instance (one base_url + one sealed token_env + a default
//                 tag_map). Generic; sees all services. Many syncs hang off it.
//   SYNC        = one DatabaseService (optionally narrowed by database/schema) -> one FeatureGen
//                 catalog source, with a tag-map override + table naming. The per-source binding.
//
// Ingest pulls from a SYNC (by sync_id), never a flat connector. Preview never writes; import
// runs ingest_upload in one transaction under the approving human's session identity.
//
// The bot token VALUE never crosses this client in either direction: rows carry only an env-var
// REFERENCE (token_env), create/patch reject any extra field (422) so a plaintext token cannot
// ride along, and no response ever contains the secret — only token_present (whether the
// referenced env var is set on the server).

export type TableNaming = 'table' | 'schema_table'

// One OpenMetadata instance as the wire returns it. `token_present` says whether the referenced
// environment variable is set on the server — the value itself is never serialized anywhere.
export interface Integration {
  integration_id: string
  name: string
  base_url: string
  token_env: string
  tag_map: Record<string, string>
  created_by: string
  created_at: string
  token_present: boolean
}

export interface IntegrationSpec {
  name: string
  base_url: string
  tag_map?: Record<string, string>
  // env-var REFERENCE, never a token; the server defaults it to FEATUREGEN_OM_TOKEN__<NAME>
  token_env?: string
}

// Every field optional: the server merges each provided field over the current row, then
// re-validates the whole result (so a patch can never leave a row off-namespace or off-allowlist).
export interface IntegrationPatch {
  name?: string
  base_url?: string
  tag_map?: Record<string, string>
  token_env?: string
}

export function listIntegrations(): Promise<Integration[]> {
  return request('/integrations')
}

export function getIntegration(integrationId: string): Promise<Integration> {
  return request(`/integrations/${encodeURIComponent(integrationId)}`)
}

export function createIntegration(spec: IntegrationSpec): Promise<Integration> {
  // token_env is carried only when the caller names a reference, so the server's name-derived
  // default (FEATUREGEN_OM_TOKEN__<NAME>) applies otherwise. Exactly the declared fields ride the
  // wire — extra fields are forbidden (422), precisely so a plaintext token can never ride along.
  const body: Record<string, unknown> = { name: spec.name, base_url: spec.base_url, tag_map: spec.tag_map ?? {} }
  if (spec.token_env) body.token_env = spec.token_env
  return post('/integrations', body)
}

export function patchIntegration(
  integrationId: string,
  changes: IntegrationPatch,
): Promise<Integration> {
  return patch(`/integrations/${encodeURIComponent(integrationId)}`, changes)
}

export function deleteIntegration(integrationId: string): Promise<{ deleted: boolean }> {
  return request(`/integrations/${encodeURIComponent(integrationId)}`, { method: 'DELETE' })
}

// One DatabaseService the integration's bot token can see (live from OM), flagged with whether a
// sync already binds it. Discovery is a convenience — the sync-create path never needs it, so an
// OM outage degrades gracefully (the caller can still add a sync by typing a service name).
export interface DiscoveredService {
  service_name: string
  service_type: string
  fqn: string
  synced: boolean
  sync_id: string | null
}

export function discoverServices(integrationId: string): Promise<DiscoveredService[]> {
  return request(`/integrations/${encodeURIComponent(integrationId)}/services`)
}

// One sync as the wire returns it: a service (optionally narrowed) bound to a catalog source.
export interface Sync {
  sync_id: string
  integration_id: string
  service_name: string
  database_filter: string | null
  schema_filter: string | null
  target_source: string
  tag_map_override: Record<string, string> | null
  table_naming: TableNaming
  created_by: string
  created_at: string
  last_import_at: string | null
}

export interface SyncSpec {
  service_name: string
  target_source: string
  database_filter?: string | null
  schema_filter?: string | null
  // null (or omitted) inherits the integration's tag_map wholesale; a map OVERRIDES it per tag.
  tag_map_override?: Record<string, string> | null
  table_naming?: TableNaming
}

export interface SyncPatch {
  service_name?: string
  target_source?: string
  database_filter?: string | null
  schema_filter?: string | null
  tag_map_override?: Record<string, string> | null
  table_naming?: TableNaming
}

export function listSyncs(integrationId: string): Promise<Sync[]> {
  return request(`/integrations/${encodeURIComponent(integrationId)}/syncs`)
}

export function getSync(integrationId: string, syncId: string): Promise<Sync> {
  return request(
    `/integrations/${encodeURIComponent(integrationId)}/syncs/${encodeURIComponent(syncId)}`)
}

export function createSync(integrationId: string, spec: SyncSpec): Promise<Sync> {
  // Every declared field rides the wire (server model forbids extras, 422). Optional scope and
  // override default to null; table naming defaults to bare table name.
  return post(`/integrations/${encodeURIComponent(integrationId)}/syncs`, {
    service_name: spec.service_name,
    target_source: spec.target_source,
    database_filter: spec.database_filter ?? null,
    schema_filter: spec.schema_filter ?? null,
    tag_map_override: spec.tag_map_override ?? null,
    table_naming: spec.table_naming ?? 'table',
  })
}

export function patchSync(
  integrationId: string,
  syncId: string,
  changes: SyncPatch,
): Promise<Sync> {
  return patch(
    `/integrations/${encodeURIComponent(integrationId)}/syncs/${encodeURIComponent(syncId)}`,
    changes)
}

export function deleteSync(integrationId: string, syncId: string): Promise<{ deleted: boolean }> {
  return request(
    `/integrations/${encodeURIComponent(integrationId)}/syncs/${encodeURIComponent(syncId)}`,
    { method: 'DELETE' })
}

export interface TagMapEntry {
  om_tag: string
  mapped_to: string
  unmapped: boolean
  count: number
}

export interface PreviewTable {
  // 'removed': a table in the current catalog the pull no longer includes — import DELETE-then-
  // rebuilds the source, so it would be dropped and its facts staled. Surfaced so the human never
  // approves a loss the dry run didn't show.
  table: string
  status: 'new' | 'changed' | 'unchanged' | 'removed'
  columns: number
  quarantine: { column: string; reason: string }[]
  changes: string[]
}

export interface AsOfSuggestion {
  table: string
  column: string
  hint: string
}

// The dry run a human approves. `snapshot_hash` is the honesty anchor: import must present it
// back, and the server answers 409 if OpenMetadata moved since this preview was taken. The tag
// map shown here is the EFFECTIVE map: integration.tag_map merged with the sync's override.
export interface SyncPreview {
  summary: {
    tables: number
    columns: number
    new: number
    changed: number
    unchanged: number
    removed: number
    would_quarantine: number
    semantics_pending: number
  }
  tag_map: TagMapEntry[]
  tables: PreviewTable[]
  brake: { would_hold: boolean; reason: string | null }
  as_of_suggestions: AsOfSuggestion[]
  snapshot_hash: string
}

export function previewSync(syncId: string): Promise<SyncPreview> {
  // No body: the sync and its integration carry the URL, token, scope, and effective tag map.
  return request(`/syncs/${encodeURIComponent(syncId)}/preview`, { method: 'POST' })
}

// Import wraps the standard IngestResult (same pipeline, same shape) with the audit record id
// and the review-queue handoff counts.
export interface SyncImportResult {
  result: IngestResult
  import_id: string
  review_queue: { quarantined: number; semantics_pending: number }
}

export function importSync(syncId: string, snapshotHash: string): Promise<SyncImportResult> {
  return post(`/syncs/${encodeURIComponent(syncId)}/import`, { snapshot_hash: snapshotHash })
}

export function recommendFeatures(
  objective: string,
  catalogSource: string | null,
  targetRef: string | null = null,
  entity: string | null = null,
  feedback: string | null = null,
): Promise<RecommendResult> {
  return post('/features/recommend', {
    objective,
    catalog_source: catalogSource,
    target_ref: targetRef,
    // Entity-scoped gather: candidates come from every catalog holding this entity.
    entity,
    // HUMAN guidance for the whole round; every candidate still runs the full gauntlet.
    feedback,
  })
}

export function recommendFeatureSets(
  objective: string,
  catalogSource: string | null,
  targetRef: string | null = null,
  entity: string | null = null,
  feedback: string | null = null,
): Promise<FeatureSetsResult> {
  // Same request body as /features/recommend; the response groups proposals by strategy lens
  // and adds the advisory pick plus the rejections aggregated across every lens's rounds.
  return post('/features/recommend-sets', {
    objective,
    catalog_source: catalogSource,
    target_ref: targetRef,
    entity,
    feedback,
  })
}

export function refineCandidate(
  candidate: RefineCandidate,
  instruction: string,
  catalogSource: string | null = null,
  entity: string | null = null,
  targetRef: string | null = null,
  objective: string | null = null,
): Promise<RefineResult> {
  return post('/features/refine', {
    // Defaults applied at the boundary so the wire always carries the full candidate shape
    // the backend declares (description "", derives_from [], aggregation/grain_table null).
    candidate: {
      name: candidate.name,
      description: candidate.description ?? '',
      derives_from: candidate.derives_from ?? [],
      aggregation: candidate.aggregation ?? null,
      grain_table: candidate.grain_table ?? null,
    },
    instruction,
    catalog_source: catalogSource,
    entity,
    target_ref: targetRef,
    // The round's prediction goal: the engine revises against the objective the candidate
    // was generated for, not the instruction alone.
    objective,
  })
}

export function featureRecipe(query: string, catalogSource: string): Promise<Recipe> {
  return post('/features/recipe', { query, catalog_source: catalogSource })
}

export async function leakageCheck(
  derivesFrom: string[],
  targetRef: string,
): Promise<LeakageWarning[]> {
  const body = await post<{ warnings: LeakageWarning[] }>('/features/leakage-check', {
    derives_from: derivesFrom,
    target_ref: targetRef,
  })
  return body.warnings
}
