import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { AssetDetailScreen } from './AssetDetailScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getAssetDetail: vi.fn(), postFieldDecision: vi.fn() }
})
const getAssetDetail = vi.mocked(api.getAssetDetail)
const postFieldDecision = vi.mocked(api.postFieldDecision)

// A column asset covering the honesty properties: declared vs operational type; three metadata
// fields whose AUTHORITY differs from whether a value is present (governed/hint/missing all carry a
// non-empty value); verified relationships alongside a proposed candidate; a mixed readiness matrix;
// history + audit; and exactly ONE server-returned field-correction command (currency) so the drawer
// appears for that field and no other.
function fixture(): api.AssetDetail {
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
          provenance: 'source_declared', selected_evidence_ids: ['ev-c1'],
        },
        // hint: value present but only LLM-proposed — authority is NOT 'governed'.
        entity: {
          value: 'Account', authority: 'hint', c1_status: 'proposed',
          provenance: 'llm_proposed', selected_evidence_ids: [],
        },
        // missing: value present but NOTHING attested it — must read as unattested, not "present".
        unit: {
          value: 'dollars', authority: 'missing', c1_status: 'none',
          provenance: null, selected_evidence_ids: [],
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

beforeEach(() => {
  getAssetDetail.mockReset()
  getAssetDetail.mockResolvedValue({ detail: fixture(), etag: 'etag-1' })
  postFieldDecision.mockReset()
  postFieldDecision.mockResolvedValue({
    field: 'currency', action: 'confirm_existing', outcome: 'confirmed', replayed: false,
    projected: true, latest_decision_id: 'dec-2', evidence_set_hash: 'hash-2',
    policy_version: 'pol-1', actions: ['reject'],
  })
})

function renderScreen() {
  return render(<AssetDetailScreen source="deposits" objectRef="public.accounts.balance" />)
}

// The AuthorityBadge span carrying `label` at the given governance tone (badge + tone class).
function authorityChip(label: string, tone: string): HTMLElement | undefined {
  return screen.getAllByText(label).find(
    el => el.classList.contains('badge') && el.classList.contains(tone),
  )
}

describe('asset detail — tabs + identity', () => {
  it('renders the 5 section tabs and loads from getAssetDetail', async () => {
    renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    for (const label of [
      'Overview', 'Metadata & evidence', 'Relationships', 'Readiness', 'History',
    ]) {
      expect(screen.getByRole('button', { name: label })).toBeInTheDocument()
    }
    expect(getAssetDetail).toHaveBeenCalledWith('deposits', 'public.accounts.balance')
  })

  it('overview shows the two-type honesty — declared vs operational, only a technical source attests', async () => {
    renderScreen()
    // declared "double" but operational "unknown": the declared type is never evidence of numeric.
    expect(await screen.findByText('double')).toBeInTheDocument()
    expect(screen.getByText('unknown')).toBeInTheDocument()
    expect(screen.getByText(/only a technical source/i)).toBeInTheDocument()
    expect(
      screen.getByText(/never on its own evidence a column is operationally numeric/i),
    ).toBeInTheDocument()
  })
})

describe('asset detail — authority rendered from the response, never from the value', () => {
  it('drives each field badge from authority/provenance, not from the value being non-empty', async () => {
    renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Metadata & evidence' }))

    // All three fields carry a NON-EMPTY value, yet their badges differ purely by authority:
    // governed → "source declared" (verified tone); hint → "llm proposed" (proposed tone);
    // missing → "unattested" (quiet tone) even though the value "dollars" is present.
    expect(await screen.findAllByText('USD')).not.toHaveLength(0)
    expect(screen.getByText('Account')).toBeInTheDocument()
    expect(screen.getByText('dollars')).toBeInTheDocument()

    expect(authorityChip('source declared', 'gj-verified')).toBeTruthy()
    expect(authorityChip('llm proposed', 'gj-proposed')).toBeTruthy()
    // value present but authority "missing" → unattested, NOT a present/governed state.
    expect(authorityChip('unattested', 'gj-none')).toBeTruthy()
  })
})

describe('asset detail — relationships: verified distinct from proposed', () => {
  it('renders verified joins/edges distinctly from proposed candidates + a nonblank neighborhood graph', async () => {
    const { container } = renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Relationships' }))

    // Distinct sections: a Verified subsection and a Proposed candidates subsection.
    expect(await screen.findByText('Verified')).toBeInTheDocument()
    expect(screen.getByText('Proposed candidates')).toBeInTheDocument()

    // The verified join renders with a VERIFIED chip; the candidate with a proposed chip.
    expect(screen.getByText('accounts.balance → customers.id')).toBeInTheDocument()
    expect(authorityChip('VERIFIED', 'gj-verified')).toBeTruthy()
    expect(authorityChip('currency_binding', 'gj-proposed')).toBeTruthy()

    // The inline SVG graph is nonblank (anchor present) and draws verified vs proposed edges with
    // visually distinct classes.
    const graph = screen.getByRole('img', { name: /neighborhood graph/i })
    expect(graph).toBeInTheDocument()
    expect(container.querySelector('.adg-node--anchor')).toBeTruthy()
    expect(container.querySelector('.adg-edge--verified')).toBeTruthy()
    expect(container.querySelector('.adg-edge--proposed')).toBeTruthy()
    // The a11y parallel list distinguishes verified from proposed for non-visual readers.
    const a11y = container.querySelector('.adg-graph-a11y')
    expect(a11y?.textContent).toMatch(/verified/)
    expect(a11y?.textContent).toMatch(/proposed/)
  })

  it('shows an unavailable semantic subsection honestly, never as an empty-success', async () => {
    const detail = fixture()
    detail.relationships!.semantic = { status: 'unavailable' }
    // The backend names the withheld semantic subsection 'relationships.semantic' (the real wire),
    // not a bare 'semantic'.
    detail.unavailable_sections = ['relationships.semantic', 'audit']
    getAssetDetail.mockResolvedValue({ detail, etag: 'etag-1' })
    renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Relationships' }))
    expect(await screen.findByText(/semantic links are not available/i)).toBeInTheDocument()
    // Not a "no semantic links" empty-success.
    expect(screen.queryByText('Proposed candidates')).not.toBeInTheDocument()
  })

  it('graph draws the NON-anchor endpoint as the neighbor for an inbound (anchor-as-to_ref) join', async () => {
    const detail = fixture()
    // an INBOUND join: the anchor (public.accounts.balance) is the to_ref; the REAL counterparty is
    // the from_ref. The backend returns edges where the anchor is EITHER endpoint, so the graph must
    // pick the end that is NOT the anchor — else it draws the anchor as its own neighbor.
    detail.relationships!.approved_joins = [{
      from_ref: 'public.orders.account_id', to_ref: 'public.accounts.balance',
      cardinality: 'N:1', status: 'VERIFIED', approved_join_fact_key: 'ajk-in',
    }]
    // isolate the join: drop the verified semantic edge + candidate so only the join drives the graph.
    detail.relationships!.semantic = {
      status: 'available', verified_edges: [], candidates: [], divergences: [],
    }
    getAssetDetail.mockResolvedValue({ detail, etag: 'etag-1' })
    const { container } = renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Relationships' }))
    await screen.findByRole('img', { name: /neighborhood graph/i })

    // the a11y list names the REAL counterparty (the from_ref) — the non-anchor end is chosen.
    const a11y = container.querySelector('.adg-graph-a11y')!
    expect(a11y.textContent).toContain('orders.account_id')

    // the anchor label is drawn EXACTLY ONCE (as the anchor node), never also as its own neighbor.
    const nodeLabels = Array.from(container.querySelectorAll('.adg-node-label'))
      .map(el => el.textContent)
    expect(nodeLabels.filter(t => t === 'accounts.balance')).toHaveLength(1)
    expect(nodeLabels).toContain('orders.account_id')
  })

  it('derives a verified-list row tone from its OWN status field, not from list membership', async () => {
    const detail = fixture()
    // a row the backend returned in the verified list but whose status is NOT VERIFIED must read as
    // PARTIAL (badge + border), never hardcoded verified — authority is a response fact, not a
    // position in a list.
    detail.relationships!.approved_joins = [{
      from_ref: 'public.accounts.balance', to_ref: 'public.customers.id',
      cardinality: 'N:1', status: 'PARTIALLY_CONFIRMED', approved_join_fact_key: 'ajk-p',
    }]
    detail.relationships!.semantic = {
      status: 'available', verified_edges: [], candidates: [], divergences: [],
    }
    getAssetDetail.mockResolvedValue({ detail, etag: 'etag-1' })
    const { container } = renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Relationships' }))

    // badge tone derived from status → partial, never verified.
    expect(authorityChip('PARTIALLY_CONFIRMED', 'gj-partial')).toBeTruthy()
    expect(authorityChip('PARTIALLY_CONFIRMED', 'gj-verified')).toBeFalsy()
    // row border derived from status → partial; and the graph edge draws as proposed, not verified.
    expect(container.querySelector('.adg-rel-partial')).toBeTruthy()
    expect(container.querySelector('.adg-edge--proposed')).toBeTruthy()
    expect(container.querySelector('.adg-edge--verified')).toBeFalsy()
  })
})

describe('asset detail — readiness matrix', () => {
  it('summarizes the capability matrix from the real statuses (2 / 5 ready)', async () => {
    renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Readiness' }))
    // as_measure + as_join_key ready; as_entity_key + as_grain_key blocked; as_event_time unavailable.
    expect(await screen.findByText(/2 \/ 5 ready/)).toBeInTheDocument()
    expect(screen.getByText(/2 blocked/)).toBeInTheDocument()
    expect(screen.getByText('as measure')).toBeInTheDocument()
    expect(screen.getByText(/no entity assignment/)).toBeInTheDocument()
  })
})

describe('asset detail — history + audit honesty', () => {
  it('renders runs + stages and shows audit as not available when it is gated out', async () => {
    renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'History' }))
    expect(await screen.findByText('run-1')).toBeInTheDocument()
    expect(screen.getByText('parse')).toBeInTheDocument()
    // audit is absent + named in unavailable_sections → honest "not available", never invented.
    expect(screen.getByText(/audit summaries are not available/i)).toBeInTheDocument()
  })
})

