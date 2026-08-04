import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { AssetDetailScreen } from './AssetDetailScreen'
import { fixture, suggestionsFixture } from './AssetDetailScreen.fixture'

// The Context tab (semantic Task 7). It is fed from the DOSSIER's own `context` section, so this
// file also pins the architectural decision the review forced: no second request, no second
// snapshot, no `/context` route.
//
// The product rules under test are the no-"blocked" ones:
//   * a proposed value is shown as a usable value with a neutral author chip;
//   * a review badge is never rendered as permission — executability is a separate, server-answered
//     fact and the row says so in both directions;
//   * ownership/usage/data-product context reads "not supplied", never zero;
//   * what the bounded view left out is stated, per kind.

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    getAssetDetail: vi.fn(),
    postFieldDecision: vi.fn(),
    getTableSuggestions: vi.fn(),
  }
})
const getAssetDetail = vi.mocked(api.getAssetDetail)
const getTableSuggestions = vi.mocked(api.getTableSuggestions)

beforeEach(() => {
  getAssetDetail.mockReset()
  getTableSuggestions.mockReset()
  getTableSuggestions.mockResolvedValue(suggestionsFixture())
})

function contextFixture(over: Partial<api.ContextSection> = {}): api.ContextSection {
  return {
    status: 'available',
    version: 'context-graph/v1',
    anchor_id: 'deposits:public.accounts.balance',
    source_meaning: [
      {
        field: 'definition', value: 'Customer ledger balance.', resolution_status: 'declared',
        operational_influence: null, authority_label: 'source_attested', producer: 'source',
        strength: 'attested', lifecycle: 'active', evidence_ids: ['ev-def'],
      },
    ],
    resolved_meaning: [
      {
        field: 'concept', value: 'monetary_stock', resolution_status: 'current',
        operational_influence: null, authority_label: 'llm_proposed', producer: 'llm',
        strength: 'proposed', lifecycle: 'active', evidence_ids: ['ev-con'],
      },
      {
        field: 'entity', value: 'customer', resolution_status: 'current',
        operational_influence: 'hint', authority_label: 'source_proposed', producer: 'source',
        strength: 'proposed', lifecycle: 'active', evidence_ids: [],
      },
      {
        field: 'party_role', value: 'counterparty', resolution_status: 'current',
        operational_influence: null, authority_label: 'deterministic', producer: 'parser',
        strength: 'supported', lifecycle: 'active', evidence_ids: [],
      },
    ],
    concept_path: ['monetary_stock', 'measure'],
    identifier_namespace: null,
    related_columns: [
      { object_ref: 'deposits::public.accounts.id', column: 'id', concept: 'account_id',
        party_role: null },
    ],
    relationships: [],
    profiles: {
      catalog_profile_revision_id: null,
      dataset_profile_hash: null,
      data_role: null, primary_entity: null, authority_role: null, temporal_storage_model: null,
      missing_context: [],
    },
    uncertainty: {
      missing_context: ['dataset_profile_absent', 'relationship_context_absent'],
      not_supplied: ['ownership', 'usage', 'data_product'],
    },
    not_supplied: ['ownership', 'usage', 'data_product'],
    nodes: [], edges: [],
    truncation: { truncated: false, omitted: {} },
    content_hash: 'abc',
    ...over,
  }
}

async function openContext(context?: api.ContextSection | undefined,
                          over: (d: api.AssetDetail) => void = () => {}) {
  const detail = fixture()
  detail.context = context
  over(detail)
  getAssetDetail.mockResolvedValue({ detail, etag: 'etag-1' })
  render(<AssetDetailScreen source="deposits" objectRef="public.accounts.balance" />)
  await screen.findByRole('group', { name: /asset sections/i })
  await userEvent.click(screen.getByRole('button', { name: 'Context' }))
}

it('opens from the dossier without issuing a second request', async () => {
  await openContext(contextFixture())
  expect(screen.getByTestId('context-tab')).toBeInTheDocument()
  // ONE fetch for the whole page. A separate /context call would be a second snapshot the
  // consistency token does not cover.
  expect(getAssetDetail).toHaveBeenCalledTimes(1)
})

it('separates what the source said from what the platform resolved', async () => {
  await openContext(contextFixture())
  const source = screen.getByTestId('context-source-meaning')
  expect(within(source).getByText('Customer ledger balance.')).toBeInTheDocument()
  const resolved = screen.getByTestId('context-resolved-meaning')
  expect(within(resolved).getByText('monetary_stock')).toBeInTheDocument()
})

it('renders an AI-proposed value as a usable value with a neutral author chip', async () => {
  await openContext(contextFixture())
  const row = screen.getByTestId('context-value-concept')
  expect(within(row).getByText('monetary_stock')).toBeInTheDocument()
  expect(within(row).getByText('AI proposed')).toBeInTheDocument()
  // The no-blocked rule: nothing frames the proposal as an error or a blocker.
  expect(row.textContent).not.toMatch(/blocked|invalid|failed|error/i)
})

