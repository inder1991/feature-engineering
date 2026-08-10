// Typed client for the FeatureGen API. Session headers come from the dev-session store —
// the API resolves roles server-side from them (stub for real session auth, M6 seam).
import { getSession } from './session'

export class ApiError extends Error {
  // Explicit fields + assignment instead of constructor parameter properties: the scaffold's
  // tsconfig sets erasableSyntaxOnly, which forbids the `public x` shorthand. Same public shape.
  status: number
  detail: string
  // The X-Ingestion-Run-Id response header when the failed request carried one (POST /uploads
  // and /syncs/{id}/import attach it to every post-open 4xx/5xx), so a failed ingest's run
  // record stays inspectable via GET /ingestion-runs/{id}. null when the server sent no header;
  // optional in the constructor so existing throw/new sites keep working unchanged.
  ingestionRunId: string | null
  // The machine-readable `error_code` a HANDLER-level refusal carries beside `detail` (e.g.
  // SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION on a 422). null whenever the server sent none —
  // framework validation errors deliberately have no code, so a caller must never read the
  // absence as "some other code".
  errorCode: string | null
  constructor(
    status: number,
    detail: string,
    ingestionRunId: string | null = null,
    errorCode: string | null = null,
  ) {
    super(detail)
    this.status = status
    this.detail = detail
    this.ingestionRunId = ingestionRunId
    this.errorCode = errorCode
  }
}

// Core transport: same auth headers + error handling as always, but hands back the Response
// alongside the parsed body for the few callers that need transport metadata (the ingest run-id
// header). Everything else goes through the body-only `request` wrapper below.
async function requestWithResponse<T>(
  path: string,
  init?: RequestInit,
): Promise<{ body: T; response: Response }> {
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
    let errorCode: string | null = null
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
      // Two 422 bodies exist on the same route and neither may crash the client: a HANDLER
      // refusal is {detail: string, error_code: string}, while a type failure caught before the
      // handler ran keeps FastAPI's native list-`detail` and carries NO code.
      if (typeof body.error_code === 'string') errorCode = body.error_code
    } catch {
      // non-JSON error body (proxy HTML page and the like): keep the status fallback
    }
    // A failed ingest still opened a run: keep its id (header) on the error, or it is lost —
    // the JSON body of a 4xx/5xx never carries it.
    throw new ApiError(res.status, detail, res.headers.get('X-Ingestion-Run-Id'), errorCode)
  }
  return { body: (await res.json()) as T, response: res }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const { body } = await requestWithResponse<T>(path, init)
  return body
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
  // Catalog objects this upload dropped/renamed/type-changed — not facts staled (#30).
  changed_objects: number
  quarantined: number
  flagged: string | null
  // MF-5 truthful counts (backend IngestResult additive fields). Optional so older fixtures and a
  // pre-MF-5 backend keep compiling; the callout renders the second line only when they arrive.
  // objects_stored == tables + columns; containment_edges == columns (one contains edge each);
  // facts_asserted mirrors `asserted`; join_candidates is Pass C's count (0 when off); Pass B
  // splits into proposed (a synthesis with a grain or as-of) + abstained (neither).
  objects_stored?: number
  tables?: number
  columns?: number
  containment_edges?: number
  facts_asserted?: number
  join_candidates?: number
  passb_proposed?: number
  passb_abstained?: number
  // D4 semantic-binding counts (behind OVERLAY_SEMANTIC_BINDING_CANDIDATES/_PROPOSALS; all 0 when
  // off). candidates = persisted candidate rows (all dispositions); proposed = strong candidates
  // routed to an E1 DRAFT fact; abstained = strong candidates whose propose was not accepted (a
  // re-upload duplicate); failed = tables whose candidate/proposal work hit its fail-soft except.
  semantic_binding_candidates?: number
  semantic_binding_proposed?: number
  semantic_binding_abstained?: number
  semantic_binding_failed?: number
  // Cross-catalog entity-link accounting (behind OVERLAY_ENTITY_BRIDGES; all 0 when off).
  // Confirmation records review but is not an availability gate. `considered` means fully scored,
  // while `truncated` counts cheap block matches omitted by the bounded enumerator.
  entity_bridges_proposed?: number
  entity_bridge_candidates_considered?: number
  entity_bridge_candidates_retained?: number
  entity_bridge_candidates_suppressed?: number
  entity_bridge_candidates_truncated?: number
  // CLIENT-attached from the X-Ingestion-Run-Id response header, never a body field: the id of
  // the per-stage run record behind GET /ingestion-runs/{id}. Optional so existing fixtures and
  // callers keep compiling; null when the server sent no header.
  ingestion_run_id?: string | null
}

// ---- ingestion runs (read-only per-stage record of one upload/import) ------------------------
// One pipeline stage of an ingestion run. `stage` and `state` stay open strings: an unknown
// value from a newer backend must summarize (or stay quiet), never break the client. Known
// states include succeeded | partial | failed | skipped_no_client | disabled | not_applicable |
// not_run | lagged | deferred | audit_degraded; known stages include parse, validation, brake,
// fact_assertion, drift, enrich_concept/definition/domain, pass_b, pass_c, governed_joins,
// projection_drain, quarantine.
export interface IngestionStage {
  stage: string
  attempt: number
  state: string
  reason_code: string | null
  detail: Record<string, unknown> | null
  started_at: string | null
  completed_at: string | null
}

// One append-only status transition of a run (opened -> ingested/held/rejected/failed), in
// recorded order. reason_code is the redacted vocabulary token, never raw error text.
export interface IngestionStatusEvent {
  status: string
  at: string
  reason_code: string | null
}

// The GET /ingestion-runs/{id} record as the backend returns it (overlay/upload/ingestion_run
// get_run): the run row keyed `id` — NOT run_id — plus who/what/when manifest facts, the
// status history, and the per-stage reports. The wire carries a few more columns (file_sha256,
// fingerprints, effective_config, heartbeat_at); declare only what the client reads, under the
// exact backend names. Runs exist for EVERY outcome now — ingested, held, rejected, and failed.
export interface IngestionRun {
  id: string
  origin_type: string
  catalog_source: string
  filename: string | null
  actor_subject: string | null
  actor_role_claims: string[]
  authorization_decision: string | null
  status: string
  row_count: number | null
  quarantined_count: number | null
  started_at: string | null
  completed_at: string | null
  redacted_failure_code: string | null
  status_history: IngestionStatusEvent[]
  stages: IngestionStage[]
}

