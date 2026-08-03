import type * as api from '../api'

// The v2 discovery wire shape, field-for-field, as reusable builders. Lives OUTSIDE the test file
// so the column-dossier suite can render the exact same payload the table suite does: two fixtures
// would be two contracts, and the whole point of the shared card is that there is one.
export const FIXTURE_SOURCE = 'core_banking'
export const FIXTURE_TABLE = 'public.comp_fin_tran'
const SOURCE = FIXTURE_SOURCE
const TABLE = FIXTURE_TABLE

// ── fixture builders (the v2 wire shape, field-for-field) ───────────────────────────────────────

export function evidence(over: Partial<api.EvidenceAuthority> = {}): api.EvidenceAuthority {
  return {
    producer: 'taxonomy', strength: 'attested', lifecycle: 'active',
    producer_ref: 'recipe-revision:r1', evidence_id: null, ...over,
  }
}

export function label(over: Partial<api.AttributedLabel> = {}): api.AttributedLabel {
  return {
    id: 'trend', display_name: 'Trend & Trajectory', basis: 'template_authored',
    evidence: [evidence()], operational_influence: null,
    source_refs: ['discovery-metadata-revision:d1'], ...over,
  }
}

export function text(over: Partial<api.AttributedText> = {}): api.AttributedText {
  return {
    value: 'payments', basis: 'catalog_resolved',
    evidence: [evidence({ producer: 'llm', strength: 'proposed', producer_ref: null })],
    operational_influence: null, source_refs: ['public.comp_fin_tran.tran_amt'], ...over,
  }
}

export function operand(over: Partial<api.SuggestionOperand> = {}): api.SuggestionOperand {
  return {
    catalog_source: SOURCE,
    logical_ref: 'core_banking.comp_fin_tran.bal_amt',
    graph_object_ref: 'public.comp_fin_tran.bal_amt',
    table_ref: 'comp_fin_tran',
    recipe_role: 'balance',
    classification: 'measure',
    visibility_requires_current: [],
    evidence_refs: ['ev-1'],
    ...over,
  }
}

export function dataset(
  over: Partial<api.SuggestionSourceDataset> = {},
): api.SuggestionSourceDataset {
  return {
    catalog_source: SOURCE, table_ref: 'comp_fin_tran',
    data_role: null, authority_role: null, temporal_storage_model: null, primary_entity: null,
    dataset_profile_hash: null, profile_status: 'unavailable', ...over,
  }
}

export function suggestion(over: Partial<api.FeatureSuggestionV2> = {}): api.FeatureSuggestionV2 {
  return {
    schema_version: 'feature-suggestion-v2',
    suggestion_id: 'sug-1',
    suggestion_revision_id: 'rev-1',
    generation_source: 'recipe',
    template_id: 'balance_trend_90d',
    recipe_revision_id: 'r1',
    discovery_metadata_revision_id: 'd1',
    validation_rule_content_hashes: ['vr-1'],
    read_scope_rule_content_hashes: ['rs-1'],
    name: 'account_balance_trend_90d',
    display_name: 'account_balance_trend_90d',
    business_interpretation: text({
      value: 'How this account’s balance has trended over the last 90 days.',
      basis: 'template_authored', evidence: [evidence()], source_refs: ['recipe-revision:r1'],
    }),
    business_value: text({
      value: 'A falling balance trend leads attrition and hardship by weeks.',
      basis: 'template_authored', evidence: [evidence()], source_refs: ['recipe-revision:r1'],
    }),
    feature_category: label(),
    feature_category_derived_from_family_mapping: false,
    discovery_disposition: 'complete',
    recipe_family: label({ id: 'balance_trend', display_name: 'Balance trend' }),
    business_domains: [],
    contextual_domain_terms: [],
    use_cases: [],
    keywords: [],
    entity: label({
      id: 'account', display_name: 'account',
      source_refs: ['concept-registry:2026.1'],
    }),
    contextual_entity_terms: [],
    grain_refs: [[SOURCE, 'public.comp_fin_tran.acct_id']],
    operation_kind: 'trend_90d',
    window: '90d',
    time_ref: [SOURCE, 'public.comp_fin_tran.as_of_dt'],
    recipe: 'trend_90d(bal_amt) BY acct_id OVER 90d [as_of_dt]',
    recipe_parts: {
      operation: 'trend_90d', measures: ['bal_amt'], grain: 'acct_id',
      window: '90d', time: 'as_of_dt',
    },
    recipe_stage: null,
    eligibility_note: null,
    authoring_notes: [],
    output_additivity: null,
    point_in_time_declaration: null,
    source_datasets: [dataset()],
    operands: [operand()],
    relationship_dependencies: [],
    validation_status: 'DESIGN_CHECKED',
    requirements: [],
    warnings: [],
    binding_quality: 'exact',
    semantic_context_hashes: [],
    dataset_profile_hashes: [],
    grounding_trace_content_hash: 'trace-1',
    ...over,
  }
}

