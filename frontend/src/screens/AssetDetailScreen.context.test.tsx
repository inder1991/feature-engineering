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
    getTableSuggestionsV4: vi.fn(),
  }
})
const getAssetDetail = vi.mocked(api.getAssetDetail)
const getTableSuggestions = vi.mocked(api.getTableSuggestionsV4)

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

function directRelationship(): api.ContextRelationship {
  return {
    relationship_ref: 'bfk_1', kind: 'direct_equality',
    left_ref: 'deposits::public.accounts.id', right_ref: 'cards::public.cust.cif_id',
    availability: 'available', review_status: 'unreviewed',
    assessment_revision_id: 'bca_1', producer: 'taxonomy', strength: 'proposed',
    lifecycle: 'active', current: true, evidence_ids: [], executable_now: false,
    realizations: [],
  }
}

function crosswalkRelationship(
  over: Partial<api.ContextRelationship> = {},
): api.ContextRelationship {
  return {
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
    ...over,
  }
}

it('labels a crosswalk distinctly and never as a failure', async () => {
  // A direct bridge AND a crosswalk between the SAME endpoints: both rows, distinct refs, distinct
  // kinds. Collapsing them would lose the difference between "these ids are equal" and "these ids
  // are related through this table" — and only one of those may turn out to be true.
  await openContext(contextFixture({
    relationships: [directRelationship(), crosswalkRelationship()],
  }))
  expect(screen.getByTestId('context-link-bfk_1')).toBeInTheDocument()
  const cross = screen.getByTestId('context-link-cwd_1')
  // The LABEL names the claim, not the machine word: "crosswalk" alone is only distinct to someone
  // who already knows the model, and the two kinds differ in whether a third table is needed at all.
  expect(within(cross).getByText('crosswalk (mapping-mediated)')).toBeInTheDocument()
  expect(
    within(screen.getByTestId('context-link-bfk_1')).getByText('direct equality'),
  ).toBeInTheDocument()
  // The mapping table is what makes it readable AS a crosswalk, and both legs are named.
  const detail = screen.getByTestId('context-crosswalk-cwd_1')
  expect(detail).toHaveTextContent('deposits::public.acct_xref')
  expect(detail).toHaveTextContent('deposits::public.acct_xref.acct_no')
  expect(detail).toHaveTextContent('deposits::public.acct_xref.cif_id')
  // NO-BLOCKED framing: unreviewed is a state, not a fault, and it is described as usable.
  expect(detail).toHaveTextContent(/nobody has reviewed this mapping yet/i)
  expect(detail).toHaveTextContent(/proposal you can act on/i)
  expect(detail).not.toHaveTextContent(/blocked|failed|invalid|error/i)
  // The runnability sentence rides on BOTH review branches — see the confirmed case below.
  expect(detail).toHaveTextContent(/running a crosswalk is not available yet, whatever its review/i)
  // And nothing on the row implies it can run.
  expect(within(cross).getByText('not executable now')).toBeInTheDocument()
})

it('a CONFIRMED crosswalk is still not runnable, and says so in the same breath', async () => {
  // The invariant, and the reason both sentences have to render together: a review is
  // accountability, never permission. If confirming a crosswalk swapped the "not available yet"
  // sentence for the reviewer hint, the badge would read as the thing that unlocked execution —
  // which is exactly the misreading the whole release is built to prevent. Neither wording had a
  // test before, because the fixture only ever carried an unreviewed crosswalk.
  await openContext(contextFixture({
    relationships: [
      crosswalkRelationship({ review_status: 'human_verified', strength: 'confirmed' }),
    ],
  }))
  const detail = screen.getByTestId('context-crosswalk-cwd_1')
  expect(detail).toHaveTextContent(/a reviewer has confirmed this mapping/i)
  expect(detail).toHaveTextContent(/running a crosswalk is not available yet, whatever its review/i)
  // The unreviewed wording is GONE — confirmed is confirmed, not both at once.
  expect(detail).not.toHaveTextContent(/nobody has reviewed this mapping yet/i)
  // Still nothing that reads as permission.
  const cross = screen.getByTestId('context-link-cwd_1')
  expect(within(cross).getByText('not executable now')).toBeInTheDocument()
  expect(detail).not.toHaveTextContent(/blocked|failed|invalid|error/i)
})

