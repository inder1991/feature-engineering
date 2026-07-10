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
// `useCaseOrigins` records each confirmed use-case's provenance (llm_proposed / user_added) so the
// proposed-vs-accepted delta stays queryable; `confirmationSource` names how it was confirmed.
export interface ConfirmedScopeInput {
  primary: string | null
  secondary: string[]
  expansion: 'exact' | 'include_descendants'
  unscoped: boolean
  useCaseOrigins: Record<string, string>
  confirmationSource: string
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
  final_disposition: 'eligible' | 'unbuildable' | 'safety_rejected' | 'out_of_scope'
  relevance_tier: 'primary' | 'supporting' | null
  applicability: DispositionStage
  grounding: DispositionStage
  safety: DispositionStage
}

// Run the recognizer over the objective and persist an append-only attempt (no generation run yet).
// Fail-open: the endpoint never returns 5xx; a recognizer failure comes back as status
// 'technical_failure' with unscoped semantics, so the caller can still generate over everything.
export function contractRecognitions(
  hypothesis: string,
  objective: string,
): Promise<RecognitionResp> {
  return post('/contract/recognitions', { hypothesis, objective })
}

// Gate #1 intake: mandatory hypothesis + objective; the server persists the intent and returns the
// gauntlet-validated considered set (anchor + generated alternatives + an advisory recommendation).
// Phase 1B: when `confirmedScope` is supplied (the human confirmed/broadened the recognised scope),
// the server ALSO mints a generation run, persists the scope, grounds only the in-scope recipe
// subset, and attaches per-recipe `dispositions` + an `in_scope_count`. When it is absent, this is
// byte-identical to today's one-shot generate.
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
    // Phase 1B scoped-grounding fields. All null on the flag-off one-shot path → the server takes
    // today's ground-everything route (recognition/applicability never engage).
    intent_id: opts.intentId ?? null,
    recognition_id: opts.recognitionId ?? null,
    confirmed_scope: opts.confirmedScope
      ? {
          primary: opts.confirmedScope.primary,
          secondary: opts.confirmedScope.secondary,
          expansion: opts.confirmedScope.expansion,
          unscoped: opts.confirmedScope.unscoped,
          use_case_origins: opts.confirmedScope.useCaseOrigins,
          confirmation_source: opts.confirmedScope.confirmationSource,
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
// the draft. chosen_option_id is the chosen feature's name from the considered set.
export function contractDraft(
  intentId: string,
  chosenSource: 'anchor' | 'alternative',
  chosenOptionId: string,
  why = '',
): Promise<DraftResp> {
  return post('/contract/draft', {
    intent_id: intentId,
    chosen_source: chosenSource,
    chosen_option_id: chosenOptionId,
    why,
  })
}

// Gate #2 — the governing write. The draft (from contractDraft) is sent back with its intent_id; the
// server re-runs the MCV and mints a versioned, DESIGN-CHECKED contract.
export function contractConfirm(draft: ContractDraft, intentId: string): Promise<Contract> {
  return post('/contract/confirm', { ...draft, intent_id: intentId })
}

export function listContracts(limit = 50): Promise<ContractSummary[]> {
  return request(`/contracts?limit=${limit}`)
}

export function getContract(contractId: string): Promise<ContractDetail> {
  return request(`/contracts/${encodeURIComponent(contractId)}`)
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