export function provenance(
  over: Partial<api.SuggestionBuildProvenance> = {},
): api.SuggestionBuildProvenance {
  return {
    scope_set_id: null, metadata_snapshot_ids: [], dependency_revision_ids: [],
    evidence_event_ids: [], relationship_realization_revision_ids: [],
    producer_commit: null, refresh_id: null, generated_at: null, ...over,
  }
}

export function hit(over: Partial<api.FeatureSuggestionV2> = {}): api.FeatureSuggestionHit {
  return { suggestion: suggestion(over), projection: null, provenance: provenance() }
}

export const NEEDS_VALIDATION = hit({
  suggestion_id: 'sug-2',
  name: 'customer_inflow_30d',
  display_name: 'customer_inflow_30d',
  business_interpretation: text({
    value: 'Money flowing in to this customer over the last 30 days.',
    basis: 'template_authored', evidence: [evidence()], source_refs: ['recipe-revision:r1'],
  }),
  recipe: 'inflow_outflow(tran_amt) BY cif_id OVER 30d [as_of_dt]',
  validation_status: 'NEEDS_EXTERNAL_VALIDATION',
  entity: label({ id: 'customer', display_name: 'customer' }),
  grain_refs: [[SOURCE, 'public.comp_fin_tran.cif_id']],
  requirements: [
    { code: 'UNIT_CONSISTENT', operand: [SOURCE, 'public.comp_fin_tran.tran_amt'], detail: '' },
  ],
  warnings: [{
    code: 'MISSING_UNIT',
    operand_refs: [[SOURCE, 'public.comp_fin_tran.tran_amt']],
    detail: 'the gauntlet raised the matching typed requirement',
  }],
  operands: [operand({
    graph_object_ref: 'public.comp_fin_tran.tran_amt',
    logical_ref: 'core_banking.comp_fin_tran.tran_amt',
  })],
})

export function page(
  overCollection: Partial<api.SuggestionCollectionContextV2> = {},
  hits: api.FeatureSuggestionHit[] = [hit(), NEEDS_VALIDATION],
  overPage: Partial<api.FeatureSuggestionPageV2> = {},
): api.FeatureSuggestionPageV2 {
  return {
    read_mode: 'on_demand',
    read_scope_key: 'scope-abc',
    projection: null,
    collection: {
      anchor_catalog_source: SOURCE,
      anchor_table_ref: TABLE,
      anchor_column_ref: null,
      table_known: true,
      summary: { suggested: 2, design_checked: 1, needs_external_validation: 1, groups: 2 },
      groups: [
        {
          entity: label({ id: 'account', display_name: 'account' }),
          contextual_entity_terms: [],
          grain_refs: [[SOURCE, 'public.comp_fin_tran.acct_id']],
          suggestion_ids: ['sug-1'],
        },
        {
          entity: label({ id: 'customer', display_name: 'customer' }),
          contextual_entity_terms: [],
          grain_refs: [[SOURCE, 'public.comp_fin_tran.cif_id']],
          suggestion_ids: ['sug-2'],
        },
      ],
      rejections: [],
      neighbourhood: {
        tables_considered: 0, tables_available: 0, truncated: false, max_hops: 1,
        limit_reason: null,
      },
      omitted_counts: {},
      ...overCollection,
    },
    hits,
    facets: {},
    next_cursor: null,
    ...overPage,
  }
}