it('an unmeasured crosswalk reads as discoverable-unmeasured, never as a blocker', async () => {
  // Release C Task 11. "Nobody has profiled this yet" and "this cannot work" are different
  // answers, and the no-blocked rule says the first must never wear the second's clothes.
  await openContext(contextFixture({ relationships: [crosswalkRelationship()] }))
  const measured = screen.getByTestId('crosswalk-measurement-cwd_1')
  expect(measured).toHaveTextContent(/discoverable, unmeasured/i)
  expect(measured).not.toHaveTextContent(/blocked|failed|invalid|error|unavailable/i)
})

it('a measured crosswalk shows BOTH directions, which may disagree', async () => {
  // The headline shape: 1:1 forward, N:1 reverse. Rendering one verdict for the pair would either
  // hide the usable direction or imply the unusable one is fine.
  await openContext(contextFixture({
    relationships: [crosswalkRelationship({
      crosswalk: {
        definition_id: 'cwd_1', definition_revision_id: 'cwd_2',
        mapping_dataset_ref: 'deposits::public.acct_xref',
        source_to_mapping_refs: ['deposits::public.acct_xref.acct_no'],
        mapping_to_target_refs: ['deposits::public.acct_xref.cif_id'],
        mapping_temporal_policy_revision_id: null, leg_pins: [],
        executable_now: false,
        admission_policy_version: 'crosswalk-admission-v1:abc',
        measurement: {
          observation_revision_id: 'cwo_1', scope_id: 'sandbox', observed_at: '2026-08-04T13:00:00Z',
          as_of: '2026-08-04', method: 'exact', row_coverage: 'full', complete: true,
          composed_row_count: 7, source_to_target_max_matches: 1, target_to_source_max_matches: 2,
          mapping_row_count: 3, mapping_temporal_policy_revision_id: null,
          caveats: ['mapping_temporal_policy_absent'], failures: [],
        },
        directions: [
          { direction: 'source_to_target', safety_status: 'deterministically_validated',
            cardinality: 'many_to_one', sandbox_admissible: true, production_admissible: true,
            reason_codes: ['deterministic_crosswalk_policy_satisfied'] },
          { direction: 'target_to_source', safety_status: 'unsafe', cardinality: 'one_to_many',
            sandbox_admissible: false, production_admissible: false,
            reason_codes: ['directional_crosswalk_fanout'] },
        ],
      },
    })],
  }))
  const measured = screen.getByTestId('crosswalk-measurement-cwd_1')
  expect(measured).toHaveTextContent(/7 joined rows over 3 mapping rows/i)
  expect(measured).toHaveTextContent(/source to target/i)
  expect(measured).toHaveTextContent(/target to source/i)
  expect(measured).toHaveTextContent(/production admissible/i)
  expect(measured).toHaveTextContent(/refused/i)
  // The caveat is SHOWN, never swallowed: the counts alone would read as a claim about now.
  expect(measured).toHaveTextContent(/mapping temporal policy absent/i)
  // And admission never makes it runnable.
  const cross = screen.getByTestId('context-link-cwd_1')
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

// ── Release C Task 13: the four kinds, the framing rules and lineage on the row ─────────────────

it('names all four relationship kinds distinctly, each by the claim it makes', async () => {
  // The plan's first checkbox. Four kinds that must not read alike: whether a join needs a third
  // table, whether it needs a value transform, and whether it is a join at all are three different
  // questions, and a shared machine-word badge answers none of them.
  await openContext(contextFixture({
    relationships: [
      directRelationship(),
      crosswalkRelationship(),
      crosswalkRelationship({ relationship_ref: 'tr_1', kind: 'transformed', crosswalk: null }),
      crosswalkRelationship({ relationship_ref: 'so_1', kind: 'semantic_only', crosswalk: null }),
    ],
  }))
  // `getByText` THROWS on a miss and on a duplicate, so this loop is the whole assertion: four
  // labels, each rendered exactly once. (The line that used to sit here asserted that a literal
  // array of four distinct strings had four distinct members, which is a fact about the array.)
  const labels = ['direct equality', 'crosswalk (mapping-mediated)', 'transformed', 'semantic-only']
  for (const label of labels) {
    expect(screen.getByText(label)).toBeInTheDocument()
  }
})

it('shows what already depends on a crosswalk, and never a zero it has not earned', async () => {
  // The user-ratified rule. "Nothing uses this" and "no store records who uses this" are different
  // claims; rendering the second as 0 is what invites the "approve it and it becomes usable" story.
  await openContext(contextFixture({
    relationships: [crosswalkRelationship({
      crosswalk: {
        ...crosswalkRelationship().crosswalk!,
        already_depended_on_by: [
          {
            category: 'published_features', state: 'not_tracked_yet', count: null,
            display: 'not tracked yet', store: 'contract_metadata_dependency',
            basis: 'a registered feature whose contract depends on this crosswalk',
            reason: 'no crosswalk marker exists in that column',
          },
        ],
      },
    })],
  }))
  const depends = screen.getByTestId('crosswalk-depends-cwd_1')
  expect(depends).toHaveTextContent(/what already depends on this/i)
  expect(depends).toHaveTextContent(/not tracked yet/i)
  expect(depends).not.toHaveTextContent(/\b0\b/)
  expect(depends).not.toHaveTextContent(/approv|unblock|enable/i)
})

it('renders every unresolved reason as one of the three families, never as a fault', async () => {
  await openContext(contextFixture({
    relationships: [crosswalkRelationship({
      crosswalk: {
        ...crosswalkRelationship().crosswalk!,
        unresolved_families: {
          directional_crosswalk_fanout: 'structurally_unsuitable',
          crosswalk_not_measured: 'needs_data_check',
          mapping_temporal_policy_unresolved: 'undecided',
        },
      },
    })],
  }))
  const families = screen.getByTestId('crosswalk-families-cwd_1')
  expect(families).toHaveTextContent('Structurally unsuitable')
  expect(families).toHaveTextContent('Needs a data check')
  expect(families).toHaveTextContent('Nobody has decided yet')
  expect(families).not.toHaveTextContent(/blocked|failed|rejected|error/i)
})

it('says the deployment switch is off in its own sentence, not as a verdict', async () => {
  // Flag off keeps a crosswalk discoverable and structurally non-executable, and that is a fact
  // about this installation. Saying "not safe" instead would blame the evidence for a switch.
  await openContext(contextFixture({
    relationships: [crosswalkRelationship({
      crosswalk: { ...crosswalkRelationship().crosswalk!, execution_enabled: false },
    })],
  }))
  const detail = screen.getByTestId('context-crosswalk-cwd_1')
  expect(detail).toHaveTextContent(/switched off in this deployment/i)
  expect(detail).toHaveTextContent(/structurally cannot run here/i)
  expect(detail).not.toHaveTextContent(/unsafe|failed|blocked/i)
})

it('names the mapping row policy a crosswalk was measured under', async () => {
  // Uniqueness over unfiltered SCD history and uniqueness over the rows a traversal reads are
  // different claims, and the reader cannot tell them apart without seeing whether a policy exists.
  await openContext(contextFixture({
    relationships: [crosswalkRelationship({
      crosswalk: {
        ...crosswalkRelationship().crosswalk!,
        mapping_temporal_policy_revision_id: 'dtp_abc123',
      },
    })],
  }))
  expect(screen.getByTestId('context-crosswalk-cwd_1')).toHaveTextContent('dtp_abc123')
})

it('says nobody has declared a mapping row policy, without calling it a fault', async () => {
  await openContext(contextFixture({ relationships: [crosswalkRelationship()] }))
  const undeclared = screen.getByTestId('context-crosswalk-cwd_1')
  expect(undeclared).toHaveTextContent(/nobody has declared which rows/i)
  expect(undeclared).not.toHaveTextContent(/blocked|invalid/i)
})

it('lists every pinned revision the traversal would carry, including both legs', async () => {
  // Lineage's own question on the row: WHICH revisions was this computed under. The same set Task
  // 12 seals into the generated project's provenance — one leg shown would read as a direct link.
  await openContext(contextFixture({
    relationships: [crosswalkRelationship({
      crosswalk: {
        ...crosswalkRelationship().crosswalk!,
        pinned_revisions: {
          crosswalk_definition_revision: 'cwd_rev_1',
          composed_observation_revision: 'cwo_obs_1',
        },
        leg_pins: [
          {
            kind: 'same_catalog', plan_hash: 'plan-1',
            from_dataset_ref: 'deposits::public.accounts',
            to_dataset_ref: 'deposits::public.acct_xref',
            from_binding_revision_id: 'pbr_a', to_binding_revision_id: 'pbr_b',
            read_set_hash: 'read-1',
          },
          {
            kind: 'cross_catalog', plan_hash: 'plan-2',
            from_dataset_ref: 'deposits::public.acct_xref',
            to_dataset_ref: 'cards::public.cust',
            from_binding_revision_id: 'pbr_b', to_binding_revision_id: 'pbr_c',
            read_set_hash: 'read-2', realization_revision_ids: ['bjr_1'],
          },
        ],
      },
    })],
  }))
  const pins = screen.getByTestId('crosswalk-pins-cwd_1')
  expect(pins).toHaveTextContent('cwd_rev_1')
  expect(pins).toHaveTextContent('cwo_obs_1')
  // BOTH legs, with the mapping table appearing as the destination of one and the origin of the
  // other — which is what makes it readable as two hops rather than one.
  expect(pins).toHaveTextContent('deposits::public.accounts → deposits::public.acct_xref')
  expect(pins).toHaveTextContent('deposits::public.acct_xref → cards::public.cust')
  expect(pins).toHaveTextContent('bjr_1')
})

// ── the vocabulary that must never appear on a crosswalk row ────────────────────────────────────
//
// WHY A DOM-WIDE SCAN AND NOT THREE MORE `not.toHaveTextContent` LINES. The three negatives this
// file already carries (`/blocked|failed|invalid|error/i` and friends) are each pinned to one
// element and each catches a SUBSTITUTION — a sentence replaced by a worse one. None of them
// catches an ADDITION, which is the way this row will actually regress: appending "Approval
// unblocks it: confirm to enable, and 0 features depend on it today" to the crosswalk hint left
// every assertion in this file green, because every sentence they check for was still there.
//
// The list MIRRORS `overlay/upload/governance_queue.FORBIDDEN_PHRASES` (the server scans the
// payload with the same words; this scans what the screen renders out of it) plus the softer forms
// a crosswalk row is likeliest to grow. Two copies of a prohibition is one copy going stale, and
// the copy is unavoidable across the language boundary — so it is named here, in one place, rather
// than spread across the assertions.
const FORBIDDEN_ON_A_CROSSWALK_ROW = [
  // The server list, verbatim in meaning.
  /blocks\s+(n|\d+)\s+features?/i,
  /approve to enable/i,
  /waiting to become usable/i,
  /production approval required/i,
  /\bblocked\b/i,
  /sign-?off/i,
  // The softer forms. Each joins approval to capability, which is the one claim this row exists to
  // refuse: a review is accountability, and availability is automatic.
  /approval unblocks/i,
  /confirm to enable/i,
  /\bunblocks?\b/i,
  /approve to (unblock|run)/i,
  /pending approval/i,
  // TWO FAMILIES, not two more sentences. The list above is a list of PHRASES, and a phrase list is
  // beaten by rephrasing: "Not yet usable: once a reviewer verifies this mapping it becomes
  // runnable, and no features currently depend on it" contains none of them and asserts both lies
  // at once. These match the SHAPE instead.
  //
  // (a) any review verb wired to a capability verb wired to a capability adjective, within one
  //     sentence. The sentence bound (`[^.!?]`) is what keeps the honest copy legal: "A reviewer
  //     has confirmed this mapping." and "Running a crosswalk is not available yet, whatever its
  //     review says." are two sentences, and the row is allowed to say both.
  /\b(review\w*|verif\w*|endorse\w*|accept\w*|approv\w*|confirm\w*|sign-?off)\b[^.!?]{0,80}\b(becomes?|makes?|enables?|unlocks?|turns?)\b[^.!?]{0,40}\b(runnable|usable|available|executable|live)\b/i,
  // (b) the false zero SPELLED OUT. The numeric guard below catches "0 features"; this catches the
  //     same claim in words, which no digit scan can see. "What already depends on this: not
  //     tracked yet" is the honest render and matches neither half.
  /\b(no|zero)\s+(\w+\s+){0,2}features?\b[^.!?]{0,25}\bdepend|\bnothing\b[^.!?]{0,25}\bdepends?\b/i,
]

// A 0 is EARNED only where a store actually counted one: `state === 'counted'` renders through the
// `gq-usage-counted` class, and that is the single place a literal zero may legitimately appear.
// Everything else — "not tracked yet" rendered as 0, a fabricated dependency count — is the exact
// invitation to "approve it and things become usable" that the tri-state exists to remove.
function textOutsideCountedUsage(row: HTMLElement): string {
  const clone = row.cloneNode(true) as HTMLElement
  clone.querySelectorAll('.gq-usage-counted').forEach(node => node.remove())
  return clone.textContent ?? ''
}

it('renders no forbidden phrase anywhere on a crosswalk row, in any state', async () => {
  // EVERYTHING AT ONCE, so the scan covers every branch the row can take: measured with one
  // refused direction, all three families, an untracked dependency, both leg pins, the deployment
  // switch off. A phrase added to any of those blocks lands in this string.
  await openContext(contextFixture({
    relationships: [crosswalkRelationship({
      crosswalk: {
        ...crosswalkRelationship().crosswalk!,
        mapping_temporal_policy_revision_id: 'dtp_abc123',
        execution_enabled: false,
        admission_policy_version: 'crosswalk-admission-v1:abc',
        measurement: {
          observation_revision_id: 'cwo_1', scope_id: 'sandbox',
          observed_at: '2026-08-04T13:00:00Z', as_of: '2026-08-04', method: 'exact',
          row_coverage: 'full', complete: true, composed_row_count: 7,
          source_to_target_max_matches: 1, target_to_source_max_matches: 2,
          mapping_row_count: 3, mapping_temporal_policy_revision_id: 'dtp_abc123',
          caveats: ['mapping_temporal_policy_absent'], failures: [],
        },
        directions: [
          { direction: 'source_to_target', safety_status: 'deterministically_validated',
            cardinality: 'many_to_one', sandbox_admissible: true, production_admissible: true,
            reason_codes: ['deterministic_crosswalk_policy_satisfied'] },
          { direction: 'target_to_source', safety_status: 'unsafe', cardinality: 'one_to_many',
            sandbox_admissible: false, production_admissible: false,
            reason_codes: ['directional_crosswalk_fanout'] },
        ],
        unresolved_families: {
          directional_crosswalk_fanout: 'structurally_unsuitable',
          crosswalk_not_measured: 'needs_data_check',
          mapping_temporal_policy_unresolved: 'undecided',
        },
        already_depended_on_by: [{
          category: 'published_features', state: 'not_tracked_yet', count: null,
          display: 'not tracked yet', store: 'contract_metadata_dependency',
          basis: 'a registered feature whose contract depends on this crosswalk',
          reason: 'no crosswalk marker exists in that column',
        }],
        pinned_revisions: {
          crosswalk_definition_revision: 'cwd_rev_1', composed_observation_revision: 'cwo_obs_1',
        },
        leg_pins: [
          {
            kind: 'same_catalog', plan_hash: 'plan-1',
            from_dataset_ref: 'deposits::public.accounts',
            to_dataset_ref: 'deposits::public.acct_xref',
            from_binding_revision_id: 'pbr_a', to_binding_revision_id: 'pbr_b',
            read_set_hash: 'read-1',
          },
          {
            kind: 'cross_catalog', plan_hash: 'plan-2',
            from_dataset_ref: 'deposits::public.acct_xref',
            to_dataset_ref: 'cards::public.cust',
            from_binding_revision_id: 'pbr_b', to_binding_revision_id: 'pbr_c',
            read_set_hash: 'read-2', realization_revision_ids: ['bjr_1'],
          },
        ],
      },
    })],
  }))
  const row = screen.getByTestId('context-link-cwd_1')
  // The lineage block is a <details>: its contents are in the DOM whether or not it is open, and
  // `textContent` reads them — which is what makes this a scan of the whole row.
  const rendered = row.textContent ?? ''
  expect(rendered).toContain('acct_xref')          // the fixture actually rendered

  for (const forbidden of FORBIDDEN_ON_A_CROSSWALK_ROW) {
    expect(rendered).not.toMatch(forbidden)
  }
  // And no zero anybody has not earned. "Nothing uses this" and "no store records who uses this"
  // are different claims, and only the second is true here.
  expect(textOutsideCountedUsage(row)).not.toMatch(/\b0\b/)
})

it('keeps the scan honest when a store HAS counted a dependency', async () => {
  // The other side of the zero guard: a counted 0 is a real answer from a real store, and refusing
  // to render it would make the guard a rule about digits rather than about unearned claims. Also
  // proves the exemption is scoped — the forbidden-phrase scan still runs over the whole row.
  await openContext(contextFixture({
    relationships: [crosswalkRelationship({
      crosswalk: {
        ...crosswalkRelationship().crosswalk!,
        already_depended_on_by: [{
          category: 'published_features', state: 'counted', count: 0,
          display: '0', store: 'contract_metadata_dependency',
          basis: 'a registered feature whose contract depends on this crosswalk',
          reason: '',
        }],
      },
    })],
  }))
  const row = screen.getByTestId('context-link-cwd_1')
  expect(screen.getByTestId('crosswalk-depends-cwd_1')).toHaveTextContent('0')
  expect(textOutsideCountedUsage(row)).not.toMatch(/\b0\b/)
  for (const forbidden of FORBIDDEN_ON_A_CROSSWALK_ROW) {
    expect(row.textContent ?? '').not.toMatch(forbidden)
  }
})

// ── the table this column belongs to (Task 9) ────────────────────────────────────────────────────

const TABLE_SHAPE: api.ContextValue[] = [
  {
    field: 'table_role', value: 'dimension', proposed_value: null,
    resolution_status: 'current', operational_influence: null,
    authority_label: 'llm_proposed', producer: 'llm', strength: 'proposed',
    lifecycle: 'active', evidence_ids: ['ev-tr'],
  },
  {
    field: 'event_or_snapshot', value: 'snapshot', proposed_value: null,
    resolution_status: 'current', operational_influence: null,
    authority_label: 'source_attested', producer: 'source', strength: 'attested',
    lifecycle: 'active', evidence_ids: ['ev-eos'],
  },
]

it('shows the table role and the table shape, each with who said it', async () => {
  // The same amount column means different things on an event table and on a daily snapshot, so a
  // reviewer cannot judge the column without them. They rode `context.table_context` from the day
  // the section shipped and the screen rendered none of it.
  await openContext(contextFixture({ table_context: TABLE_SHAPE }))
  const role = screen.getByTestId('context-table-role')
  expect(role).toHaveTextContent('Table role')
  expect(role).toHaveTextContent('dimension')
  expect(within(role).getByText('AI proposed')).toBeInTheDocument()
  const shape = screen.getByTestId('context-event-or-snapshot')
  expect(shape).toHaveTextContent('snapshot')
  // A source-attested axis must not read like the AI-proposed one beside it.
  expect(within(shape).getByText('source attested')).toBeInTheDocument()
})

it('says the table shape is not established rather than leaving the row blank', async () => {
  await openContext(contextFixture({ table_context: [] }))
  expect(screen.getByTestId('context-table-role')).toHaveTextContent(/not established/i)
  expect(screen.getByTestId('context-event-or-snapshot')).toHaveTextContent(/not established/i)
})

it('renders no table section for an anchor whose payload carries no table context', async () => {
  await openContext(contextFixture())
  expect(screen.queryByTestId('context-table-shape')).toBeNull()
})

it('does not name an author for an axis that resolved to nothing', async () => {
  // The fourth state, and the one nobody lists: the bundle CARRIES the field because evidence rows
  // exist for it, but nothing resolved and nothing is proposed (`semantic_context` keeps a
  // table value whenever `value is None and not entries` is false). Rendering an em dash beside a
  // "source attested" chip would name somebody as the author of nothing.
  await openContext(contextFixture({
    table_context: [{
      field: 'table_role', value: null, proposed_value: null,
      resolution_status: 'unresolved_pending_review', operational_influence: null,
      authority_label: 'source_attested', producer: 'source', strength: 'attested',
      lifecycle: 'active', evidence_ids: ['ev-tr'],
    }],
  }))
  const role = screen.getByTestId('context-table-role')
  expect(role).toHaveTextContent(/not established/i)
  expect(role.textContent).not.toContain('—')
  expect(within(role).queryByText('source attested')).toBeNull()
})

it('keeps the table’s search projection off the page', async () => {
  // `table_context` carries the table node's `semantic_terms` beside the two axes, and it is the
  // SPACE-joined search blob — term name, every glossary synonym, BIAN, FIBO and the process path
  // concatenated. It arrives with no evidence rows, so `display_label()` returns "system": the
  // platform named as author of text the source glossary wrote. It is index material, and this
  // section is not where index material goes.
  await openContext(contextFixture({
    table_context: [
      ...TABLE_SHAPE,
      {
        field: 'semantic_terms',
        value: 'Trade Amount notional consideration Payment Order fibo-fnd:MonetaryAmount',
        proposed_value: null, resolution_status: 'current', operational_influence: null,
        authority_label: 'system', producer: null, strength: null, lifecycle: null,
        evidence_ids: [],
      },
    ],
  }))
  const section = screen.getByTestId('context-table-shape')
  expect(section.textContent).not.toMatch(/fibo-fnd:MonetaryAmount/)
  expect(section.textContent).not.toMatch(/notional consideration/)
  expect(within(section).queryByText('system')).toBeNull()
  expect(screen.queryByTestId('context-value-semantic_terms')).toBeNull()
  // The axes beside it still render — the exclusion is one field, not the section.
  expect(screen.getByTestId('context-table-role')).toHaveTextContent('dimension')
})

it('shows the table prose that only the table node carries', async () => {
  await openContext(contextFixture({
    table_context: [
      ...TABLE_SHAPE,
      {
        field: 'ai_summary', value: 'One row per account per day.', proposed_value: null,
        resolution_status: 'current', operational_influence: null,
        authority_label: 'llm_proposed', producer: 'llm', strength: 'proposed',
        lifecycle: 'active', evidence_ids: [],
      },
    ],
  }))
  const section = screen.getByTestId('context-table-shape')
  expect(within(section).getByText('One row per account per day.')).toBeInTheDocument()
})

// ── a proposal that did not win resolution (Task 9) ──────────────────────────────────────────────

it('renders the AI’s answer for a field it was never allowed to resolve', async () => {
  // `field_policies._MEASURE_ANNOTATION` keeps the LLM out of resolving unit/currency, so
  // `graph_node` never receives its answer and `value` is null by design. Rendering an em dash for
  // that column tells a reader nobody has an answer, which is false.
  await openContext(contextFixture({
    resolved_meaning: [{
      field: 'unit', value: null, proposed_value: 'dollars',
      resolution_status: 'unresolved_pending_review', operational_influence: 'hint',
      authority_label: 'llm_proposed', producer: 'llm', strength: 'proposed',
      lifecycle: 'active', evidence_ids: ['ev-unit'],
    }],
  }))
  const row = screen.getByTestId('context-value-unit')
  expect(within(row).getByText('dollars')).toBeInTheDocument()
  // Distinguishable from a resolved value at the point it is read.
  expect(within(row).getByText('AI proposed · unconfirmed')).toBeInTheDocument()
  expect(row.textContent).not.toMatch(/blocked|invalid|failed|error/i)
})

it('names the model as the author of the model’s own proposal, not the strongest record', async () => {
  // `proposed_value` is ALWAYS the LLM's row (`semantic_context` keys the lookup on
  // `EvidenceProducer.LLM`), but `authority_label` is the STRONGEST active entry — a different
  // record whenever a field resolved to nothing and two producers proposed at equal strength, where
  // the lead is settled by `evidence_id` order. Reading the chip off `authority_label` displayed the
  // model's value under somebody else's name.
  await openContext(contextFixture({
    resolved_meaning: [{
      field: 'unit', value: null, proposed_value: 'dollars',
      resolution_status: 'unresolved_pending_review', operational_influence: 'hint',
      // The taxonomy row won the lead on evidence_id; the VALUE on screen is still the model's.
      authority_label: 'source_proposed', producer: 'taxonomy', strength: 'proposed',
      lifecycle: 'active', evidence_ids: ['ev-a', 'ev-b'],
    }],
  }))
  const row = screen.getByTestId('context-value-unit')
  expect(within(row).getByText('dollars')).toBeInTheDocument()
  expect(within(row).getByText('AI proposed · unconfirmed')).toBeInTheDocument()
  expect(within(row).queryByText(/source proposed/)).toBeNull()
  // The strongest record is not hidden — it is named as what it is, in the tooltip.
  expect(within(row).getByTitle(/strongest active record taxonomy\/proposed\/active/))
    .toBeInTheDocument()
})

it('never lets a stale proposal displace a value the platform resolved', async () => {
  await openContext(contextFixture({
    resolved_meaning: [{
      field: 'unit', value: 'USD', proposed_value: 'AED',
      resolution_status: 'current', operational_influence: 'governed',
      authority_label: 'human', producer: 'human', strength: 'confirmed',
      lifecycle: 'active', evidence_ids: ['ev-unit'],
    }],
  }))
  const row = screen.getByTestId('context-value-unit')
  expect(within(row).getByText('USD')).toBeInTheDocument()
  expect(within(row).queryByText('AED')).toBeNull()
  expect(within(row).queryByText(/unconfirmed/)).toBeNull()
  expect(within(row).getByText('human confirmed')).toBeInTheDocument()
})

// ── how sure the adjudicator was (Task 9) ────────────────────────────────────────────────────────

it('shows the adjudicator’s confidence as explanation, never as authority', async () => {
  await openContext(contextFixture(), d => {
    d.semantic_adjudication = {
      status: 'available', structured_result_id: 'sr-1', selected_concept: 'monetary_stock',
      alternatives: ['monetary_flow', 'balance'], confidence_band: 'low',
      reason_codes: [], missing_context: [], ontology_gap: null,
    }
  })
  const band = screen.getByTestId('context-confidence-band')
  expect(band).toHaveTextContent(/low confidence/i)
  // It EXPLAINS the reading; it is not a claim that anybody attested it. Said positively, because
  // the assertion this replaced (`not.toMatch(/authorit(y|ative)\b(?!.*not)/i)`) had a lookahead
  // over the whole remaining string and passed on any later "not" anywhere in the row.
  expect(band).toHaveTextContent(/how sure it was, not who says it is right/i)
  // And it lives inside the adjudication section, under the heading that frames the whole block as
  // alternatives considered — never beside the value as if it were the value's authority.
  expect(within(screen.getByTestId('context-adjudication')).getByTestId('context-confidence-band'))
    .toBeInTheDocument()
  expect(band.textContent).not.toMatch(/blocked|invalid|failed|error/i)
})

it('says the adjudicator recorded no confidence rather than implying certainty', async () => {
  // A reading with no band must not render as a reading nobody doubted. Absence is spoken.
  await openContext(contextFixture(), d => {
    d.semantic_adjudication = {
      status: 'available', structured_result_id: 'sr-2', selected_concept: 'monetary_stock',
      alternatives: [], reason_codes: [], missing_context: [], ontology_gap: null,
    }
  })
  expect(screen.getByTestId('context-confidence-band'))
    .toHaveTextContent(/recorded no confidence/i)
})

// ── cardinality: a hop nobody has established (Task 9) ───────────────────────────────────────────

it('says a link with no directional realization has no established cardinality', async () => {
  await openContext(contextFixture({ relationships: [directRelationship()] }))
  const note = screen.getByTestId('context-cardinality-bfk_1')
  expect(note).toHaveTextContent(/cardinality not established/i)
  // "Nobody has measured it" is a state, not a fault — and never a claim a review would change it.
  expect(note.textContent).not.toMatch(/blocked|invalid|failed|error/i)
  for (const forbidden of FORBIDDEN_ON_A_CROSSWALK_ROW) {
    expect(note.textContent ?? '').not.toMatch(forbidden)
  }
})

it('states a realization’s missing cardinality as not established, never as a blank', async () => {
  await openContext(contextFixture({
    relationships: [{
      ...directRelationship(),
      realizations: [{
        realization_revision_id: 'brr_1', from_ref: 'deposits::public.accounts',
        to_ref: 'cards::public.cust', lifecycle: 'active', safety_status: 'unmeasured',
        cardinality: null, scope_id: null, sandbox_eligible: false,
        production_eligible: false, executable_now: false,
      }],
    }],
  }))
  expect(screen.getByTestId('context-link-bfk_1')).toHaveTextContent(/cardinality not established/i)
  // The link-level note belongs to links with NO realization at all; this one has one.
  expect(screen.queryByTestId('context-cardinality-bfk_1')).toBeNull()
})

// ── the authority badge's colour agrees with its words HERE TOO ───────────────────────────────────

it('gives the Context tab the same authority tones as the rest of the dossier', async () => {
  // REPORTED after the tone rule landed: "still the same issue at many places". The rule was
  // applied to the axis rows, the header chip and the glossary card, and this row was missed — it
  // set a tone ONLY for the proposal-only case and otherwise fell through to a bare `.badge`,
  // which renders neutral grey whatever the label says. So "source attested" was green on
  // Metadata & evidence and grey here.
  //
  // My verification missed it too: I measured 44 badges on the OVERVIEW tab and reported "zero
  // inconsistent" without saying the page has six tabs. Sweeping all six found it in seconds.
  await openContext(contextFixture())
  const attested = screen.getAllByText('source attested')[0]
  expect(attested).toHaveClass('gj-verified')
  const proposed = screen.getAllByText('source proposed')[0]
  expect(proposed).toHaveClass('gj-proposed')
})
