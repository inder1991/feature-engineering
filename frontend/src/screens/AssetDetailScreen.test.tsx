import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { AssetDetailScreen } from './AssetDetailScreen'
import { fixture, suggestionsFixture } from './AssetDetailScreen.fixture'

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
const postFieldDecision = vi.mocked(api.postFieldDecision)
const getTableSuggestions = vi.mocked(api.getTableSuggestionsV4)

beforeEach(() => {
  getAssetDetail.mockReset()
  getAssetDetail.mockResolvedValue({ detail: fixture(), etag: 'etag-1' })
  postFieldDecision.mockReset()
  postFieldDecision.mockResolvedValue({
    field: 'currency', action: 'confirm_existing', outcome: 'confirmed', replayed: false,
    projected: true, latest_decision_id: 'dec-2', evidence_set_hash: 'hash-2',
    policy_version: 'pol-1', actions: ['reject'],
  })
  getTableSuggestions.mockReset()
  getTableSuggestions.mockResolvedValue(suggestionsFixture())
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

  it('type policy: a declared type backs the display — never a bare "unknown"', async () => {
    renderScreen()
    // declared "double", operational unknown → the headline is `double · declared` (basis chip);
    // the word "unknown" appears NOWHERE, because the platform HOLDS a type — the file declared it.
    const typeLine = await screen.findByTestId('type-display')
    expect(typeLine).toHaveTextContent('double')
    expect(typeLine).toHaveTextContent('declared')
    expect(screen.queryByText('unknown')).toBeNull()
    // the operational slot states its honest gap without the bare word.
    expect(screen.getByText('— not attested yet')).toBeInTheDocument()
    expect(screen.getByText(/only a technical source/i)).toBeInTheDocument()
    expect(
      screen.getByText(/never on its own evidence a column is operationally numeric/i),
    ).toBeInTheDocument()
  })

  it('renders bare "unknown" ONLY when nothing at all is held', async () => {
    const detail = fixture()
    detail.identity.declared_type = null
    detail.identity.operational_type = 'unknown'
    getAssetDetail.mockResolvedValue({ detail, etag: 'etag-1' })
    renderScreen()
    const typeLine = await screen.findByTestId('type-display')
    expect(typeLine).toHaveTextContent('unknown')
    expect(typeLine).not.toHaveTextContent('declared')
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

  it('falls back to the evidence author when there is no decision, only "unattested" when truly nothing', async () => {
    getAssetDetail.mockResolvedValue({
      detail: {
        ...fixture(),
        effective_metadata: { fields: {
          concept: { value: 'monetary_flow', authority: 'hint', c1_status: 'no_decision',
                     provenance: null, evidence_provenance: 'AI proposed', selected_evidence_ids: [] },
          unit:    { value: null, authority: 'missing', c1_status: 'no_decision',
                     provenance: null, evidence_provenance: null, selected_evidence_ids: [] },
        } },
      },
      etag: 'etag-1',
    })
    renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Metadata & evidence' }))
    expect(authorityChip('AI proposed', 'gj-proposed')).toBeTruthy()   // known author, not "unattested"
    // `unit` has no value, so it now sits in the collapsed "not set" group — a field nobody has set
    // is not the story on the page. Expand it: the assertion is about the LABEL being honest, and
    // that property is unchanged by where the row lives.
    await userEvent.click(screen.getByRole('button', { name: /not set/i }))
    expect(authorityChip('unattested', 'gj-none')).toBeTruthy()        // genuinely nothing
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

  it('containment is a one-line summary, not a dump of every sibling column', async () => {
    const detail = fixture()
    detail.relationships!.containment = {
      table: {
        object_ref: 'public.comp_financial_tran_repos_dly',
        table: 'comp_financial_tran_repos_dly',
      },
      columns: [
        {
          object_ref: 'public.t.actual_tran_amt', column: 'actual_tran_amt',
          data_type: 'unknown', sensitivity: null,
        },
        { object_ref: 'public.t.cif_id', column: 'cif_id', data_type: 'unknown', sensitivity: null },
      ],
    }
    getAssetDetail.mockResolvedValue({ detail, etag: 'etag-1' })
    renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Relationships' }))
    expect(await screen.findByText(/2 other columns/)).toBeInTheDocument()   // the summary line
    expect(screen.queryByText('actual_tran_amt')).not.toBeInTheDocument()    // NOT dumped as a row
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
  it('leads with what the column is USABLE for, naming each role in plain words', async () => {
    // Was: "2 / 5 ready · 2 blocked" over `as measure` / `no entity assignment`. That framing read
    // as failure for a catalog behaving exactly as designed, and "blocked" is not a state this
    // product has — it uses AI-proposed values whether or not a human has reviewed them.
    renderScreen()
    await screen.findByRole('group', { name: /asset sections/i })
    await userEvent.click(screen.getByRole('button', { name: 'Readiness' }))
    expect(await screen.findByText(/Usable for 3 of 5 roles/)).toBeInTheDocument()
    expect(screen.getByText('Measure')).toBeInTheDocument()
    expect(screen.getByText('AI proposed')).toBeInTheDocument()
    expect(screen.queryByText(/blocked/i)).toBeNull()
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
    // A read-only field now shows NO affordance at all. The old copy — "read-only, the server
    // returned no correction command for this field" — was implementation talk aimed at the wrong
    // audience, repeated on every such row. Absence is the clearer statement, and asserting the
    // absence is a stronger contract than asserting the apology.
    expect(screen.queryByText(/returned no correction command/i)).toBeNull()
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
