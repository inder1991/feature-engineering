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

export function searchCatalog(q: string, limit = 20): Promise<SearchHit[]> {
  return request(`/search?q=${encodeURIComponent(q)}&limit=${limit}`)
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