it('shows the concept hierarchy as an ancestry chain', async () => {
  await openContext(contextFixture())
  expect(screen.getByTestId('context-concept-path')).toHaveTextContent('monetary_stock → measure')
})

it('says a non-identifier column has no value space rather than showing a blank', async () => {
  await openContext(contextFixture())
  expect(screen.getByTestId('context-namespace')).toHaveTextContent(/not an identifier/i)
})

it('names an unresolved issuer instead of implying the namespace is verified', async () => {
  await openContext(contextFixture({
    identifier_namespace: { scheme: 'bic', issuer_scope: null, basis: 'unresolved' },
  }))
  const namespace = screen.getByTestId('context-namespace')
  expect(namespace).toHaveTextContent('bic')
  expect(namespace).toHaveTextContent(/issuer not resolved yet/i)
})

it('renders a reviewed link WITHOUT implying it can be executed', async () => {
  await openContext(contextFixture({
    relationships: [{
      relationship_ref: 'bfk_1', kind: 'direct_equality',
      left_ref: 'deposits::public.accounts.id', right_ref: 'cards::public.cust.cif_id',
      availability: 'available', review_status: 'human_verified',
      assessment_revision_id: 'bca_1', producer: 'taxonomy', strength: 'confirmed',
      lifecycle: 'active', current: true, evidence_ids: ['ev-1'],
      // The stored realization IS production-eligible, and the link IS human-verified — and the
      // revalidating reader still says no. The row must repeat that, not infer permission.
      executable_now: false,
      realizations: [{
        realization_revision_id: 'brr_1', from_ref: 'deposits::public.accounts',
        to_ref: 'cards::public.cust', lifecycle: 'active',
        safety_status: 'deterministically_validated', cardinality: '1:1', scope_id: 'scope-1',
        sandbox_eligible: true, production_eligible: true, executable_now: false,
      }],
    }],
  }))
  const row = screen.getByTestId('context-link-bfk_1')
  expect(within(row).getByText('human verified')).toBeInTheDocument()
  expect(within(row).getByText('not executable now')).toBeInTheDocument()
  expect(within(row).queryByText('executable now')).not.toBeInTheDocument()
  // Fan-out never renders without its direction and scope.
  expect(row).toHaveTextContent('1:1')
  expect(row).toHaveTextContent('scope scope-1')
})

it('labels a crosswalk distinctly and never as a failure', async () => {
  // A direct bridge AND a crosswalk between the SAME endpoints: both rows, distinct refs, distinct
  // kinds. Collapsing them would lose the difference between "these ids are equal" and "these ids
  // are related through this table" — and only one of those may turn out to be true.
  await openContext(contextFixture({
    relationships: [
      {
        relationship_ref: 'bfk_1', kind: 'direct_equality',
        left_ref: 'deposits::public.accounts.id', right_ref: 'cards::public.cust.cif_id',
        availability: 'available', review_status: 'unreviewed',
        assessment_revision_id: 'bca_1', producer: 'taxonomy', strength: 'proposed',
        lifecycle: 'active', current: true, evidence_ids: [], executable_now: false,
        realizations: [],
      },
      {
        relationship_ref: 'cwd_1', kind: 'crosswalk',
        left_ref: 'deposits::public.accounts.id', right_ref: 'cards::public.cust.cif_id',
        availability: 'available', review_status: 'unreviewed',
        assessment_revision_id: null, producer: 'taxonomy', strength: 'proposed',
        lifecycle: 'active', current: true, evidence_ids: ['ev-role'], executable_now: false,
        realizations: [],
        crosswalk: {
          definition_id: 'cwd_1', definition_revision_id: 'cwd_2',
          mapping_dataset_ref: 'deposits::public.acct_xref',
          source_to_mapping_refs: ['deposits::public.acct_xref.acct_no'],
          mapping_to_target_refs: ['deposits::public.acct_xref.cif_id'],
          mapping_temporal_policy_revision_id: null, leg_pins: [],
        },
      },
    ],
  }))
  expect(screen.getByTestId('context-link-bfk_1')).toBeInTheDocument()
  const cross = screen.getByTestId('context-link-cwd_1')
  expect(within(cross).getByText('crosswalk')).toBeInTheDocument()
  // The mapping table is what makes it readable AS a crosswalk, and both legs are named.
  const detail = screen.getByTestId('context-crosswalk-cwd_1')
  expect(detail).toHaveTextContent('deposits::public.acct_xref')
  expect(detail).toHaveTextContent('deposits::public.acct_xref.acct_no')
  expect(detail).toHaveTextContent('deposits::public.acct_xref.cif_id')
  // NO-BLOCKED framing: unreviewed is a state, not a fault, and it is described as usable.
  expect(detail).toHaveTextContent(/nobody has reviewed this mapping yet/i)
  expect(detail).toHaveTextContent(/proposal you can act on/i)
  expect(detail).not.toHaveTextContent(/blocked|failed|invalid|error/i)
  // And nothing on the row implies it can run.
  expect(within(cross).getByText('not executable now')).toBeInTheDocument()
})