describe('asset detail — correction drawer (OCC + 409)', () => {
  it('offers the drawer ONLY for a field the server returned a command for', async () => {
    renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Metadata & evidence' }))
    // Exactly one Correct… button (currency); entity + unit are read-only (no server command).
    const correctButtons = await screen.findAllByRole('button', { name: /^correct/i })
    expect(correctButtons).toHaveLength(1)
    expect(screen.getAllByText(/read-only — the server returned no correction command/i)).toHaveLength(2)
  })

  it('echoes the OCC CAS triple + idempotency key + honesty copy in the drawer', async () => {
    renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Metadata & evidence' }))
    await userEvent.click(await screen.findByRole('button', { name: /^correct/i }))

    const drawer = screen.getByRole('complementary', { name: /correct currency/i })
    expect(within(drawer).getByText(
      /stage correction creates a new human evidence layer, it does not rewrite the source/i,
    )).toBeInTheDocument()
    // The CAS anchor the field was loaded at, echoed for auditability.
    expect(within(drawer).getByText('etag-1')).toBeInTheDocument()
    expect(within(drawer).getByText('dec-1')).toBeInTheDocument()
    expect(within(drawer).getByText('hash-1')).toBeInTheDocument()
    expect(within(drawer).getByText('pol-1')).toBeInTheDocument()
    expect(within(drawer).getByText('idempotency key')).toBeInTheDocument()
  })

  it('submits the correction with the CAS triple + idempotency key and reloads on success', async () => {
    renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Metadata & evidence' }))
    await userEvent.click(await screen.findByRole('button', { name: /^correct/i }))
    await userEvent.click(screen.getByRole('button', { name: /stage correction/i }))

    expect(postFieldDecision).toHaveBeenCalledWith(
      'deposits', 'public.accounts.balance', 'currency',
      expect.objectContaining({
        action: 'confirm_existing',
        selectedEvidenceIds: ['ev-c1'],
        replacementValue: null,
        expectedLatestDecisionId: 'dec-1',
        expectedEvidenceSetHash: 'hash-1',
        expectedPolicyVersion: 'pol-1',
        idempotencyKey: expect.any(String),
      }),
    )
    // Success reloads the asset to its fresh evidence/decision state.
    await waitFor(() => expect(getAssetDetail).toHaveBeenCalledTimes(2))
    expect(await screen.findByText(/it created a new human evidence layer/i)).toBeInTheDocument()
  })

  it('on 409 reloads the asset and tells the user to re-review — never a silent retry', async () => {
    postFieldDecision.mockRejectedValue(
      new api.ApiError(409, 'Changed since you loaded it — refresh.'),
    )
    renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Metadata & evidence' }))
    await userEvent.click(await screen.findByRole('button', { name: /^correct/i }))
    await userEvent.click(screen.getByRole('button', { name: /stage correction/i }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/the data changed since you loaded it/i)
    expect(alert).toHaveTextContent(/re-review/i)
    // Reloaded (initial + 409 reload = 2) and the command was issued exactly once (no blind retry).
    await waitFor(() => expect(getAssetDetail).toHaveBeenCalledTimes(2))
    expect(postFieldDecision).toHaveBeenCalledTimes(1)
  })
})