export function getIngestionRun(runId: string): Promise<IngestionRun> {
  return request(`/ingestion-runs/${encodeURIComponent(runId)}`)
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
  // The projected DISPLAY label (migration 1042) — what the asset page renders. Distinct from
  // `sensitivity` above, which is the raw read-scope tag a source file declares; on a catalog that
  // declares none, this is the only sensitivity a column has.
  sensitivity_display: string | null
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

// Hits per page. The backend caps `limit` at 100; this is the page the screen walks in, and the
// stride its Next/Previous controls move by.
export const SEARCH_PAGE_SIZE = 20

// The repeated-value facet groups, in the order they ride the /search query string. AND across
// groups, OR within one. grain/as_of are boolean flags carried separately (=true restricts).
export const SEARCH_FACET_KEYS = [
  'source', 'domain', 'sensitivity', 'sensitivity_display', 'additivity', 'entity', 'kind',
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
/**
 * Whether a load-bearing catalog projection was BEHIND when a read was served (semantic Task 6).
 * `ready` on the happy path — ALWAYS present, because an omitted field cannot distinguish
 * "checked and fine" from "never checked". A `lagged` marker is a disclosure, never a refusal:
 * the rows are still served, and the UI should say the view may not yet reflect the newest
 * resolved semantics rather than hide it.
 */
export interface ProjectionStatus {
  status: 'ready' | 'lagged'
  code: string
  detail: string
}

export interface SearchResult {
  hits: SearchHit[]
  facets: Record<string, FacetBucket[]>
  total: number
  projection: ProjectionStatus
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
  // Opaque, revision-scoped identity on /contract/considered-set responses. It distinguishes variants
  // that share a display name and is the only scoped drafting token; other feature APIs may omit it.
  option_id?: string
  generation_source?: 'recipe' | 'llm_freeform' | 'user_defined' | string
  recipe_id?: string
  candidate_status?: string
  planner_applicability?: string
  origin?: string
  path_authority?: string
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
  // Task 3 near-label critic (flag-only, origin-blind): {no_finding | too_close | abstain},
  // absent when the critic did not run. Deliberately no value that reads as "cleared" — the
  // critic cannot clear anything. Only `too_close` renders a warning; nothing is ever removed.
  near_label_verdict?: 'no_finding' | 'too_close' | 'abstain' | null
  near_label_rationale?: string
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

export async function uploadFile(
  file: File,
  source: string,
  // Optional catalog-narrative JSON (Release-A profiles): validated server-side BEFORE any write
  // and committed atomically with a successful ingest; ignored (with a server warning) while
  // FEATUREGEN_DATASET_PROFILES is off. Never required — a missing profile never blocks a catalog.
  catalogProfileJson?: string,
): Promise<IngestResult> {
  const form = new FormData()
  form.append('file', file)
  form.append('source', source)
  if (catalogProfileJson !== undefined && catalogProfileJson.trim() !== '') {
    form.append('catalog_profile_json', catalogProfileJson)
  }
  const { body, response } = await requestWithResponse<IngestResult>('/uploads', {
    method: 'POST',
    body: form,
  })
  return { ...body, ingestion_run_id: response.headers.get('X-Ingestion-Run-Id') }
}

export function searchCatalog(
  q: string,
  filters: SearchFilters = {},
  limit = SEARCH_PAGE_SIZE,
  offset = 0,
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
  // Windows the hits only: `total` and the facet counts still describe the whole matching set, so
  // the caller can tell from any page whether another one exists. Omitted when zero so the first
  // page's request is byte-identical to what it has always been.
  if (offset > 0) params.set('offset', String(offset))
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

// ---- semantics-pending queue (#22): owner completion of semantically blank columns ----------
// A column can land structurally vouched but semantically blank (the OpenMetadata connector does
// this BY DESIGN). The queue lists them; completion is a data owner setting the declared facts,
// exactly as a file declaration would have.

// One pending column as the wire returns it. `missing` names the semantic fields still absent,
// from the backend's set (as_of, additivity, unit, currency, entity) — kept open strings so a
// newer backend field renders instead of breaking the client.
export interface SemanticsPendingItem {
  object_ref: string
  table: string
  column: string
  data_type: string | null
  missing: string[]
}

export function getSemanticsPending(source: string): Promise<SemanticsPendingItem[]> {
  return request(`/sources/${encodeURIComponent(source)}/semantics-pending`)
}

// The values an owner is SETTING — any subset. Omitted fields never ride the wire
// (JSON.stringify drops undefined keys): completion sets values, it does not clear them.
export interface SemanticsValues {
  additivity?: string
  unit?: string
  currency?: string
  entity?: string
  is_as_of?: boolean
}

// 422 on a value outside the upload vocabularies, 409 if is_as_of would give the table a second
// as-of axis, 404 on an unknown ref — all arrive as ApiError with the backend's own sentence.
export function completeSemantics(
  source: string,
  objectRef: string,
  values: SemanticsValues,
): Promise<{ completed: boolean; applied: Record<string, unknown> }> {
  return post(
    `/sources/${encodeURIComponent(source)}/columns/${encodeURIComponent(objectRef)}/semantics`,
    values,
  )
}

// ---- join governance (confirmation surface): list / confirm / reject discovered joins -------
// Pass C proposes joins from metadata only; each needs TWO distinct admins before it projects to
// an operational graph edge. The score is advisory — approval is gated on the human checklist.

// One Pass C signal as the evidence record serializes it (asdict of SignalEvidence).
export interface JoinSignal {
  signal_name: string
  score_delta: number
  evidence_refs?: string[]
  explanation?: string
}

// Shaped evidence from the read model. Every field can be defaulted (parse status "partial") or
// the whole object empty (status "missing"/"invalid") — render defensively, never assume.
export interface JoinEvidence {
  score?: number | null
  positive_signals?: JoinSignal[]
  negative_signals?: JoinSignal[]
  namespace_compatibility?: string | null
  namespace_reason_codes?: string[]
  grain_status?: string | null
  grain_evidence?: string[]
  explanation?: string
  warnings?: string[]
}

export interface JoinApproval {
  subject: string | null
  display_name: string | null
  role: string | null
  note: string | null
  confirmed_at: string | null
}

export interface JoinTask {
  task_id: string
  side: string | null
  status: string
}

export interface JoinProposal {
  fact_key: string
  tasks: JoinTask[]
  from: { table: string; column: string }
  to: { table: string; column: string }
  cardinality: string | null
  proposed_direction: string
  status: 'PROPOSED' | 'PARTIALLY_CONFIRMED'
  approvals: JoinApproval[]
  evidence: JoinEvidence
  evidence_version: string | null
  evidence_parse_status: 'parsed' | 'partial' | 'missing' | 'invalid'
}

// Structured rejection vocabulary — mirrors the backend's Literal exactly; the category is a
// first-class analytics key surfaced on the governance dashboard, the note is free text.
export const REJECT_CATEGORIES = [
  'wrong_direction', 'wrong_cardinality', 'different_entity', 'not_a_real_key',
  'needs_data_check',
] as const
export type RejectCategory = (typeof REJECT_CATEGORIES)[number]

export interface JoinConfirmResult {
  // PARTIALLY_CONFIRMED after the first approval; VERIFIED after the second.
  governance_status: string
  // 'projected' | 'pending' | 'not_applicable' — pending defers to the next caught-up ingest.
  operational_projection: string
  approvals: JoinApproval[]
}

// A governed-join divergence: a re-upload retargeted or dropped a joins_to that admins had
// VERIFIED. Advisory only — the verified join stays operational until an admin acts. For kind
// "retargeted" the new target also appears in `proposals` as its own pending proposal (the
// existing confirm flow adopts it); for "dropped" declared_to_ref is null.
export interface JoinDivergence {
  id: number
  from_ref: string
  verified_to_ref: string
  declared_to_ref: string | null
  kind: 'retargeted' | 'dropped'
  detected_at: string
}

export function listJoinProposals(
  source: string,
): Promise<{
  source: string
  proposals: JoinProposal[]
  divergences: JoinDivergence[]
  next_cursor: string | null
}> {
  return request(`/sources/${encodeURIComponent(source)}/governance/joins`)
}

export function confirmJoin(
  factKey: string,
  body: { note?: string },
): Promise<JoinConfirmResult> {
  return post(`/governance/joins/${encodeURIComponent(factKey)}/confirm`, {
    note: body.note ?? null,
  })
}

export function rejectJoin(
  factKey: string,
  body: { category: RejectCategory; note?: string },
): Promise<{ governance_status: string; category: string }> {
  return post(`/governance/joins/${encodeURIComponent(factKey)}/reject`, {
    category: body.category,
    note: body.note ?? null,
  })
}

// Acknowledge a divergence ("seen — the verified join stands / is being handled"). Advisory
// bookkeeping only: it never touches the approved_join fact or its operational edge, and a
// later re-upload that still diverges re-opens the row. Returns the acknowledged row.
export function acknowledgeJoinDivergence(divergenceId: number): Promise<{
  id: number
  catalog_source: string
  from_ref: string
  verified_to_ref: string
  declared_to_ref: string | null
  kind: 'retargeted' | 'dropped'
  detected_at: string
  acknowledged_at: string
  acknowledged_by: string
}> {
  return post(`/governance/joins/divergences/${divergenceId}/acknowledge`, {})
}

// ---- table-fact governance (Pass B confirm surface): grain / availability_time facts --------
// Pass B proposes grain and as-of facts from LLM enrichment — never value-profiled. Unlike
// joins these are SINGLE-confirmer: one platform-admin approve reaches VERIFIED directly
// (four-eyes still holds — the proposer is the service enrichment actor, never the confirmer),
// then the fact projects synchronously into the operational overlay.

// proposed_value by fact_type — grain: {columns, is_unique}; availability_time: {column, basis}.
// evidence_parse_status "missing" means the stored value did not parse — render defensively.
export interface TableFactProposal {
  fact_key: string
  task_id: string
  target_event_id: string
  fact_type: 'grain' | 'availability_time'
  table: string
  proposed_value: {
    columns?: string[]
    is_unique?: boolean
    column?: string
    basis?: string
  } | null
  status: 'PROPOSED'
  origin: string
  advisory: {
    table_role: string | null
    primary_entity: string | null
    event_or_snapshot: string | null
  }
  evidence_parse_status: string
}

// Structured rejection vocabulary — mirrors the backend's Literal exactly; the category is a
// first-class analytics key surfaced on the governance dashboard, the note is free text.
export const TABLE_FACT_REJECT_CATEGORIES = [
  'wrong_grain_columns', 'wrong_as_of_column', 'not_unique', 'needs_data_check',
] as const
export type TableFactRejectCategory = (typeof TABLE_FACT_REJECT_CATEGORIES)[number]

export function listTableFactProposals(
  source: string,
): Promise<{ source: string; proposals: TableFactProposal[]; next_cursor: string | null }> {
  return request(`/sources/${encodeURIComponent(source)}/governance/table-facts`)
}

export function confirmTableFact(
  factKey: string,
  body: { note?: string },
): Promise<{ governance_status: string; operational_projection: string }> {
  return post(`/governance/table-facts/${encodeURIComponent(factKey)}/confirm`, {
    note: body.note ?? null,
  })
}

// ── semantic bindings (currency / entity) — the E4a four-eyes confirm surface ─────────────────
// The reject vocabulary is the server's closed set (RejectSemanticBindingRequest).
export const SEMANTIC_BINDING_REJECT_CATEGORIES = [
  'wrong_entity', 'wrong_currency_column', 'not_a_binding', 'needs_data_check',
] as const
export type SemanticBindingRejectCategory = (typeof SEMANTIC_BINDING_REJECT_CATEGORIES)[number]

export function confirmSemanticBinding(
  factKey: string,
  body: { note?: string },
): Promise<{ governance_status: string; operational_projection: string }> {
  return post(`/governance/semantic-bindings/${encodeURIComponent(factKey)}/confirm`, {
    note: body.note ?? null,
  })
}

export function rejectSemanticBinding(
  factKey: string,
  body: { category: SemanticBindingRejectCategory; note?: string },
): Promise<{ governance_status: string; category: string }> {
  return post(`/governance/semantic-bindings/${encodeURIComponent(factKey)}/reject`, {
    category: body.category, note: body.note ?? null,
  })
}

export function rejectTableFact(
  factKey: string,
  body: { category: TableFactRejectCategory; note?: string },
): Promise<{ governance_status: string; category: string }> {
  return post(`/governance/table-facts/${encodeURIComponent(factKey)}/reject`, {
    category: body.category,
    note: body.note ?? null,
  })
}

// ---- entity-bridge governance (the CROSS-CATALOG confirm surface) ---------------------------
// An entity bridge links the SAME business entity in two catalogs (the customer on a transaction
// and the customer in the customer master). Confirming one records that a HUMAN AGREES WITH THE
// SEMANTIC RELATIONSHIP — it is not permission to execute anything: a proposed bridge is already
// consumed, and whether the crossing may run automatically is the separate production-eligibility
// axis the queue reports on its own.
//
// Four-eyes is a SERVER decision, projected into `available_actions` on the queue item — never
// recomputed here. A VERIFIED bridge carries NO actions at all (reject_fact denies it; re-verify
// has its own flow), so the UI must not offer one.

// Why a reviewer says no to a cross-catalog identifier link — mirrors the backend
// EntityBridgeRejectCategory Literal exactly. `values_do_not_match` is the type-compatible pair
// whose populations simply do not overlap, which `type_basis: 'declared'` cannot see.
export const ENTITY_BRIDGE_REJECT_CATEGORIES = [
  'not_the_same_entity', 'wrong_column', 'values_do_not_match', 'duplicate_bridge',
  'needs_data_check',
] as const
export type EntityBridgeRejectCategory = (typeof ENTITY_BRIDGE_REJECT_CATEGORIES)[number]

export function confirmEntityBridge(
  factKey: string,
  body: { note?: string },
): Promise<{
  governance_status: string
  review_projection: string
  review_controls_availability: false
  review_controls_execution: false
}> {
  return post(`/governance/entity-bridges/${encodeURIComponent(factKey)}/confirm`, {
    note: body.note ?? null,
  })
}

export function rejectEntityBridge(
  factKey: string,
  body: { category: EntityBridgeRejectCategory; note?: string },
): Promise<{
  governance_status: string
  category: string
  review_projection: string
  review_controls_availability: false
  review_controls_execution: false
}> {
  return post(`/governance/entity-bridges/${encodeURIComponent(factKey)}/reject`, {
    category: body.category,
    note: body.note ?? null,
  })
}

// One key's outcome in a group reject. The route fans out to ONE governed command per key, each
// with its own audit row and its own savepoint, so PARTIAL success is the ordinary case and there
// is no single status code for it: the call always resolves 200 and the caller renders the split.
// `outcome` is rejected | already_rejected | denied | not_found | failed — kept an open string so
// a newer backend outcome still renders.
export interface BulkBridgeRejectResult {
  fact_key: string
  outcome: string
  detail?: string
  review_projection?: string
}

export function bulkRejectEntityBridges(
  factKeys: string[],
  category: EntityBridgeRejectCategory,
  note?: string,
): Promise<{
  category: string
  counts: Record<string, number>
  results: BulkBridgeRejectResult[]
}> {
  return post('/governance/entity-bridges/bulk-reject', {
    fact_keys: factKeys,
    category,
    note: note ?? null,
  })
}

export interface BridgeRealizationView {
  realization_id: string
  realization_revision_id: string
  bridge_fact_key: string
  direction: { from: string; to: string }
  from_endpoint: Record<string, unknown>
  to_endpoint: Record<string, unknown>
  column_pairs: Array<{
    from_logical_column_ref: string
    to_logical_column_ref: string
  }>
  cardinality: string
  cardinality_label: string
  cardinality_basis: string
  predicates: Array<Record<string, unknown>>
  missing_requirements: Array<Record<string, string>>
  applicability_scope: {
    scope_id: string
    execution_tier: string
    purposes: string[]
    environment: string
    partition_scope_ref: string | null
  }
  dependency_snapshot_id: string
  safety_status: string
  review_status: string
  lifecycle: string
  pointer_version: number
  execution_eligible: boolean
  execution_reason_codes: string[]
  evidence_fresh: boolean
  evidence: Array<Record<string, unknown>>
  metrics: Array<Record<string, unknown>>
  assessment: Record<string, unknown> | null
  available_review_actions: string[]
  profile_action: { state: string; label: string } | null
  review_controls_execution: false
}

export function listBridgeRealizations(
  source: string,
  bridgeFactKey?: string,
): Promise<{ source: string; realizations: BridgeRealizationView[]; next_cursor: null }> {
  const query = bridgeFactKey
    ? `?bridge_fact_key=${encodeURIComponent(bridgeFactKey)}`
    : ''
  return request(
    `/sources/${encodeURIComponent(source)}/governance/bridge-realizations${query}`,
  )
}

export function reviewBridgeRealization(
  realization: BridgeRealizationView,
  approved: boolean,
  note?: string,
): Promise<{
  review_status: string
  safety_status: string
  execution_eligible: boolean
  pointer_version: number
  review_projection: string
  review_controls_execution: false
  realization: BridgeRealizationView
}> {
  const action = approved ? 'confirm' : 'reject'
  return post(
    `/governance/bridge-realizations/${encodeURIComponent(realization.realization_id)}/${action}`,
    {
      realization_revision_id: realization.realization_revision_id,
      expected_pointer_version: realization.pointer_version,
      note: note ?? null,
    },
  )
}

// ---- the cross-catalog governance QUEUE (GET /governance/queue) ------------------------------
// One request, no source argument: every pending decision the caller may see across every catalog
// they may see. Every other governance listing is keyed by a slug the operator had to already know.
//
// THE TWO CODE FIELDS ARE TWO INDEPENDENT AXES AND MUST NEVER BE FUSED:
//   * `state` / `state_code`   — the HUMAN axis (has someone endorsed the semantics?)
//   * `production_eligibility` / `production_eligibility_code` — the AUTOMATIC axis (is the
//     directional realization / cardinality resolved, so the crossing may run in production?)
// Human review controls neither availability nor execution: a row can legitimately be unreviewed
// AND production-eligible, or human-endorsed AND sandbox-only. Bind to the *_code fields — the
// labels are display text the backend may reword; the codes are the contract.

// One "already depended on by" category. `count` is null unless `state === 'counted'`, so the
// wire itself makes "unmeasured" unrepresentable as a number: an empty store answers
// `not_tracked_yet` and MUST render as "not tracked yet", never 0. `state` is
// counted | not_tracked_yet | unreadable (open string for forward compatibility); `reason` says
// WHY a category cannot be measured and `basis` says exactly what a count would mean.
export interface GovernanceQueueUsage {
  category: string
  state: string
  count: number | null
  display: string
  store: string
  basis: string
  reason: string
}

// One pending decision, whatever its kind. `kind` is entity_bridge | approved_join | grain |
// availability_time (open string). `catalogs` holds BOTH catalogs for a bridge and the ONE
// catalog for a join / table fact — the join listing filters on from_ref.catalog_source only and
// is NOT endpoint-symmetric, so a join must never be presented as cross-catalog.
// `available_actions` is the SERVER's sanctioned set for this caller (four-eyes already applied);
// `already_depended_on_by` is bridges-only and is [] for kinds with no bridge anchor to count.
export interface GovernanceQueueItem {
  kind: string
  fact_key: string
  catalogs: string[]
  subject: string
  state: string
  state_code: string
  production_eligibility: string | null
  production_eligibility_code: string
  available_actions: string[]
  detail: Record<string, unknown>
  already_depended_on_by: GovernanceQueueUsage[]
}

// A listing or store that could not be READ. Reported so an empty queue and a broken one are
// never the same answer: items [] with complete true means "nothing is waiting".
export interface GovernanceQueueUnreadable {
  listing: string
  source: string | null
  reason: string
}

export interface GovernanceQueue {
  items: GovernanceQueueItem[]
  // The catalogs this caller may see — the exact set the queue was built over, and the ONLY
  // source of the catalog filter chips. There is NO display name anywhere in this system
  // (graph_node carries no label): a UI may upper-case the slug, never invent prose.
  catalogs: string[]
  // SCOPE-RELATIVE by construction — two operators legitimately see different numbers for the
  // same catalog, so neither map is ever a catalog total.
  items_visible_to_you_by_catalog: Record<string, number>
  items_visible_to_you_by_kind: Record<string, number>
  unreadable: GovernanceQueueUnreadable[]
  complete: boolean
  truncated: boolean
  counts_are_scope_relative: boolean
  next_cursor: string | null
}

export function getGovernanceQueue(limit = 100): Promise<GovernanceQueue> {
  return request(`/governance/queue?limit=${limit}`)
}

// ---- relationship readiness (read-only visibility over the governance outcomes) --------------
// The per-table diagnostic behind the two queues above: one row per table with the precedence-
// folded status (conflicting > confirmed > candidate_proposed > weak_candidates_only >
// no_candidates) plus the DISJOINT pair lists (each pair rendered "lo <-> hi", listed once under
// its own highest category). Pure read — confirmation stays on the governance endpoints.

export interface RelationshipReadiness {
  scope: string
  source: string
  schema: string
  table: string
  status: 'no_candidates' | 'candidate_proposed' | 'weak_candidates_only' | 'confirmed'
    | 'conflicting'
  confirmed_pairs: string[]
  proposed_pairs: string[]
  weak_pairs: string[]
  conflicting_pairs: string[]
}

export function listRelationshipReadiness(
  source: string,
): Promise<{ source: string; relationships: RelationshipReadiness[] }> {
  return request(`/sources/${encodeURIComponent(source)}/readiness/relationships`)
}

// ---- governance dashboard (read-only rollups over the recorded governance outcomes) ----------
// Phase 4 observability: per-fact-type counts by folded status, queue health, the calibration
// SEED (an observation of signal vs. outcome — nothing here changes scoring), and recent
// activity. The cross-source route also carries a per-source summary list. Pure reads; an
// unknown source answers an all-zeros dashboard, never a 404.

export interface FactTypeRollup {
  fact_type: string
  pending: number
  confirmed: number
  rejected: number
  needs_attention: number
  rejected_by_category: Record<string, number>
}

// One source's roll-up row on the cross-source dashboard (the scoping entry point).
export interface SourceGovernanceSummary {
  source: string
  pending: number
  confirmed: number
  rejected: number
  oldest_pending_age_seconds: number | null
}

export interface GovernanceDashboard {
  scope: string
  source: string | null
  generated_at: string
  fact_types: FactTypeRollup[]
  queue_health: {
    open_depth: number
    oldest_pending_age_seconds: number | null
    age_buckets: Record<string, number>
  }
  calibration_seed: {
    confirm_rate_by_bucket: Record<
      string,
      { confirmed: number; rejected: number; rate: number | null }
    >
    reject_category_by_top_signal: Record<string, Record<string, number>>
  }
  recent_activity: { days: number; confirmed: number; rejected: number }
  // Present on the cross-source route only; the single-source route omits it.
  sources?: SourceGovernanceSummary[]
}

export function getGovernanceDashboard(): Promise<GovernanceDashboard> {
  return request('/governance/dashboard')
}

export function getSourceGovernanceDashboard(source: string): Promise<GovernanceDashboard> {
  return request(`/sources/${encodeURIComponent(source)}/governance/dashboard`)
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
  // Declared type from graph_node, so a card can state what a column IS as well as what it means.
  data_type?: string
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
// `cardinality` is omitted for advisory/link-only entity edges and populated from a current
// directional realization when one has actually been evaluated.
// kind 'contains' (table -> column) is structural and always emitted regardless of layers.
export interface LineageEdge {
  from: string
  to: string
  layer: LineageLayer
  kind: 'contains' | 'join' | 'entity_bridge' | 'derives' | 'consumes'
  cardinality?: string
  // Non-bridge edges retain the original endpoint-resolution flag.
  resolved?: boolean
  // Entity bridges expose the independent truths explicitly. `trust_kind` is
  // advisory_lineage | governed_identifier_link | executable_realization.
  trust_kind?: string
  endpoint_resolved?: boolean
  link_review_status?: string
  realization_safety_status?: string
  execution_eligible?: boolean
  // entity_bridge only: WHICH entity the two columns share (customer, branch, …). Carried on the
  // edge because the node-level `entity` field is null on every column.
  entity_id?: string
  // entity_bridge only: ranking, not permission. A weak link is drawn faintly, never hidden.
  strength?: number
  why?: string
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

// ---- Governed feature-contract flow (the two-gate flow: brief -> considered set -> confirm) --------
// The backend flow is stateless over HTTP: the client carries intent_id + the transient draft between
// steps, and the server re-validates (MCV) at draft and confirm, so a tampered payload can never govern.
// Reuses FeatureIdea / FeatureSet / SetRecommendation / Rejection (defined above) — considered-set is a
// superset of recommend-sets, so its alternatives + rejections are the same shapes the Workbench renders.
// Phase 2A — one recipe's two ranking projections. Present on a scoped response ONLY when the
// backend's FEATUREGEN_INTENT_RANKING flag is on (additive; the flag-off scoped response is
// byte-identical to Phase 1B). `canonical_rank` is a dense, 1-based presentation priority — never a
// predictive-utility claim. `selected_for_initial_view` is a SEPARATE projection (the initial-view
// subset); diversity affects it ONLY and never rewrites `canonical_rank`. The two reason streams stay
// distinct: `rank_reasons` (positive AND negative codes) explains the canonical position;
// `initial_view_reasons` explains initial-view membership (why a non-initial recipe was held back).
// Codes are stable enum tokens the FRONTEND maps to display text — never render backend text here.
export interface RankedRecipe {
  recipe_id: string
  canonical_rank: number
  selected_for_initial_view: boolean
  rank_reasons: string[]
  initial_view_reasons: string[]
}

export interface ConsideredSetResp {
  intent_id: string
  anchor: FeatureIdea | null
  alternatives: FeatureSet[]
  recommendation: SetRecommendation | null
  rejections: Rejection[]
  // Phase 1B — present ONLY on a scoped response (the caller sent a confirmed_scope). The run this
  // considered set was minted under, the governing scope, how many recipes were in scope (from
  // applicability, not recognition), and the per-recipe disposition lens.
  generation_run_id?: string
  scope_id?: string
  in_scope_count?: number
  dispositions?: RecipeDisposition[]
  // Phase 2A — deterministic presentation-priority ranking of the ELIGIBLE recipes, present ONLY
  // when the backend ranking flag is on. Distinct from `recommendation` (the LLM starting-set pick)
  // and from `dispositions` (the per-recipe lens). `ranking_version` stamps the mapping/taxonomy
  // version the ranking was computed under (provenance; a bump never mutates a prior projection).
  ranking?: RankedRecipe[]
  ranking_version?: string
  // Phase-2B — per-recipe SOFT-dimension signal warnings, present ONLY when the ranking flag is on.
  // Maps a recipe_id to its warning codes (e.g. `entity_grain_mismatch` / `modelling_context_conflict`).
  // Presentation-only: a warning NEVER rejects a recipe or changes its disposition — it is a nudge the
  // ranker already applied plus a badge the human sees. The FRONTEND maps each code to display text.
  signal_warnings?: Record<string, string[]>
}

export interface ContractDraft {
  feature_name: string
  definition: string
  grain_table: string | null
  aggregation: string | null
  as_of_column: string | null
  derives_from: string[]
  target_ref: string | null
  derives_pairs: [string, string][]
  join_path: Record<string, unknown>[]
}

export interface DraftResp {
  draft: ContractDraft
  unresolved: unknown[]
  intent_id: string
  choice_id: string | null
  snapshot?: {
    generation_run_id: string
    snapshot_id: string | null
    content_hash: string | null
  } | null
}

export interface Contract {
  contract_id: string
  feature_id: string
  feature_name: string
  version: number
}

export interface ContractSummary {
  contract_id: string
  feature_id: string
  feature_name: string
  version: number
  verification: string
  created_at: string
}

export interface ContractDetail extends ContractSummary {
  definition: string
  intent_id: string | null
}

// ---- Phase 1B: scoped grounding (recognition → human confirmation → scoped considered set) ------
// One recognised use-case the recognizer proposed for the objective. `relationship` is the
// recognizer's role for it (the primary use-case vs a secondary one); `confidence` and the
// `evidence_spans` (verbatim phrases from the hypothesis/objective) justify the proposal to the
// human at Gate #1. Recognition NEVER sees catalog columns — this is use-case reasoning only.
export interface RecognitionCandidate {
  use_case_id: string
  display_name: string
  relationship: 'primary' | 'secondary'
  confidence: 'high' | 'medium' | 'low'
  evidence_spans: string[]
}

// POST /contract/recognitions result. `status` is the recognizer's verdict; `unscoped` (fail-open)
// means it could not scope the objective, so generation should ground everything. Carries NO
// generation_run_id and NO recipe count: recognition precedes generation, and applicability owns
// any recipe count (computed later, on the considered-set call).
export interface RecognitionResp {
  intent_id: string
  recognition_id: string
  status: 'classified' | 'ambiguous' | 'unscoped' | 'technical_failure'
  unscoped: boolean
  candidates: RecognitionCandidate[]
  // Phase-2B SOFT intent dimensions the recognizer proposed (additive; empty/null when none). NEVER
  // a rejection — the human confirms/overrides them at Gate #1 and they act as ranking nudges only.
  // `modelling_contexts` are governed context ids; `target_entity` is the proposed prediction grain;
  // `warnings` are the recognizer's non-fatal per-dimension notes (a value it could not map).
  modelling_contexts: string[]
  target_entity: string | null
  warnings: string[]
}

// The human's confirmed Gate #1 scope, in the shape the UI holds it (camelCase). `primary` /
// `secondary` are use-case ids; `expansion` maps the "include all sub-use-cases?" toggle
// (exact ↔ include_descendants); `unscoped` true is a BROADEN (ground all buildable recipes);
// Provenance is derived by the server from the immutable recognition and authenticated action.
export interface ConfirmedScopeInput {
  primary: string | null
  secondary: string[]
  expansion: 'exact' | 'include_descendants'
  unscoped: boolean
  // Phase-2B SOFT dimensions the human confirmed/overrode: governed modelling context ids and the
  // proposed prediction grain. They flow into the scoped considered-set as ranking nudges (never a
  // scope-narrowing filter). `targetEntity` is null when the human proposed/kept no grain.
  modellingContexts: string[]
  targetEntity: string | null
}

// One stage evaluation on a recipe's disposition. `reason_codes` carry the WHY the UI renders;
// `evaluation_version` / `evaluated_at` stamp the mapping/taxonomy version and server clock for
// replay. An out-of-scope recipe leaves downstream stages NOT_EVALUATED (never a bare null).
export interface DispositionStage {
  status: string
  reason_codes: string[]
  evaluation_version?: string
  evaluated_at?: string
}

// One recipe's final disposition, computed once from the ApplicabilityResult + grounding + safety.
// The lens groups recipes by `final_disposition`; `relevance_tier` is the applicability role for an
// eligible recipe (primary/supporting), null for a recipe that never reached grounding.
export interface RecipeDisposition {
  recipe_id: string
  final_disposition:
    | 'eligible'
    | 'unbuildable'
    | 'grounding_incomplete'
    | 'safety_rejected'
    | 'out_of_scope'
  relevance_tier: 'primary' | 'supporting' | null
  applicability: DispositionStage
  grounding: DispositionStage
  safety: DispositionStage
}

// Run the recognizer over the objective and persist an append-only attempt (no generation run yet).
// Feedback recognition binds the bounded instruction to the prior confirmed scope. A recognizer
// failure comes back as status 'technical_failure'; generation still requires a human action.
export function contractRecognitions(
  hypothesis: string,
  objective: string,
  opts: { feedback?: string; supersedesScopeId?: string } = {},
): Promise<RecognitionResp> {
  return post('/contract/recognitions', {
    hypothesis,
    objective,
    feedback: opts.feedback ?? null,
    supersedes_scope_id: opts.supersedesScopeId ?? null,
  })
}

// The mandatory read's DRAFT ticket (intake build, router-quality plan 2026-08-10). Never a
// decision: `pinned` means code matched a name the user literally typed (recorded server-side
// without a click); a fuzzy `target_column` is a model reading awaiting the confirm screen;
// `contradiction` is the warning when the prose disagreed with a typed name.
export interface IntakeTicket {
  target_column: string | null
  target_window_days: number | null
  target_type: 'binary_classification' | 'regression' | 'multiclass' | 'abstain'
  business_domain: string[]
  confidence: 'high' | 'medium' | 'abstain'
  pinned: boolean
  contradiction: string | null
}

export interface IntakeResp {
  intent_id: string
  // extracted = fresh model call; replayed = cached (free); unavailable/call_ceiling = degraded —
  // the pinned target (pure code) still lands, everything else honestly abstains.
  reason: 'extracted' | 'replayed' | 'unavailable' | 'call_ceiling'
  ticket: IntakeTicket
  // The confirm screen's one-liner: "I understood your target as `ref` — <ai_summary>".
  target_detail: {
    ref: string; catalog_source: string; concept: string; ai_summary: string
  } | null
}

// One hypothesis in, one draft reading out. Cached server-side by content (hypothesis + shortlist
// + vocabulary + prompt version), so re-asking the same question is free.
export function contractIntake(
  hypothesis: string,
  opts: { catalogSource?: string } = {},
): Promise<IntakeResp> {
  return post('/contract/intake', {
    hypothesis,
    catalog_source: opts.catalogSource ?? null,
  })
}

// The recorded reading after the human's answer — provenance is the audit fact: 'human_confirmed'
// (a person clicked), 'user_typed' (they literally named it), 'exploring' (explicit no-target).
export interface IntakeReading {
  intent_id: string
  target_ref: string | null
  target_window_days: number | null
  target_type: string | null
  business_domain: string[]
  target_provenance: string | null
  target_confirmed_by: string | null
}

// Record the human's answer to the confirm screen. Author-only (403 otherwise); the signed ref is
// validated against the read-scoped catalog server-side — a column you cannot see cannot be your
// target; off-vocabulary domain tokens are refused, never silently dropped.
export function contractIntakeTarget(
  intentId: string,
  decision: 'confirmed' | 'corrected' | 'exploring',
  opts: {
    targetRef?: string; targetWindowDays?: number; targetType?: string
    businessDomain?: string[]; catalogSource?: string
  } = {},
): Promise<IntakeReading> {
  return post('/contract/intake/target', {
    intent_id: intentId,
    decision,
    target_ref: opts.targetRef ?? null,
    target_window_days: opts.targetWindowDays ?? null,
    target_type: opts.targetType ?? null,
    business_domain: opts.businessDomain ?? [],
    catalog_source: opts.catalogSource ?? null,
  })
}

// Gate #1 intake: mandatory hypothesis + objective; the server persists the intent and returns the
// gauntlet-validated considered set (anchor + generated alternatives + an advisory recommendation).
// Phase 1B: when `confirmedScope` is supplied (the human confirmed/broadened the recognised scope),
// the server ALSO mints a generation run, persists the scope, grounds only the in-scope recipe
// subset, and attaches per-recipe `dispositions` + an `in_scope_count`. In release mode the backend
// rejects an absent scope; omission exists only for the explicitly configured emergency legacy mode.
export function contractConsideredSet(
  hypothesis: string,
  objective: string,
  opts: {
    definition?: string; catalogSource?: string; entity?: string; targetRef?: string
    feedback?: string
    intentId?: string; recognitionId?: string
    confirmedScope?: ConfirmedScopeInput
    supersedesScopeId?: string
  } = {},
): Promise<ConsideredSetResp> {
  return post('/contract/considered-set', {
    hypothesis,
    objective,
    definition: opts.definition ?? '',
    catalog_source: opts.catalogSource ?? null,
    entity: opts.entity ?? null,
    target_ref: opts.targetRef ?? null,
    // HUMAN guidance for a whole-round feedback re-run; mints a FRESH governing intent over the
    // guided set. null on the initial generate (no feedback yet).
    feedback: opts.feedback ?? null,
    // Scoped-grounding fields. Null is only valid against an explicitly configured legacy backend.
    intent_id: opts.intentId ?? null,
    recognition_id: opts.recognitionId ?? null,
    confirmed_scope: opts.confirmedScope
      ? {
          primary: opts.confirmedScope.primary,
          secondary: opts.confirmedScope.secondary,
          expansion: opts.confirmedScope.expansion,
          unscoped: opts.confirmedScope.unscoped,
          modelling_contexts: opts.confirmedScope.modellingContexts,
          target_entity: opts.confirmedScope.targetEntity,
        }
      : null,
    // Lineage/history only for a broaden: the prior scope this run supersedes. Never used to
    // derive the governing scope (that is generation_run → scope_id).
    supersedes_scope_id: opts.supersedesScopeId ?? null,
  })
}

// Record the human's Gate #1 choice (server reconstructs the feature from the persisted set) and author
// the draft. In confirmation-required mode chosen_option_id is the opaque option_id, never display name.
export function contractDraft(
  intentId: string,
  chosenSource: 'anchor' | 'alternative',
  chosenOptionId: string,
  why = '',
  expectedGenerationRunId?: string,
): Promise<DraftResp> {
  return post('/contract/draft', {
    intent_id: intentId,
    chosen_source: chosenSource,
    chosen_option_id: chosenOptionId,
    why,
    expected_generation_run_id: expectedGenerationRunId ?? null,
  })
}

// Gate #2 — the governing write. The draft (from contractDraft) is sent back with its intent_id; the
// server re-runs the MCV and mints a versioned, DESIGN-CHECKED contract.
export function contractConfirm(
  draft: ContractDraft,
  intentId: string,
  choiceId?: string | null,
): Promise<Contract> {
  return post('/contract/confirm', {
    ...draft,
    intent_id: intentId,
    choice_id: choiceId ?? null,
  })
}

export function listContracts(limit = 50): Promise<ContractSummary[]> {
  return request(`/contracts?limit=${limit}`)
}

export function getContract(contractId: string): Promise<ContractDetail> {
  return request(`/contracts/${encodeURIComponent(contractId)}`)
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

// Two or more DISTINCT upstream tables (different fullyQualifiedNames) that fold to the SAME
// catalog table name under the sync's table naming. Held OUT of the pull (fail-closed — the
// connector never silently merges distinct sources); the preview must show the exclusion.
export interface FoldCollision {
  table: string
  fqns: string[]
}

// A FOREIGN_KEY relationship the translation cannot carry (composite FK, or a second FK on a
// column that already carries one). The join is dropped on import; the preview must show the loss.
export interface DroppedJoin {
  table: string
  columns: string[]
  referred: string[]
  reason: string
}

// The dry run a human approves. `snapshot_hash` and `local_baseline_hash` are the honesty
// anchors: import must present BOTH back, and the server answers 409 if OpenMetadata moved
// (snapshot) or the local catalog for the source changed (baseline) since this preview was
// taken. The tag map shown here is the EFFECTIVE map: integration.tag_map merged with the
// sync's override.
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
  // Known data loss (#1): tables held out by folded-name collisions and FK relationships the
  // translation drops. Always present in build_preview's JSON; both empty on a clean pull.
  collisions: FoldCollision[]
  dropped_joins: DroppedJoin[]
  brake: { would_hold: boolean; reason: string | null }
  as_of_suggestions: AsOfSuggestion[]
  snapshot_hash: string
  local_baseline_hash: string
}

export function previewSync(syncId: string): Promise<SyncPreview> {
  // No body: the sync and its integration carry the URL, token, scope, and effective tag map.
  return request(`/syncs/${encodeURIComponent(syncId)}/preview`, { method: 'POST' })
}

// Import wraps the standard IngestResult (same pipeline, same shape) with the audit record id
// and `semantics_pending`: an informational COUNT of landed columns awaiting a data owner's
// semantics confirmation. It is NOT a queue — the import creates no review records for pending
// semantics. Quarantined rows (inside result) are the only items routed to a real review queue.
export interface SyncImportResult {
  result: IngestResult
  import_id: string
  semantics_pending: number
}

export async function importSync(
  syncId: string,
  snapshotHash: string,
  localBaselineHash: string,
): Promise<SyncImportResult> {
  const { body, response } = await requestWithResponse<SyncImportResult>(
    `/syncs/${encodeURIComponent(syncId)}/import`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        snapshot_hash: snapshotHash,
        local_baseline_hash: localBaselineHash,
      }),
    },
  )
  // The run id rides the inner IngestResult so the shared callout reads it from either vehicle.
  return {
    ...body,
    result: { ...body.result, ingestion_run_id: response.headers.get('X-Ingestion-Run-Id') },
  }
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

// ---- gate operationalization (Phase 3C.1, authority-only console) ---------------------------
// Read-only evaluation triggers over the persisted shadow stores. The request body carries ONLY
// the batch identifier (cohort + window) — every count and verdict is assembled server-side;
// the client never sends numbers and there is no sign/approve call on this surface.

// One shadow-dispatch cohort (a producer commit) with the window its runs span.
export interface GateCohort {
  cohort: string
  first_run_at: string
  last_run_at: string
  run_count: number
}

// The machine verdict: overall PASS/FAIL plus the per-condition booleans behind it.
export interface GateVerdict {
  passed: boolean
  gate1_capture: boolean
  gate2a_map: boolean
  gate3_gold: boolean
  gate5_stability: boolean
  gate6_drift: boolean
}

export interface GateEvaluation {
  verdict: GateVerdict
  // Human-readable failed conditions; empty when every gate passed.
  reasons: string[]
  // Always true from the server: a machine PASS never authorizes go-live by itself.
  necessary_not_sufficient: boolean
  // Which dispatched runs qualified for the window, and why the rest were excluded (fail-closed).
  coverage: { dispatched_in_range: number; qualifying: number; excluded: Record<string, number> }
  population: {
    denominator: number
    numerator: number
    headline_by_primary: Record<string, number>
    breakdown_by_category: Record<string, number>
    // "planner_outcome|compile_status" -> count
    recipe_outcome_matrix: Record<string, number>
  }
  versions: { evaluator: string; cohort: string }
}

export function listGateCohorts(): Promise<GateCohort[]> {
  return request('/gate/cohorts')
}

export function evaluateGate(body: {
  cohort: string
  since: string
  until: string
}): Promise<GateEvaluation> {
  return post('/gate/evaluate', body)
}

// ---- asset detail read model (Delivery F/G) + field-correction command ------------------------
// GET /catalog/assets/{source}/{object_ref:path} returns the bounded sections about ONE catalog
// asset (identity + effective_metadata + evidence + relationships + readiness + history + actions
// + audit), assembled under ONE REPEATABLE READ snapshot. The ETag header carries that snapshot's
// consistency_token — the OCC token a client revalidates against. POST .../fields/{field}/decisions
// is the generic scalar field-correction command (confirm_existing | propose_override |
// confirm_override | reject) with a CAS triple; a CAS conflict fails closed with HTTP 409, which the
// shared error path surfaces as a catchable ApiError(409) so the caller can reload the fresh CAS and
// retry.
//
// object_ref rides a {object_ref:path} route: it is dotted (public.accounts.balance) and MAY be
// pathful (schema/table.col). Encode each SLASH-separated segment (dots survive, real path slashes
// stay separators, and a hostile char like #/space/% is percent-encoded) — never encode the whole
// ref, which would double-encode a legitimate path slash to %2F and 404 the path route.
//
// Every section but identity is selectable via `include`, so each is OPTIONAL on the response; the
// no-include default builds them all. version/source/object_ref/kind + the *_sections lists +
// consistency_token are ALWAYS present.

export type AssetKind = 'table' | 'column'

// The physical + logical identity from the anchor graph_node row (ALWAYS built).
export interface AssetIdentity {
  graph_ref: string
  object_ref: string
  logical_ref: string
  source: string
  kind: AssetKind
  schema_name: string | null
  table: string | null
  column: string | null
  operational_type: string | null   // the numeric-usable operational data_type
  declared_type: string | null      // the source-declared SQL type (non-operational)
  is_grain: boolean
  is_as_of: boolean
}

// One display field + its C1 authority/provenance. `authority` is governed | hint | missing.
export interface EffectiveMetadataField {
  value: string | null
  authority: string
  c1_status: string
  provenance: string | null
  evidence_provenance: string | null
  selected_evidence_ids: string[]
  // The newest ACTIVE evidence's value (Task 3C): what the screen renders — with the author chip
  // and an "unconfirmed" marker — when `value` is null. The display value always wins when present.
  // Optional: an older backend omits it.
  proposed_value?: string | null
  // Type field only: which basis the displayed type stands on — 'operational' (a technical/upload
  // type) or 'declared' (the source file's SQL type, shown instead of a bare "unknown"), or null
  // when nothing at all is held.
  basis?: string | null
}

// One field the SOURCE FILE itself asserted (the "From the source glossary" dossier section):
// the value plus its provenance label ("source attested" / "source proposed"). A field the upload
// never declared is simply absent — the section is empty, never fabricated.
export interface SourceGlossaryField {
  value: string
  provenance: string
}

export interface SourceGlossarySection {
  fields: Record<string, SourceGlossaryField>
}

// A column asset carries a field per label (concept/definition/domain/additivity/unit/currency/
// entity/type); a table asset carries an empty `fields` plus a `note` (no per-field metadata).
export interface EffectiveMetadataSection {
  fields: Record<string, EffectiveMetadataField>
  note?: string
}

// One per-field proposal, bucketed by lifecycle.
export interface EvidenceProposal {
  evidence_id: string
  producer: string
  strength: string
  proposed_value: string | null
  confidence_band: string | null
}

// A field's latest decision head.
export interface LatestFieldDecision {
  decision_event_id: string
  event_type: string
  conflict_status: string | null
  load_bearing: boolean
  decided_at: string
}

export interface EvidenceSection {
  // Keyed by field_name; the inner map is keyed by lifecycle (active/stale/rejected/superseded +
  // any other lifecycle a newer backend emits — kept an open string key so it renders, not breaks).
  proposals_by_field: Record<string, Record<string, EvidenceProposal[]>>
  latest_decision_by_field: Record<string, LatestFieldDecision>
}

// ---- relationships subsections ----
export interface ContainmentColumn {
  object_ref: string
  column: string | null
  data_type: string | null
  sensitivity: string | null
}

export interface Containment {
  table: { object_ref: string; table: string }
  columns: ContainmentColumn[]   // sibling columns (column anchor) or child columns (table anchor)
}

export interface AssetApprovedJoin {
  from_ref: string
  to_ref: string
  cardinality: string | null
  status: string
  approved_join_fact_key: string | null
}

// A VERIFIED entity assignment on the anchor column.
export interface SemanticEntityEdge {
  kind: 'entity_assignment'
  status: string
  object_ref: string
  entity: string
  fact_key: string | null
  confirmed_event_id: string | null
  available_actions: string[]
}

// A VERIFIED column semantic edge (e.g. a currency binding) touching the anchor. A FIXED-CURRENCY
// binding has NO target column: `to_ref` is null and `currency_code` carries the governed ISO
// literal (e.g. a `counter_party_amt_aed` measure bound to 'AED').
export interface SemanticColumnEdge {
  kind: string   // the binding kind (never 'entity_assignment')
  status: string
  from_ref: string
  to_ref: string | null
  currency_code: string | null
  fact_key: string
  confirmed_event_id: string | null
  available_actions: string[]
}

// Narrow with `edge.kind === 'entity_assignment'` (or `'object_ref' in edge`) to the entity arm.
export type SemanticVerifiedEdge = SemanticEntityEdge | SemanticColumnEdge

export interface SemanticCandidate {
  candidate_id: string
  binding_kind: string
  disposition: string
  reason_codes: string[]
  subject_graph_ref: string
  target_graph_ref: string
  proposed_value: string | null
  fact_key: string | null
  fact_status: string | null
  available_actions: string[]
}

export interface SemanticDivergence {
  kind: string   // e.g. entity_divergence
  object_ref: string
  declared_entity: string
  governed_entity: string
  fact_key: string | null
}

// The SEMANTIC subsection is a UNION so the UI renders "unavailable" honestly: a caller lacking
// catalog:read gets {status:'unavailable'} (also named in unavailable_sections), never an empty-
// success that reads as "no semantic links". Everyone else gets a real (possibly empty) available
// subsection; a table anchor is always available-but-empty (bindings attach to columns).
export type SemanticSubsection =
  | { status: 'unavailable' }
  | {
      status: 'available'
      verified_edges: SemanticVerifiedEdge[]
      candidates: SemanticCandidate[]
      divergences: SemanticDivergence[]
    }

// One cross-catalog link: the SAME business entity in another catalog. Present whether or not a
// human has confirmed it — confirmation ANNOTATES, it does not gate visibility or consumption.
export interface CrossCatalogLink {
  entity_id: string
  left_catalog_source: string
  left_object_ref: string
  right_catalog_source: string
  right_object_ref: string
  status: 'confirmed' | 'proposed'
  // Ranking, not permission. Confirmation dominates, then a grain on either side, then an attested
  // type match. A weak link is ranked down, never hidden.
  strength: number
  data_type_family: string
  left_is_grain: boolean
  right_is_grain: boolean
  type_basis: string
  fact_key: string | null
  // The ranking in words, for someone deciding whether to trust the link.
  why: string
}

export interface Relationships {
  containment: Containment
  approved_joins: AssetApprovedJoin[]
  semantic: SemanticSubsection
  cross_catalog: CrossCatalogLink[]
}

// ---- readiness section ----
export interface ColumnRequirement {
  requirement_id: string
  status: 'confirmed' | 'proposed' | 'missing' | 'conflicting' | 'review'
  blocking: boolean
  authority: string
  c1_status: string | null
  evidence_ids: string[]
  fact_event_id: string | null
  decision_event_id: string | null
  external_preview: boolean
  reason: string
}

export interface ColumnCapability {
  use: string
  operational_status: 'ready' | 'blocked' | 'unavailable'
  requirements: ColumnRequirement[]
}

// The per-column capability MATRIX (five independent capabilities); null for a table asset.
export interface ColumnCapabilities {
  source: string
  object_ref: string
  logical_ref: string
  as_measure: ColumnCapability
  as_entity_key: ColumnCapability
  as_event_time: ColumnCapability
  as_grain_key: ColumnCapability
  as_join_key: ColumnCapability
}

export interface ReadinessRequirement {
  requirement_id: string
  scope: string
  status: 'confirmed' | 'proposed' | 'missing' | 'conflicting'
  blocking: boolean
  cause: string
  authority_required: string
}

// What a column can be USED for, in the language of someone deciding whether to build a feature
// from it. `not_considered` is distinct from `needs_data_check`: the former means no candidate
// exists, while the latter means a real candidate exists and is waiting on evidence.
export type Usability =
  | 'confirmed' | 'ai_proposed' | 'needs_data_check' | 'not_set' | 'not_considered'
  | 'not_suitable' | 'unavailable'

export interface RoleUsability {
  role: string
  label: string
  state: Usability
  headline: string
  detail: string
  action: string | null
  // The raw requirement ids behind the verdict — for the disclosure, never the headline.
  outstanding: string[]
  data_checks: string[]
}

export interface ColumnUsability {
  object_ref: string
  roles: RoleUsability[]
  usable_roles: number
  total_roles: number
  headline: string
}

// The parent table as COUNTS plus a sentence — never the rows. Shipping the diagnostic whole meant
// 341 blocking plus 445 review rows on every column page to say one thing. The full lists keep
// their own route: GET /sources/{source}/readiness?subset={table}.
export interface TableRollup {
  table: string
  headline: string
  columns_unreviewed: number
  columns_needing_decision: number
  requirements_total: number
  dominant_cause: string | null
  dominant_cause_plain: string
  columns_outstanding: number
}

export interface ReadinessSection {
  column_capabilities: ColumnCapabilities | null
  usability: ColumnUsability | null
  table_rollup: TableRollup
}

// ---- history section ----
export interface AssetHistoryStage {
  stage: string
  attempt: number
  state: string
  reason_code: string | null
}

export interface AssetHistoryRun {
  ingestion_run_id: string
  relation: string
  at: string
  status: string
  origin_type: string
  started_at: string | null
  completed_at: string | null
  stages: AssetHistoryStage[]
}

export interface HistorySection {
  runs: AssetHistoryRun[]
  truncated: boolean
}

// ---- audit section (separately gated by audit:read; ABSENT + named in unavailable_sections
// otherwise — no hidden count). SAFE summaries only: never a redacted_input or raw model output.
export interface AuditSummary {
  dispatch_ref: string
  task: string
  stage: string
  provider: string
  model: string
  prompt_version: string
  schema_version: string
  created_at: string
  field_names: string[] | null
  outcome: string | null
  outcome_at: string | null
}

export interface AuditSection {
  status: 'available'
  summaries: AuditSummary[]
  truncated: boolean
}

/** One suggested addition to the concept vocabulary — for human review, never auto-applied. */
export interface OntologyGapSuggestion {
  proposed_label: string
  parent_concept: string | null
  definition: string
  aliases: string[]
}

/**
 * The current semantic adjudication of one column. `confidence_band` is EXPLANATION for the
 * reader, never authority: the adjudicated concept is llm/proposed evidence like any other, and
 * its authority is shown by the evidence/effective_metadata sections.
 */
export interface SemanticAdjudicationSection {
  status: 'available' | 'absent'
  note?: string
  structured_result_id?: string
  selected_concept?: string
  alternatives?: string[]
  confidence_band?: 'high' | 'medium' | 'low'
  reason_codes?: string[]
  missing_context?: string[]
  ontology_gap?: OntologyGapSuggestion | null
}

// ---- Context Graph V1 (semantic Task 7) ------------------------------------------------------
// A composition of readers that already shipped — the semantic bundle, the lineage builder, the
// assembled dataset profile and the current adjudication — served as ONE dossier section so every
// fact belongs to the snapshot the consistency_token fingerprints.

export interface ContextValue {
  field: string
  value: unknown
  // The LLM's own answer where it did NOT win resolution — the normal state for a field whose
  // policy excludes the model (`_MEASURE_ANNOTATION`: unit/currency), where `graph_node` never
  // receives the proposal at all. Carried BESIDE `value`, never folded into it: `value` is what the
  // operational read model resolved, and a reader that cannot tell the two apart is how a guess
  // clears a safety check. Optional: an older backend omits the key entirely.
  proposed_value?: unknown
  resolution_status: string
  operational_influence: string | null
  // The DERIVED D2 display label (source_attested | source_proposed | human | llm_proposed |
  // deterministic | governed | system). For the chip only — never branch on it; the triple below
  // is the real authority.
  authority_label: string
  producer: string | null
  strength: string | null
  lifecycle: string | null
  evidence_ids: string[]
}

export interface ContextNode {
  id: string
  kind: string
  label: string
  detail: Record<string, unknown>
}

export interface ContextEdge {
  from: string
  to: string
  kind: string
  // 'structural' for containment (an explicit basis, empty evidence), else the D2 display label
  // or the edge layer's own authority word.
  authority: string
  status: string | null
  why: string
  producer: string | null
  strength: string | null
  lifecycle: string | null
  current: boolean
  evidence_ids: string[]
}

export interface ContextRealization {
  realization_revision_id: string
  from_ref: string
  to_ref: string
  lifecycle: string
  safety_status: string
  // Fan-out never travels without its direction (from/to) and applicability scope.
  cardinality: string | null
  scope_id: string | null
  sandbox_eligible: boolean
  // A PURE predicate over the stored record: it labels history, never a live capability.
  production_eligible: boolean
  // The ONLY live-capability answer, from the revalidating reader.
  executable_now: boolean
}

// What a composed measurement of a crosswalk FOUND. Numbers only — verdicts live per direction.
// `caveats` rides on the same object as the counts deliberately: a reader who sees "3 rows, 1:1"
// without "measured over unfiltered history" has been told something true and understood something
// false.
export interface ContextCrosswalkMeasurement {
  observation_revision_id: string
  scope_id: string
  observed_at: string
  as_of: string | null
  method: string
  row_coverage: string
  complete: boolean
  composed_row_count: number
  source_to_target_max_matches: number
  target_to_source_max_matches: number
  mapping_row_count: number
  mapping_temporal_policy_revision_id: string | null
  caveats: string[]
  failures: string[]
}

// One NAMED direction's verdict. The two are SIDES of the definition, never traversal order, and
// they are independent: 1:1 forward with N:1 reverse is ordinary and admits forward only.
export interface ContextCrosswalkDirection {
  direction: string
  safety_status: string
  cardinality: string | null
  sandbox_admissible: boolean
  production_admissible: boolean
  reason_codes: string[]
}

// ONE leg of a crosswalk, as its real owner pinned it. A same-catalog leg resolves through the
// join planner and carries NO fact key or realization revision; a cross-catalog leg resolves
// through one governed bridge realization and carries both. Empty at discovery by contract — a leg
// is pinned by RESOLVING it, which admission does.
export interface ContextCrosswalkLegPin {
  kind: string
  plan_hash: string
  from_dataset_ref: string
  to_dataset_ref: string
  from_binding_revision_id: string
  to_binding_revision_id: string
  read_set_hash: string
  binding_revision_ids?: string[]
  fact_keys?: string[]
  realization_revision_ids?: string[]
  dependency_snapshot_ids?: string[]
  predicate_content_hashes?: string[]
}

// One category's answer to "what already depends on this". Reuses the governance-queue tri-state:
// `count` is null unless `state === 'counted'`, so the type itself makes "unmeasured" impossible to
// render as 0. Never show a number this does not carry.
export interface ContextCrosswalkUsage {
  category: string
  state: 'counted' | 'not_tracked_yet' | 'unreadable'
  count: number | null
  display: string
  store: string
  basis: string
  reason: string
}

// The Release-C crosswalk extension. It says what the crosswalk IS — which mapping dataset, which
// revision, both legs — plus, since Task 11, what a measurement found and what admission concluded
// per direction, and since Task 13 the three-family reading of every reason code, whether this
// DEPLOYMENT enables crosswalk execution at all, what already depends on it, and every pinned
// revision a traversal would carry. `executable_now` is carried explicitly rather than derived
// from `production_admissible`: that predicate labels history, not a live capability.
// `measurement: null` with empty `directions` is "discoverable, unmeasured" — a state, never a
// failure.
export interface ContextCrosswalk {
  definition_id: string
  definition_revision_id: string
  mapping_dataset_ref: string
  source_to_mapping_refs: string[]
  mapping_to_target_refs: string[]
  mapping_temporal_policy_revision_id: string | null
  leg_pins: ContextCrosswalkLegPin[]
  measurement?: ContextCrosswalkMeasurement | null
  directions?: ContextCrosswalkDirection[]
  admission_policy_version?: string | null
  executable_now?: boolean
  // code -> undecided | needs_data_check | structurally_unsuitable. The UI renders the FAMILY;
  // a bare reason code reads as a fault.
  unresolved_families?: Record<string, string>
  // This installation's switch, separate from the evidence. False means every crosswalk is
  // discoverable and structurally non-executable here — a deployment fact, not a verdict.
  execution_enabled?: boolean
  already_depended_on_by?: ContextCrosswalkUsage[]
  pinned_revisions?: Record<string, string>
}

export interface ContextRelationship {
  relationship_ref: string
  kind: string
  // Present only on a crosswalk. A direct bridge and a crosswalk between the same two endpoints
  // are different records answering different questions, and both appear.
  crosswalk?: ContextCrosswalk | null
  left_ref: string
  right_ref: string
  // available | unavailable, and nothing else — availability never encodes safety.
  availability: string
  review_status: string | null
  assessment_revision_id: string | null
  producer: string
  strength: string
  lifecycle: string
  current: boolean
  evidence_ids: string[]
  executable_now: boolean
  realizations: ContextRealization[]
}

export interface ContextProfileField {
  value: string | null
  producer?: string
  strength?: string
  lifecycle?: string
  state: string | null
  unresolved_family: string | null
}

export interface ContextProfiles {
  catalog_profile_revision_id: string | null
  dataset_profile_hash: string | null
  data_role: ContextProfileField | null
  primary_entity: ContextProfileField | null
  authority_role: ContextProfileField | null
  temporal_storage_model: ContextProfileField | null
  missing_context: string[]
}

// Per-kind accounting of what the bounded reads left out. `truncated` keeps its shipped meaning —
// a BUDGET cut — while `omitted` counts everything not returned, so "no joins" and "joins that did
// not fit" are never the same answer.
export interface ContextTruncation {
  truncated: boolean
  omitted: Record<string, number>
}

export interface ContextSection {
  // available (column) | table | projection_unavailable | unavailable. None of these is an error.
  status: string
  version?: string
  anchor_id?: string
  note?: string
  projection?: { code: string; detail: string }
  source_meaning?: ContextValue[]
  resolved_meaning?: ContextValue[]
  table_context?: ContextValue[]
  concept_path?: string[]
  identifier_namespace?: { scheme: string; issuer_scope: string | null; basis: string } | null
  related_columns?: {
    object_ref: string
    column: string
    concept: string | null
    party_role: string | null
  }[]
  relationships?: ContextRelationship[]
  profiles?: ContextProfiles
  uncertainty?: { missing_context: string[]; not_supplied: string[] }
  // Context this platform has no producer for. Rendered as "not supplied", never as zero.
  not_supplied?: string[]
  nodes?: ContextNode[]
  edges?: ContextEdge[]
  truncation?: ContextTruncation
  content_hash?: string
}

export interface AssetDetail {
  version: string
  source: string
  object_ref: string
  kind: AssetKind
  identity: AssetIdentity
  // Selectable sections — OPTIONAL because `include` can build a subset (the default builds all).
  effective_metadata?: EffectiveMetadataSection
  evidence?: EvidenceSection
  relationships?: Relationships
  readiness?: ReadinessSection
  history?: HistorySection
  // What the source file itself said about this term (Task 3C's dossier section).
  source_glossary?: SourceGlossarySection
  // Server-calculated commands the caller may run; F0 keeps this empty.
  actions?: unknown[]
  audit?: AuditSection
  // The adjudicator's reviewable second opinion for a column Pass A could not settle (Task 5).
  // `absent` is the NORMAL state — adjudication is the exception path, not a gap in the data.
  semantic_adjudication?: SemanticAdjudicationSection
  // Context Graph V1 (Task 7). A SECTION, deliberately not its own endpoint: it rides this
  // response's single repeatable-read snapshot and its consistency_token.
  context?: ContextSection
  included_sections: string[]
  unavailable_sections: string[]
  // Whether a load-bearing projection was behind when this dossier was assembled (Task 6). It is
  // INSIDE the fingerprinted body, so a lagged snapshot never shares a consistency_token with a
  // ready one.
  projection?: ProjectionStatus
  // The snapshot fingerprint, echoed as the ETag header (the OCC token).
  consistency_token: string
}

// Encode a {object_ref:path} value: encodeURIComponent EACH slash-separated segment, then rejoin
// with '/'. Dots survive (encodeURIComponent leaves them untouched), a real path slash stays a
// separator, and a hostile char inside a segment (#, space, %) is percent-encoded — without
// double-encoding the path slashes to %2F (which the :path route would then fail to match / 404).
function encodeObjectRefPath(objectRef: string): string {
  return objectRef.split('/').map(encodeURIComponent).join('/')
}

// GET the bounded asset detail. `include` names the sections to build (repeatable; default = all).
// Uses requestWithResponse so the ETag header (the snapshot's consistency token) rides back out:
// RETURNS { detail, etag }. The etag is the OCC token a follow-up read revalidates against; it also
// lives verbatim in detail.consistency_token (the ETag header adds the HTTP quote wrapper).
export async function getAssetDetail(
  source: string,
  objectRef: string,
  include?: readonly string[],
): Promise<{ detail: AssetDetail; etag: string }> {
  let path = `/catalog/assets/${encodeURIComponent(source)}/${encodeObjectRefPath(objectRef)}`
  if (include && include.length > 0) {
    // list[str] Query on the route reads a REPEATED include= param, so append one per section.
    const params = new URLSearchParams()
    for (const section of include) params.append('include', section)
    path += `?${params}`
  }
  const { body, response } = await requestWithResponse<AssetDetail>(path)
  // The ETag header carries the quoted consistency token; fall back to the body token if a proxy
  // stripped the header, so the caller always gets an OCC token to echo.
  const etag = response.headers.get('ETag') ?? body.consistency_token
  return { detail: body, etag }
}

// ---- field-correction command (Delivery F) ----
export type FieldDecisionAction =
  'confirm_existing' | 'propose_override' | 'confirm_override' | 'reject'

// The correction command as the UI holds it (camelCase). The expected_* triple is the CAS anchor the
// field was loaded at (the field's evidence-set + decision head + policy version); ANY drift fails
// closed with 409. expectedLatestDecisionId is null for a field with no decision head yet.
export interface FieldDecisionRequest {
  action: FieldDecisionAction
  selectedEvidenceIds?: string[]
  replacementValue?: string | null
  reason?: string | null
  idempotencyKey: string
  expectedLatestDecisionId: string | null
  expectedEvidenceSetHash: string
  expectedPolicyVersion: string
}

// The command result: the outcome + the NEW CAS anchor (so the client can chain a follow-up command
// without a re-read) + the actions still callable on the field.
export interface FieldDecisionResult {
  field: string
  action: string
  outcome: string   // confirmed | proposed | rejected | replayed
  replayed: boolean
  projected: boolean
  latest_decision_id: string | null
  evidence_set_hash: string
  policy_version: string
  actions: string[]
}

// ---- P4 v1: read-only per-table suggested features ----
// GET-only, by design: the payload is what the deterministic engine can ground on ONE table (no
// hypothesis, no intent, no LLM), and v1 offers no verb to accept, dismiss or govern a suggestion.
// Every field below is the engine's own — statuses are the gauntlet's tri-state, `binding_quality`
// is the signal it already returns. There is no relevance score in this system, so there is none here.
// A registry-typed requirement parameter value. Tuples become lists on the wire, scalars pass
// through; `requirements_to_json` emits `params` ADDITIVELY, only when a requirement has any.
export type RequirementParamValue =
  | string | number | boolean | null | (string | number | boolean | null)[]

export interface SuggestionRequirement {
  code: string            // the closed REQUIREMENT_CODES vocabulary (UNIT_CONSISTENT, ...)
  operand: string[]       // [catalog_source, object_ref] the requirement concerns
  detail: string
  // Additive v2 fields (`contract._serial.requirements_to_json`): present only when the typed
  // requirement carries registry params / a non-default schema version. A v1 body has neither.
  params?: [string, RequirementParamValue][]
  schema_version?: string
}

export interface RecipeParts {
  operation: string
  measures: string[]
  grain: string
  window: string
  time: string
}

export interface FeatureSuggestion {
  name: string
  description: string
  recipe: string
  recipe_parts: RecipeParts
  validation_status: string      // DESIGN_CHECKED | NEEDS_EXTERNAL_VALIDATION
  requirements: SuggestionRequirement[]
  uses: string[]                 // the object_refs it binds
  binding_quality: string
  grain_table: string
}

// entity_label is the ENTITY the features are computed per ('account'); entity_ref is the COLUMN
// that entity is bound to. An empty entity_label = an entity the catalog could not NAME, so no
// heading is rendered for it; an empty entity_ref = no bound entity at all.
export interface SuggestionGroup {
  entity_ref: string
  entity_label: string
  suggestions: FeatureSuggestion[]
}

export interface SuggestionRejection {
  name: string
  reason: string
  code: string
}

// WHAT THE PAGE DID NOT LOOK AT. Suggestions are grounded on the opened table plus the tables joined
// DIRECTLY to it, bounded by a table cap and a column budget — walking the join graph transitively
// has no resource bound on a real catalog, where almost everything reaches the customer/account hub.
// The bound is honest rather than hidden: these five fields say how much of the neighbourhood was
// used, how much exists, whether anything was dropped and which limit dropped it. The counts are
// about NEIGHBOURS — the opened table itself is never truncated away and is not counted here.
export interface JoinNeighbourhood {
  tables_considered: number
  tables_available: number
  truncated: boolean
  // The hop bound that was applied (1 on any automatic page load). Stated even when nothing was
  // truncated: deeper join paths exist and were deliberately not loaded.
  max_hops: number
  // Which bound bit: 'table_cap' | 'column_budget', or null when nothing was left out.
  limit_reason: string | null
}

export interface TableSuggestions {
  catalog_source: string
  table: string
  // false = this catalog holds no such table. An unknown table and a table with no concepts both
  // return zero suggestions, so this is what keeps the empty screen's DIAGNOSIS honest.
  table_known: boolean
  summary: { suggested: number; clean_ready: number; needs_review: number; entities: number }
  groups: SuggestionGroup[]
  rejections: SuggestionRejection[]
  neighbourhood: JoinNeighbourhood
}

// An automatic page load takes the server's capped default (one hop). The route also accepts an
// explicit `?max_hops=` for a deliberate, wider request — no caller passes it yet: choosing WHICH
// deeper join path to follow is a governed act that wants its own picker UI, which is DEFERRED.
export function getTableSuggestions(source: string, table: string): Promise<TableSuggestions> {
  return request(
    `/catalog/${encodeURIComponent(source)}/tables/${encodeURIComponent(table)}/suggestions`,
  )
}

// ---- Release A v2: the per-table DISCOVERY contract (`?contract_version=2`) -------------------
// The same read-only route, asked for its richer payload EXPLICITLY. v1 remains the server default
// for the whole of Release A, so a client that wants v2 says so in the URL — it never sniffs for an
// optional field on a v1 body, which would make "an older deployment" and "a suggestion with no
// category" the same observation.
//
// Everything below mirrors `overlay/upload/suggestion_contract.page_to_json` field-for-field. The
// closed vocabularies are typed as unions because they ARE closed server-side; every renderer still
// falls back for an unknown member rather than crashing on a newer backend.

export type EvidenceProducer =
  | 'source' | 'structural_connector' | 'parser' | 'llm' | 'profiler' | 'taxonomy'
  | 'human' | 'legacy'
export type AssertionStrength = 'proposed' | 'supported' | 'attested' | 'confirmed'
export type EvidenceLifecycle = 'active' | 'stale' | 'rejected' | 'superseded'

// One evidence OCCURRENCE on the real three-axis vocabulary. A value may carry several at once
// (source + LLM + human); collapsing them into one "best authority" is forbidden by the contract,
// so this always travels as a list and the UI renders every member.
export interface EvidenceAuthority {
  producer: EvidenceProducer
  strength: AssertionStrength
  lifecycle: EvidenceLifecycle
  producer_ref: string | null
  evidence_id: string | null
}

// WHERE a value came from, as a kind of authorship. Distinct from `strength`, which is how hard
// its producer asserts it: an `llm_proposed` basis with `proposed` strength is the weakest pair.
export type AttributedBasis = 'template_authored' | 'catalog_resolved' | 'human' | 'llm_proposed'
// Read from governed state, never inferred. null for every Release-A discovery value.
export type OperationalInfluence = 'governed' | 'hint'

// A CONTROLLED registry id with provenance. `id` is the stable audit key; `display_name` is what a
// human reads. Never minted from free text — catalog wording travels as AttributedText below.
export interface AttributedLabel {
  id: string
  display_name: string
  basis: AttributedBasis
  evidence: EvidenceAuthority[]
  operational_influence: OperationalInfluence | null
  source_refs: string[]
}

// Attributed FREE TEXT: displayable and searchable, never a facet id. This is what unmapped
// catalog domain/entity wording arrives as, and the UI must not badge it as a controlled value.
export interface AttributedText {
  value: string
  basis: AttributedBasis
  evidence: EvidenceAuthority[]
  operational_influence: OperationalInfluence | null
  source_refs: string[]
}

// What an operand IS to the computation, read from the engine's typed refs — never guessed from a
// column name, a type or an AI-proposed concept.
export type SuggestionOperandClassification = 'measure' | 'grain' | 'time' | 'grouping' | 'other'

export interface SuggestionOperand {
  catalog_source: string
  logical_ref: string
  graph_object_ref: string
  table_ref: string
  recipe_role: string       // the TEMPLATE AUTHOR's declared slot name; '' when none bound
  classification: SuggestionOperandClassification
  visibility_requires_current: string[]   // non-empty = this input is visibility-restricted
  evidence_refs: string[]
}

// Release A's only `profile_status`: the profile plan has not landed, so the descriptive roles are
// null because nobody has decided them, NOT because the dataset has none. Left as `string` because
// the vocabulary's remaining members are owned by that plan and are not frozen here.
export const PROFILE_STATUS_UNAVAILABLE = 'unavailable'

export interface SuggestionSourceDataset {
  catalog_source: string
  table_ref: string
  data_role: AttributedLabel | null
  authority_role: AttributedLabel | null
  temporal_storage_model: AttributedLabel | null
  primary_entity: AttributedLabel | null
  dataset_profile_hash: string | null
  profile_status: string
}

// One TRAVERSED relationship leg, in the direction travelled.
//
// The last three are LEFT AS `string` ON PURPOSE, and the reason is worth stating because the rest
// of this block narrows aggressively. Each is written by TWO producers that do not agree, and the
// dataclass carrying them validates only `relationship_kind` — so a TS union here would be a claim
// the server does not enforce, and a payload that violated it would type-check as impossible while
// rendering as garbage. The renderer maps every member below to words and degrades unknown members
// gracefully, which is the honest equivalent.
//
//   cardinality   — the join-path walker passes the `graph_edge` column through verbatim, whose DB
//                   CHECK is '1:1' | '1:N' | 'N:1' | NULL; the planner instead emits the taxonomy's
//                   `Cardinality` StrEnum: 'one_to_one' | 'one_to_many' | 'many_to_one' |
//                   'many_to_many'. NULL becomes 'unknown' — never guessed at 1:1. Nothing
//                   normalizes the two notations.
//   safety_status — 'clearing' (governed-verified, or declared with no contradicting fact) or
//                   'unverified'. Closed by call-site enumeration only, not by any validator.
//   review_status — 'file_declared' when nobody confirmed it. Otherwise the approved_join status
//                   column, whose CHECK admits 'DRAFT' | 'PARTIALLY_CONFIRMED' | 'VERIFIED' |
//                   'REJECTED' | 'STALE' | 'REVERIFY' (only VERIFIED is reachable on an
//                   operational edge today), or the planner's own 'governed_bridge' | 'unlinked'.
export interface SuggestionRelationshipDependency {
  relationship_ref: string
  relationship_kind: string
  from_ref: [string, string]
  to_ref: [string, string]
  realization_content_hash: string | null
  cardinality: string
  safety_status: string
  review_status: string
}

// The CLOSED warning vocabulary. Prose renders a code; it is never an alternative decision field.
// Staleness is deliberately absent: it is projection state, not a property of the suggestion.
export const SUGGESTION_WARNING_CODES = [
  'NEAR_LABEL', 'SENSITIVE_INPUT', 'MISSING_TEMPORAL_EVIDENCE', 'MISSING_UNIT', 'MISSING_CURRENCY',
  'RELATIONSHIP_UNCONFIRMED', 'RELATIONSHIP_SAFETY_UNPROVEN',
  'DIRECTIONAL_CARDINALITY_UNAVAILABLE', 'PROFILE_PROPOSED',
] as const
export type SuggestionWarningCode = (typeof SUGGESTION_WARNING_CODES)[number]

export interface SuggestionWarning {
  code: SuggestionWarningCode
  operand_refs: [string, string][]
  detail: string
}

export type SuggestionValidationStatus = 'DESIGN_CHECKED' | 'NEEDS_EXTERNAL_VALIDATION'
// How completely the discovery registry maps this recipe. `needs_sme` is UNREPRESENTABLE in v1 —
// the contract has no carrier for its provenance — so a UI chip for it could never occur.
export type SuggestionDiscoveryDisposition = 'complete' | 'partial' | 'unclassified'
// The server-stamped generation vocabulary. This surface emits only `recipe`.
export type SuggestionGenerationSource = 'recipe' | 'llm_freeform' | 'user_defined'

export interface FeatureSuggestionV2 {
  schema_version: string          // 'feature-suggestion-v2'
  suggestion_id: string           // STABLE logical candidate identity — the React key, not the name
  suggestion_revision_id: string  // exact content/context revision
  generation_source: SuggestionGenerationSource

  template_id: string | null
  recipe_revision_id: string | null
  discovery_metadata_revision_id: string | null
  validation_rule_content_hashes: string[]
  read_scope_rule_content_hashes: string[]
  name: string
  display_name: string
  business_interpretation: AttributedText | null
  business_value: AttributedText | null

  feature_category: AttributedLabel | null
  // The provenance-badge signal a consumer must NOT read off `basis`, which says
  // `template_authored` for a taxonomy-DERIVED category as well as for an authored one.
  feature_category_derived_from_family_mapping: boolean
  discovery_disposition: SuggestionDiscoveryDisposition
  recipe_family: AttributedLabel | null
  business_domains: AttributedLabel[]        // empty in Release A: no controlled resolver
  contextual_domain_terms: AttributedText[]  // catalog wording, NOT a controlled domain
  use_cases: AttributedLabel[]
  keywords: AttributedText[]

  entity: AttributedLabel | null
  contextual_entity_terms: AttributedText[]
  grain_refs: [string, string][]             // ordered composite key
  operation_kind: string
  window: string | null
  time_ref: [string, string] | null
  recipe: string
  recipe_parts: RecipeParts

  // The remaining AUTHORED recipe declarations. null means the SME wrote none: silence, not an
  // empty value.
  recipe_stage: AttributedText | null
  eligibility_note: AttributedText | null
  authoring_notes: AttributedText[]
  output_additivity: AttributedText | null
  point_in_time_declaration: AttributedText | null

  source_datasets: SuggestionSourceDataset[]
  operands: SuggestionOperand[]
  relationship_dependencies: SuggestionRelationshipDependency[]
  validation_status: SuggestionValidationStatus
  requirements: SuggestionRequirement[]
  warnings: SuggestionWarning[]
  binding_quality: string

  semantic_context_hashes: string[]   // empty in Release A: the semantic plan has not landed
  dataset_profile_hashes: string[]    // empty in Release A: the profile plan has not landed
  grounding_trace_content_hash: string
}

// The exact ids a reader compares for CURRENTNESS — and which are excluded from every semantic
// hash for exactly that reason, so a replay under a new event id churns no revision.
export interface SuggestionBuildProvenance {
  scope_set_id: string | null
  metadata_snapshot_ids: string[]
  dependency_revision_ids: string[]
  evidence_event_ids: string[]
  relationship_realization_revision_ids: string[]
  producer_commit: string | null
  refresh_id: string | null
  generated_at: string | null
}

export type SuggestionProjectionStateName =
  | 'current' | 'stale' | 'pending' | 'partial' | 'failed' | 'retired'

// Release-B projection currentness. Release A reports `null` rather than inventing a `current` it
// cannot prove, so a UI must never synthesize a freshness badge from an absent projection.
export interface SuggestionProjectionState {
  state: SuggestionProjectionStateName
  scope_set_id: string | null
  read_scope_key: string
  scope_epoch: number
  target_fingerprint: string
  current_fingerprint: string | null
  generated_at: string | null
  stale_reason: string | null
  // The SAME closed key vocabulary the collection's identical field carries — a projection omits
  // things for the same reasons a live page does, so it may not be the looser type.
  omitted_counts: SuggestionOmittedCounts
}

export interface FeatureSuggestionHit {
  suggestion: FeatureSuggestionV2
  projection: SuggestionProjectionState | null
  provenance: SuggestionBuildProvenance
}

// V1's counts with `clean_ready` renamed to what it actually means. `groups` keeps V1's `entities`
// semantics: the number of NAMED grain buckets.
export interface SuggestionSummaryV2 {
  suggested: number
  design_checked: number
  needs_external_validation: number
  groups: number
}

export interface SuggestionGroupV2 {
  entity: AttributedLabel | null
  contextual_entity_terms: AttributedText[]
  grain_refs: [string, string][]
  suggestion_ids: string[]
}

export interface SuggestionRejectionV2 {
  template_id: string | null
  candidate_name: string
  code: string
  explanation: string
}

// What a bound left out. Every key is a count of values the page did NOT show; an unknown key from
// a newer backend degrades to its de-underscored words rather than disappearing. Scope-revealing
// keys are dropped server-side, so a caller cannot tell "nothing withheld" from "N withheld".
export type SuggestionOmissionKey =
  | 'operands' | 'business_domains' | 'contextual_domain_terms' | 'contextual_entity_terms'
  | 'use_cases' | 'keywords' | 'authoring_notes' | 'relationship_dependencies'
  | 'evidence_refs' | 'term_evidence'
  | 'withheld_missing_trace' | 'withheld_incomplete_trace' | 'withheld_missing_context'
  | 'withheld_unresolvable_path' | 'withheld_non_recipe_generation_source'
export type SuggestionOmittedCounts = Partial<Record<SuggestionOmissionKey, number>>

export interface SuggestionCollectionContextV2 {
  anchor_catalog_source: string | null
  anchor_table_ref: string | null
  anchor_column_ref: string | null
  // false = this catalog holds no such table FOR THIS CALLER. Keeps the empty screen's DIAGNOSIS
  // honest, exactly as in v1.
  table_known: boolean | null
  summary: SuggestionSummaryV2
  groups: SuggestionGroupV2[]
  rejections: SuggestionRejectionV2[]
  neighbourhood: JoinNeighbourhood | null
  omitted_counts: SuggestionOmittedCounts
}

// Named apart from the catalog-search `FacetBucket` above: the two contracts are versioned by
// different owners and a shared name would silently couple them.
export interface SuggestionFacetBucket {
  id: string
  display_name: string
  count: number
}

export interface FeatureSuggestionPageV2 {
  read_mode: 'on_demand' | 'projected'
  read_scope_key: string          // opaque hash of the caller's visibility CLASSES; not authz
  projection: SuggestionProjectionState | null
  collection: SuggestionCollectionContextV2
  hits: FeatureSuggestionHit[]
  facets: Record<string, SuggestionFacetBucket[]>   // empty until Release B
  next_cursor: string | null
}

// The typed handler-level refusal for a version this deployment does not serve. A NON-integer
// `contract_version` is a different thing: it keeps FastAPI's native 422, whose `detail` is a LIST
// and which carries no code at all.
export const SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION = 'SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION'

// Ask for v2 EXPLICITLY. The version is a literal, not a probe: if this deployment does not serve
// it the route answers 422 with the code above and the caller says so, rather than silently
// rendering a v1 body with every discovery field missing.
export function getTableSuggestionsV2(
  source: string,
  table: string,
): Promise<FeatureSuggestionPageV2> {
  return request(
    `/catalog/${encodeURIComponent(source)}/tables/${encodeURIComponent(table)}`
      + '/suggestions?contract_version=2',
  )
}

// POST one scalar field-correction. Maps the camelCase request to the backend snake_case body
// (defaults mirror the server model: selected_evidence_ids [], replacement_value/reason null). A CAS
// conflict (a concurrent decision/evidence/policy drift) fails closed as HTTP 409, and a four-eyes /
// authz denial as 403 — the shared error path throws BOTH as a catchable ApiError carrying that
// status, so the caller can catch a 409 to reload the asset (fresh CAS) and retry.
export function postFieldDecision(
  source: string,
  objectRef: string,
  field: string,
  req: FieldDecisionRequest,
): Promise<FieldDecisionResult> {
  return post(
    `/catalog/assets/${encodeURIComponent(source)}/${encodeObjectRefPath(objectRef)}`
      + `/fields/${encodeURIComponent(field)}/decisions`,
    {
      action: req.action,
      selected_evidence_ids: req.selectedEvidenceIds ?? [],
      replacement_value: req.replacementValue ?? null,
      reason: req.reason ?? null,
      idempotency_key: req.idempotencyKey,
      expected_latest_decision_id: req.expectedLatestDecisionId,
      expected_evidence_set_hash: req.expectedEvidenceSetHash,
      expected_policy_version: req.expectedPolicyVersion,
    },
  )
}

// ── the data agent: a question, planned and previewed ────────────────────────────────────────────
// `/analysis/plan` retrieves, extracts, grounds, previews and — behind the source/temporal flag —
// SEALS, returning the identity a caller executes that exact plan by. It never runs the statement.
// The API DOES have a run endpoint now (`POST /analysis/execute`, Release-B Task 9), and this
// client deliberately does not call it: running a sealed plan against the bank's warehouse is a
// separate approval gate, and a Run control that 409'd on it would teach people to click through a
// governance decision. The screen offers no Run button for that reason, not because nothing can
// execute.

export interface AnalysisFinding {
  code: string
  subject: string
  detail: string
  clears_when: string
}

export interface AnalysisPeriod {
  label: string
  partitions: string[]
}

export interface AnalysisPreview {
  question: string
  entity: string
  measure: string
  comparison: string
  dimensions: string[]
  periods: AnalysisPeriod[]
  findings: AnalysisFinding[]
  sql: string
  plan_hash: string
  runnable: boolean
  rests_on_unconfirmed_facts: boolean
  blocked_by: { code: string; subject: string } | null
}

export interface ClarificationOption {
  value: string
  label: string
}

export interface AnalysisClarification {
  code: string
  question: string
  optional: boolean
  allows_multiple: boolean
  options: ClarificationOption[]
}

// ── Release B: which copy answered, which of its rows, and what else was considered ─────────────
// The candidate list is READ-SCOPED server-side: a dataset this caller may not see never appears
// by name, only in `considered_withheld`. The UI renders that count and must never try to
// reconstruct what is behind it.

export interface AnalysisCandidate {
  dataset_ref: string
  disposition: string
  reason_codes: string[]
}

export interface AnalysisSourceDecision {
  need_role: string
  withheld: boolean
  dataset_ref?: string
  selection_basis?: string
  authority_basis?: string
  authority_role?: string
  considered?: AnalysisCandidate[]
  considered_withheld?: number
  considered_total?: number
}

export interface AnalysisRowDecision {
  dataset_ref: string
  selection_kind: string
  cutoff_value_ref: string | null
  predicates: { column_ref: string; operator: string }[]
  predicates_withheld: boolean
}

export interface AnalysisSelectionRefusal {
  code: string
  subjects: string[]
  subjects_withheld: number
  detail: string
  // undecided | needs_data_check | structurally_unsuitable | needs_setup. The UI renders the
  // FAMILY: an undecided thing is not a failure and must never be drawn as one.
  family: string
}

export interface AnalysisSelection {
  resolved: boolean
  sources: AnalysisSourceDecision[]
  rows: AnalysisRowDecision[]
  refusals: AnalysisSelectionRefusal[]
  warnings: string[]
}

export interface AnalysisPlanResponse {
  preview: AnalysisPreview
  clarifications: AnalysisClarification[]
  retrieval?: { tables_considered: string[]; dropped_columns: number }
  // null while FEATUREGEN_SOURCE_TEMPORAL_SELECTION is off.
  selection?: AnalysisSelection | null
  // The identity this exact plan can later be executed by. null when nothing was sealed.
  sealed_plan_hash?: string | null
}

export function planAnalysis(question: string): Promise<AnalysisPlanResponse> {
  return post('/analysis/plan', { question })
}

export function clarifyAnalysis(
  question: string, code: string, chosen: string[],
): Promise<AnalysisPlanResponse> {
  return post('/analysis/clarify', { question, code, chosen })
}

// ── data sources: where each catalog's DATA lives ───────────────────────────────────────────────
// Distinct from the OpenMetadata integrations above, which are where the catalog DESCRIPTION comes
// from. These grant read access to a warehouse, so writes need the platform-admin claim.

export interface DataSourceConnection {
  connection_id: string
  environment: string
  engine: string
  tier: string
  host: string
  port: number
  auth_mechanism: string
  secret_ref: string
  execution_principal: string
  allowed_schemas: string[]
  database_name: string
  active: boolean
  usable_here: boolean
}

export interface DataSourceConnections {
  environment: string
  engines: string[]
  connections: DataSourceConnection[]
}

export interface CatalogEngine {
  catalog_source: string
  engine: string | null
  tier: string | null
  declared_by: string | null
}

export function getDataSourceConnections(): Promise<DataSourceConnections> {
  return request('/data-sources/connections')
}

export function putDataSourceConnection(
  body: Omit<DataSourceConnection, 'environment' | 'usable_here'>,
): Promise<{ connection_id: string; environment: string }> {
  return request(`/data-sources/connections/${encodeURIComponent(body.connection_id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function getCatalogEngines(): Promise<{ catalogs: CatalogEngine[] }> {
  return request('/data-sources/catalogs')
}

export function putCatalogEngine(
  source: string, engine: string, tier: string,
): Promise<CatalogEngine> {
  return request(`/data-sources/catalogs/${encodeURIComponent(source)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ engine, tier }),
  })
}

// ---- entity map v0 (ingestion-richness Task 3D) ----
// The READ-ONLY face of the ontology: entities as nodes (read-scoped column counts per catalog),
// available cross-catalog links as edges — the SAME availability truth governance and the planner
// read, never a second interpretation.

export interface EntityCatalogGroup {
  catalog_source: string
  column_count: number
  sample_refs: string[]
}

export interface EntityMapNode {
  entity_id: string
  registered: boolean
  column_count: number
  catalogs: EntityCatalogGroup[]
}

export interface EntityMapEndpoint {
  catalog_source: string
  table_ref: string
  column_refs: string[]
  entity_id: string | null
  concept: string | null
  namespace: string | null
}

export interface EntityMapRealization {
  from_catalog_source: string
  from_table_ref: string
  to_catalog_source: string
  to_table_ref: string
  lifecycle: string
  safety_status: string
  sandbox_eligible: boolean
  production_eligible: boolean
}

export interface EntityMapLink {
  candidate_id: string
  candidate_revision_id: string
  bridge_fact_key: string
  status: 'proposed' | 'confirmed'
  folded_status: string | null
  strength: number
  left: EntityMapEndpoint
  right: EntityMapEndpoint
  realizations: EntityMapRealization[]
}

export interface EntityMap {
  entities: EntityMapNode[]
  links: EntityMapLink[]
}

export function getEntityMap(): Promise<EntityMap> {
  return request('/catalog/entity-map')
}

// ── Release-A dataset/catalog profiles ──────────────────────────────────────────────────────────
// Every route here is flag-gated server-side (FEATUREGEN_DATASET_PROFILES): while the flag is off
// the routes 404, and the UI treats that as "surface absent" (render nothing), never as an error.

// One resolved profile value + its authority triple VERBATIM (producer × strength × lifecycle).
// A proposed value is USABLE and labeled — the UI must never frame it as failure (no-blocked rule).
export interface ProfileSemanticValue {
  value: string
  producer: string
  strength: string
  lifecycle: string
  evidence_ids: string[]
}

// unresolved_family ∈ {undecided, needs_data_check, structurally_unsuitable} — the UI renders the
// FAMILY, never a raw failure string; null when the field is in a normal display/load state.
export interface EffectiveProfileField {
  display: ProfileSemanticValue | null
  load_bearing: ProfileSemanticValue | null
  state: string
  unresolved_reason: string | null
  unresolved_family: string | null
  reason_codes: string[]
}

export interface GovernedFactHead {
  fact_key: string
  folded_status: string
  confirmed_event_id: string | null
}

export interface AssetProfile {
  dataset_logical_ref: string
  catalog_profile_revision_id: string | null
  description: EffectiveProfileField
  business_context: EffectiveProfileField
  domains: EffectiveProfileField
  data_role: EffectiveProfileField
  primary_entity: EffectiveProfileField
  authority_role: EffectiveProfileField
  temporal_storage_model: EffectiveProfileField
  event_or_snapshot: EffectiveProfileField
  grain_fact: GovernedFactHead | null
  availability_fact: GovernedFactHead | null
  missing_context: string[]
  dataset_profile_hash: string
}

export function getAssetProfile(source: string, objectRef: string): Promise<AssetProfile> {
  return request(
    `/catalog/asset-profiles/${encodeURIComponent(source)}/${encodeObjectRefPath(objectRef)}`)
}

// The data_owner proposal surface: writes HUMAN/PROPOSED evidence for the three new profile
// fields. expectedDatasetProfileHash is the aggregate CAS anchor — any drift 409s server-side.
export interface AssetProfilePut {
  expectedDatasetProfileHash: string
  businessContext?: string
  authorityRole?: string
  temporalStorageModel?: string
  note?: string
}

export interface AssetProfilePutResult {
  written: string[]
  dataset_profile_hash: string
  profile: AssetProfile
}

export function putAssetProfile(
  source: string,
  objectRef: string,
  req: AssetProfilePut,
): Promise<AssetProfilePutResult> {
  return request(
    `/catalog/asset-profiles/${encodeURIComponent(source)}/${encodeObjectRefPath(objectRef)}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_dataset_profile_hash: req.expectedDatasetProfileHash,
        business_context: req.businessContext ?? null,
        authority_role: req.authorityRole ?? null,
        temporal_storage_model: req.temporalStorageModel ?? null,
        note: req.note ?? null,
      }),
    },
  )
}

// One catalog the caller may see. `tables`/`columns` are READ-SCOPED counts (honest about the
// caller's scope, never about the catalog). `display_name`/`has_profile` ride only while
// FEATUREGEN_DATASET_PROFILES is on — optional, so a flag-off payload types identically.
export interface VisibleCatalog {
  source: string
  tables: number
  columns: number
  display_name?: string | null
  has_profile?: boolean
}

// The catalogs THIS caller may see (derived, column-level scope). Never 404s and never errors:
// "no catalogs you may see" and "no catalogs at all" are deliberately the same answer, so the
// response cannot be used to probe for hidden catalogs.
export function listCatalogs(): Promise<{ catalogs: VisibleCatalog[] }> {
  return request('/catalogs')
}

export interface CatalogProfileRevision {
  catalog_source: string
  display_name: string | null
  description: string | null
  business_context: string | null
  business_domains: string[]
  producer: string
  strength: string
  lifecycle: string
  producer_ref: string
  ingestion_run_id: string | null
  content_hash: string
  revision_id: string
}

export interface CatalogProfile {
  source: string
  pointer_version: number   // 0 == no narrative yet; the version a first PUT must carry
  profile: CatalogProfileRevision | null
}

export function getCatalogProfile(source: string): Promise<CatalogProfile> {
  return request(`/catalogs/${encodeURIComponent(source)}/profile`)
}

// expectedPointerVersion rides IN THE BODY (the repo's CAS convention); a miss 409s.
export function putCatalogProfile(
  source: string,
  req: {
    expectedPointerVersion: number
    displayName?: string
    description?: string
    businessContext?: string
    businessDomains?: string[]
  },
): Promise<{ source: string; revision_id: string; pointer_version: number }> {
  return request(`/catalogs/${encodeURIComponent(source)}/profile`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      expected_pointer_version: req.expectedPointerVersion,
      display_name: req.displayName ?? null,
      description: req.description ?? null,
      business_context: req.businessContext ?? null,
      business_domains: req.businessDomains ?? [],
    }),
  })
}

// ── Release-B dataset policies (flag-gated: 404 while FEATUREGEN_SOURCE_TEMPORAL_SELECTION is off,
// or while its FEATUREGEN_DATASET_PROFILES dependency is unmet — the panel then renders nothing).
//
// expectedPointerVersion rides IN THE BODY and is REQUIRED (0 == "no policy existed when I opened
// this form"). A miss 409s; the panel keeps the author's draft and shows the other version beside it.

export interface PolicyProvenance {
  evidence: {
    producer: string
    strength: string
    lifecycle: string
    producer_ref: string | null
    evidence_id: string | null
  }[]
  decision_refs: string[]
}

export interface ServingPolicyRevision {
  revision_id: string
  content_hash: string
  eligible_dataset_refs: string[]
  preferred_dataset_refs: string[]
  provenance: PolicyProvenance
}

export interface ServingPolicyView {
  entity_id: string
  need_role: string
  serving_purpose: string
  pointer_version: number
  declared_by?: string
  // True when the policy cannot by itself pick one dataset. Shown as an explicit statement, never
  // resolved by rendering the first element first.
  ambiguous: boolean
  policy: ServingPolicyRevision | null
}

export function getServingPolicy(
  entityId: string, needRole: string, servingPurpose: string,
): Promise<ServingPolicyView> {
  return request('/catalog/dataset-policies/serving/'
    + `${encodeURIComponent(entityId)}/${encodeURIComponent(needRole)}`
    + `/${encodeURIComponent(servingPurpose)}`)
}

export function putServingPolicy(
  entityId: string, needRole: string, servingPurpose: string,
  req: {
    expectedPointerVersion: number
    eligibleDatasetRefs: string[]
    preferredDatasetRefs: string[]
  },
): Promise<{ revision_id: string; pointer_version: number; ambiguous: boolean }> {
  return request('/catalog/dataset-policies/serving/'
    + `${encodeURIComponent(entityId)}/${encodeURIComponent(needRole)}`
    + `/${encodeURIComponent(servingPurpose)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      expected_pointer_version: req.expectedPointerVersion,
      eligible_dataset_refs: req.eligibleDatasetRefs,
      preferred_dataset_refs: req.preferredDatasetRefs,
    }),
  })
}