it('says no link is in view rather than asserting none exists', async () => {
  await openContext(contextFixture())
  expect(screen.getByTestId('context-links')).toHaveTextContent(/no cross-catalog link is in view/i)
})

it('renders the profile identity it was assembled under', async () => {
  await openContext(contextFixture({
    profiles: {
      catalog_profile_revision_id: 'cpr_' + 'a'.repeat(64),
      dataset_profile_hash: 'deadbeef',
      data_role: { value: 'crosswalk', producer: 'source', strength: 'attested',
        lifecycle: 'active', state: 'display_only', unresolved_family: null },
      primary_entity: null, authority_role: null, temporal_storage_model: null,
      missing_context: [],
    },
  }))
  const profiles = screen.getByTestId('context-profiles')
  expect(profiles).toHaveTextContent('deadbeef')
  expect(screen.getByTestId('context-data-role')).toHaveTextContent('crosswalk')
})

it('renders an undecided data role as a product family, never a failure string', async () => {
  await openContext(contextFixture({
    profiles: {
      catalog_profile_revision_id: null, dataset_profile_hash: 'deadbeef',
      data_role: { value: null, state: 'no_evidence', unresolved_family: 'undecided' },
      primary_entity: null, authority_role: null, temporal_storage_model: null,
      missing_context: [],
    },
  }))
  expect(screen.getByTestId('context-data-role')).toHaveTextContent('Nobody has decided yet')
})

it('shows the adjudicator alternatives and any ontology gap as suggestions', async () => {
  await openContext(contextFixture(), d => {
    d.semantic_adjudication = {
      status: 'available',
      structured_result_id: 'sr_1',
      selected_concept: 'monetary_stock',
      alternatives: ['monetary_flow', 'balance_amount'],
      confidence_band: 'medium',
      reason_codes: ['ambiguous_alternatives'],
      missing_context: [],
      ontology_gap: {
        proposed_label: 'settled_balance', definition: 'A balance after settlement.',
        parent_concept: 'monetary_stock', aliases: [],
      },
    }
  })
  const section = screen.getByTestId('context-adjudication')
  expect(within(section).getByText('monetary_flow')).toBeInTheDocument()
  expect(screen.getByTestId('context-ontology-gap')).toHaveTextContent('settled_balance')
  expect(screen.getByTestId('context-ontology-gap')).toHaveTextContent(/never applied automatically/i)
})

it('reports ownership and usage as NOT SUPPLIED, never as zero', async () => {
  await openContext(contextFixture())
  const notSupplied = screen.getByTestId('context-not-supplied')
  expect(notSupplied).toHaveTextContent('ownership')
  expect(notSupplied).toHaveTextContent('usage')
  expect(notSupplied).toHaveTextContent('data product')
  expect(notSupplied).toHaveTextContent(/not the same as there being none/i)
  expect(notSupplied.textContent).not.toMatch(/\b0 owners?\b/)
})

it('states what the bounded view left out, per kind', async () => {
  await openContext(contextFixture({
    truncation: { truncated: true, omitted: { lineage_column: 3, relationship: 1 } },
  }))
  const omitted = screen.getByTestId('context-omitted')
  expect(omitted).toHaveTextContent('3 lineage column')
  expect(omitted).toHaveTextContent('1 relationship')
  expect(screen.getByTestId('context-truncated')).toBeInTheDocument()
})

it('renders missing-context codes as readable phrases, not as a data-quality score', async () => {
  await openContext(contextFixture())
  const missing = screen.getByTestId('context-missing')
  expect(missing).toHaveTextContent('dataset profile absent')
  // Nothing here presents a percentage, a completeness bar or a failure count.
  expect(missing.textContent).not.toMatch(/%|complete|score|fail/i)
})

it('discloses a lagged projection instead of showing a stale resolved meaning', async () => {
  await openContext(contextFixture({
    status: 'projection_unavailable',
    source_meaning: undefined,
    resolved_meaning: undefined,
    projection: { code: 'CATALOG_PROJECTION_UNAVAILABLE', detail: 'overlay is behind' },
  }))
  expect(screen.getByTestId('context-projection')).toHaveTextContent(/catalog projection was behind/i)
  expect(screen.queryByTestId('context-resolved-meaning')).not.toBeInTheDocument()
})

it('renders a table anchor with an honest note instead of fabricated column meaning', async () => {
  await openContext(contextFixture({
    status: 'table',
    note: 'table asset — structural and profile context only',
    source_meaning: undefined,
    resolved_meaning: undefined,
    concept_path: undefined,
  }))
  expect(screen.getByTestId('context-table-note')).toHaveTextContent(/table asset/i)
  expect(screen.queryByTestId('context-source-meaning')).not.toBeInTheDocument()
})

it('degrades to an explicit unavailable message rather than a blank tab', async () => {
  await openContext(undefined)
  expect(screen.getByTestId('context-unavailable')).toHaveTextContent(/not available/i)
})
