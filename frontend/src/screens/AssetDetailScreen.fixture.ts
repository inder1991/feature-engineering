import type * as api from '../api'

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
      table_diagnostic: {
        scope: 'TABLE', operational_status: 'blocked',
        blocking_requirements: [{
          requirement_id: 'table.grain', scope: 'TABLE', status: 'missing', blocking: true,
          cause: 'no confirmed grain', authority_required: 'platform_admin',
        }],
        review_requirements: [], advisory_gaps: ['as_of column not confirmed'],
        summary_scores: {},
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