export interface TemporalPolicyRevision {
  revision_id: string
  content_hash: string
  temporal_storage_model: string
  current_selection: string
  historical_selection: string
  effective_from_ref: string | null
  effective_to_ref: string | null
  snapshot_ref: string | null
  current_flag_ref: string | null
  availability_ref: string | null
  tie_break_refs: string[]
  provenance: PolicyProvenance
}

export interface TemporalPolicyView {
  dataset_logical_ref: string
  // The PROFILE's answer. Non-null means a governed classification exists and the policy must agree
  // with it; null means nobody has decided, and the policy IS the operational declaration.
  load_bearing_temporal_storage_model: string | null
  displayed_temporal_storage_model: string | null
  pointer_version: number
  declared_by?: string
  policy: TemporalPolicyRevision | null
}

export interface TemporalPolicyPut {
  expectedPointerVersion: number
  temporalStorageModel: string
  currentSelection: string
  historicalSelection: string
  effectiveFromRef?: string
  effectiveToRef?: string
  snapshotRef?: string
  currentFlagRef?: string
  availabilityRef?: string
  tieBreakRefs?: string[]
}

export function getTemporalPolicy(source: string, objectRef: string): Promise<TemporalPolicyView> {
  return request('/catalog/dataset-policies/temporal/'
    + `${encodeURIComponent(source)}/${encodeObjectRefPath(objectRef)}`)
}

