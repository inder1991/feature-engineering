import type * as api from '../api'

// An EMPTY per-table suggestions page (v2 discovery contract): what the column dossier's usage
// section loads by default in tests that are not about suggestions — table known, nothing
// suggested, no neighbours, Release A's `on_demand` read with no projection. Tests about the
// section override it.
export function suggestionsFixture(): api.FeatureSuggestionPageV2 {
  return {
    read_mode: 'on_demand',
    read_scope_key: 'scope-test',
    projection: null,
    collection: {
      anchor_catalog_source: 'deposits',
      anchor_table_ref: 'accounts',
      anchor_column_ref: null,
      table_known: true,
      summary: { suggested: 0, design_checked: 0, needs_external_validation: 0, groups: 0 },
      groups: [],
      rejections: [],
      neighbourhood: {
        tables_considered: 0, tables_available: 0, truncated: false, max_hops: 1,
        limit_reason: null,
      },
      omitted_counts: {},
    },
    hits: [],
    facets: {},
    next_cursor: null,
  }
}

// A column asset covering the honesty properties: declared vs operational type; three metadata
// fields whose AUTHORITY differs from whether a value is present (governed/hint/missing all carry a
// non-empty value); verified relationships alongside a proposed candidate; a mixed readiness matrix;
// history + audit; and exactly ONE server-returned field-correction command (currency) so the drawer
// appears for that field and no other.
export function fixture(): api.AssetDetail {
  return {
    version: 'v1',
    source: 'deposits',
    object_ref: 'public.accounts.balance',
    kind: 'column',
    identity: {
      graph_ref: 'deposits:public.accounts.balance',
      object_ref: 'public.accounts.balance',
      logical_ref: 'deposits.accounts.balance',
      source: 'deposits',
      kind: 'column',
      schema_name: 'public',
      table: 'accounts',
      column: 'balance',
      operational_type: 'unknown',
      declared_type: 'double',
      is_grain: false,
      is_as_of: false,
    },
    effective_metadata: {
      fields: {
        // governed: value present AND attested by the source.
        currency: {
          value: 'USD', authority: 'governed', c1_status: 'confirmed',
          provenance: 'source_declared', evidence_provenance: null, selected_evidence_ids: ['ev-c1'],
        },
        // hint: value present but only LLM-proposed — authority is NOT 'governed'.
        entity: {
          value: 'Account', authority: 'hint', c1_status: 'proposed',
          provenance: 'llm_proposed', evidence_provenance: null, selected_evidence_ids: [],
        },
        // missing: value present but NOTHING attested it — must read as unattested, not "present".
        unit: {
          value: 'dollars', authority: 'missing', c1_status: 'none',
          provenance: null, evidence_provenance: null, selected_evidence_ids: [],
        },
      },
    },
    evidence: {
      proposals_by_field: {
        currency: {
          active: [{
            evidence_id: 'ev-c1', producer: 'source', strength: 'declared',
            proposed_value: 'USD', confidence_band: null,
          }],
          rejected: [{
            evidence_id: 'ev-c2', producer: 'llm', strength: 'weak',
            proposed_value: 'EUR', confidence_band: 'low',
          }],
        },
      },
      latest_decision_by_field: {
        currency: {
          decision_event_id: 'dec-1', event_type: 'confirm_existing', conflict_status: null,
          load_bearing: true, decided_at: '2026-07-10T00:00:00Z',
        },
      },
    },
    relationships: {
      containment: {
        table: { object_ref: 'public.accounts', table: 'accounts' },
        columns: [
          { object_ref: 'public.accounts.opened_at', column: 'opened_at', data_type: 'date', sensitivity: null },
        ],
      },
      approved_joins: [{
        from_ref: 'public.accounts.balance', to_ref: 'public.customers.id',
        cardinality: 'N:1', status: 'VERIFIED', approved_join_fact_key: 'ajk-1',
      }],
      // One strong link and one weak one — the real shape. The branch pair matches on type alone
      // and is exactly the case that must be visible but ranked down.
      cross_catalog: [
        {
          entity_id: 'customer', left_catalog_source: 'cib',
          left_object_ref: 'public.bo_cib_customer.cust_num', right_catalog_source: 'ftr',
          right_object_ref: 'public.comp_financial_tran_repos_dly.cif_id',
          status: 'proposed', strength: 10, data_type_family: 'text',
          left_is_grain: true, right_is_grain: false, type_basis: 'declared', fact_key: 'fk-cust',
          why: "one side is its table's key; types as declared in the source file",
        },
        {
          entity_id: 'branch', left_catalog_source: 'cib',
          left_object_ref: 'public.bo_cib_customer.cust_prim_branch_nm', right_catalog_source: 'ftr',
          right_object_ref: 'public.comp_financial_tran_repos_dly.sol_desc',
          status: 'proposed', strength: 0, data_type_family: 'text',
          left_is_grain: false, right_is_grain: false, type_basis: 'declared', fact_key: 'fk-br',
          why: 'neither side is a key — types match but this may not be a real join',
        },
      ],
      semantic: {
        status: 'available',
        verified_edges: [{
          kind: 'entity_assignment', status: 'VERIFIED', object_ref: 'public.accounts.balance',
          entity: 'Account', fact_key: 'fk-ent-1', confirmed_event_id: 'e1', available_actions: [],
        }],
        candidates: [{
          candidate_id: 'cand-1', binding_kind: 'currency_binding', disposition: 'proposed',
          reason_codes: ['name_match'], subject_graph_ref: 'deposits:public.accounts.balance',
          target_graph_ref: 'deposits:public.fx.usd_rate', proposed_value: 'USD',
          fact_key: null, fact_status: null, available_actions: [],
        }],
        divergences: [],
      },
    },
    readiness: {
      column_capabilities: {
        source: 'deposits', object_ref: 'public.accounts.balance',
        logical_ref: 'deposits.accounts.balance',
        as_measure: {
          use: 'as_measure', operational_status: 'ready',
          requirements: [{
            requirement_id: 'measure.numeric', status: 'confirmed', blocking: false,
            authority: 'source', c1_status: 'confirmed', evidence_ids: [], fact_event_id: null,
            decision_event_id: null, external_preview: false, reason: 'numeric type confirmed',
          }],
        },
        as_entity_key: {
          use: 'as_entity_key', operational_status: 'blocked',
          requirements: [{
            requirement_id: 'entity.assigned', status: 'missing', blocking: true,
            authority: 'human', c1_status: null, evidence_ids: [], fact_event_id: null,
            decision_event_id: null, external_preview: false, reason: 'no entity assignment',
          }],
        },
        as_event_time: { use: 'as_event_time', operational_status: 'unavailable', requirements: [] },
        as_grain_key: { use: 'as_grain_key', operational_status: 'blocked', requirements: [] },
        as_join_key: { use: 'as_join_key', operational_status: 'ready', requirements: [] },
      },
      // The product view the screen renders: five roles, each usable or not, in plain words.
      usability: {
        object_ref: 'public.accounts.balance',
        roles: [
          { role: 'as_measure', label: 'Measure', state: 'confirmed', headline: 'Confirmed',
            detail: 'Confirmed, with nothing outstanding.', action: null,
            outstanding: [], data_checks: [] },
          { role: 'as_entity_key', label: 'Entity key', state: 'not_set', headline: 'Not set',
            detail: 'Nothing — person or AI — has proposed entity.assigned for this column.',
            action: 'assign', outstanding: ['entity.assigned'], data_checks: [] },
          { role: 'as_event_time', label: 'Event time', state: 'unavailable',
            headline: 'Unavailable',
            detail: 'The governed projection could not be read.', action: null,
            outstanding: [], data_checks: [] },
          { role: 'as_grain_key', label: 'Grain key', state: 'ai_proposed',
            headline: 'AI proposed',
            detail: 'Proposed by AI and not yet reviewed by a person.', action: 'confirm',
            outstanding: ['grain'], data_checks: [] },
          { role: 'as_join_key', label: 'Join key', state: 'confirmed', headline: 'Confirmed',
            detail: 'Confirmed, with nothing outstanding.', action: null,
            outstanding: [], data_checks: [] },
        ],
        usable_roles: 3, total_roles: 5, headline: 'Usable for 3 of 5 roles',
      },
      table_rollup: {
        table: 'accounts', headline: '1 columns need a decision (waiting on a review decision).',
        columns_unreviewed: 0, columns_needing_decision: 1, requirements_total: 1,
        dominant_cause: 'unresolved_authority',
        dominant_cause_plain: 'waiting on a review decision', columns_outstanding: 1,
      },
    },
    history: {
      runs: [{
        ingestion_run_id: 'run-1', relation: 'asserted_in', at: '2026-07-10T00:00:00Z',
        status: 'ingested', origin_type: 'upload', started_at: '2026-07-10T00:00:00Z',
        completed_at: '2026-07-10T00:01:00Z',
        stages: [{ stage: 'parse', attempt: 1, state: 'succeeded', reason_code: null }],
      }],
      truncated: false,
    },
    // audit intentionally ABSENT + named unavailable → the History tab must say "not available",
    // never invent summaries.
    actions: [{
      field: 'currency',
      available_actions: ['confirm_existing', 'propose_override', 'reject'],
      expected_latest_decision_id: 'dec-1',
      expected_evidence_set_hash: 'hash-1',
      expected_policy_version: 'pol-1',
    }],
    included_sections: [
      'effective_metadata', 'evidence', 'relationships', 'readiness', 'history',
    ],
    unavailable_sections: ['audit'],
    consistency_token: 'token-1',
  }
}