export function putTemporalPolicy(
  source: string, objectRef: string, req: TemporalPolicyPut,
): Promise<{ dataset_logical_ref: string; revision_id: string; pointer_version: number }> {
  return request('/catalog/dataset-policies/temporal/'
    + `${encodeURIComponent(source)}/${encodeObjectRefPath(objectRef)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      expected_pointer_version: req.expectedPointerVersion,
      temporal_storage_model: req.temporalStorageModel,
      current_selection: req.currentSelection,
      historical_selection: req.historicalSelection,
      effective_from_ref: req.effectiveFromRef || null,
      effective_to_ref: req.effectiveToRef || null,
      snapshot_ref: req.snapshotRef || null,
      current_flag_ref: req.currentFlagRef || null,
      availability_ref: req.availabilityRef || null,
      tie_break_refs: req.tieBreakRefs ?? [],
    }),
  })
}

// ── data-use policies (D14) — the PII allow-policy surface the feature use gate reads ───────────
//
// `status` is a CLOSED three-value vocabulary and NOT a boolean, deliberately: "nobody has decided
// yet" and "somebody withdrew a decision" are different facts, and only the GATE may collapse them.
export type DataUsePolicyStatus = 'none' | 'active' | 'revoked'

export interface DataUsePolicyState {
  concept_name: string
  description: string
  group: string
  status: DataUsePolicyStatus
  pointer_version: number          // 0 == never declared; the version a first approve must carry
  purpose: string | null
  revision_id: string | null
  approved_by: string | null       // who first authored this revision's content
  approved_at: string | null
  declared_by: string | null       // who made it current (the two differ after a re-declaration)
  updated_at: string | null
}

export interface DataUsePolicyListing {
  concepts: DataUsePolicyState[]
  purpose_bounds: { min: number; max: number }
}

export function getDataUsePolicies(): Promise<DataUsePolicyListing> {
  return request('/governance/data-use-policies')
}

export function approveDataUsePolicy(
  conceptName: string, req: { expectedPointerVersion: number; purpose: string },
): Promise<{ concept_name: string; revision_id: string; pointer_version: number; status: string }> {
  return post(`/governance/data-use-policies/${encodeURIComponent(conceptName)}/approve`, {
    expected_pointer_version: req.expectedPointerVersion,
    purpose: req.purpose,
  })
}

export function revokeDataUsePolicy(
  conceptName: string, req: { expectedPointerVersion: number },
): Promise<{ concept_name: string; revision_id: string; pointer_version: number; status: string }> {
  return post(`/governance/data-use-policies/${encodeURIComponent(conceptName)}/revoke`, {
    expected_pointer_version: req.expectedPointerVersion,
  })
}
