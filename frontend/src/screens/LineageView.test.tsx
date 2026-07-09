import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { LineageView } from './LineageView'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, lineageGraph: vi.fn() }
})
const lineageGraph = vi.mocked(api.lineageGraph)

beforeEach(() => {
  lineageGraph.mockReset()
})

const ANCHOR: api.SearchHit = {
  object_ref: 'public.accounts.balance', table: 'accounts', column: 'balance', kind: 'column',
  data_type: 'numeric', definition: 'end-of-day ledger balance', is_grain: false, is_as_of: false,
  catalog_source: 'deposits', concept: null, domain: null, sensitivity: null,
  additivity: 'semi_additive', unit: 'dollars', currency: 'USD', entity: 'Account', score: 1.2,
}

// Wire-shape helpers: optional keys OMITTED when absent, exactly like the endpoint.
function tbl(source: string, table: string, stale = false): api.LineageNode {
  return {
    id: `${source}:public.${table}`, kind: 'table', object_ref: `public.${table}`, table,
    catalog_source: source, grain: false, as_of: false, stale, resolved: true,
  }
}
function col(
  source: string, table: string, column: string,
  extra: Partial<api.LineageNode> = {},
): api.LineageNode {
  return {
    id: `${source}:public.${table}.${column}`, kind: 'column',
    object_ref: `public.${table}.${column}`, table, column, catalog_source: source,
    grain: false, as_of: false, stale: false, resolved: true, ...extra,
  }
}
function contains(source: string, table: string, column: string): api.LineageEdge {
  return {
    from: `${source}:public.${table}`, to: `${source}:public.${table}.${column}`,
    layer: 'joins', kind: 'contains', resolved: true,
  }
}

// The canned depth-1 graph around deposits:public.accounts.balance, mirroring the contract:
// two verified joins (one reverse-traversed, fan inverted), one declared join to a stub that
// is not uploaded yet, and the feature lineage chain to a consumer.
const BASE: api.LineageGraph = {
  nodes: [
    tbl('deposits', 'accounts'),
    col('deposits', 'accounts', 'id', { grain: true }),
    col('deposits', 'accounts', 'posted_at', { as_of: true }),
    col('deposits', 'accounts', 'balance'),
    col('deposits', 'accounts', 'cust_id'),
    col('deposits', 'accounts', 'ledger_id'),
    tbl('deposits', 'customers'),
    col('deposits', 'customers', 'cust_id', { grain: true, entity: 'Customer' }),
    col('deposits', 'customers', 'email', { sensitivity: 'pii' }),
    tbl('deposits', 'transactions'),
    col('deposits', 'transactions', 'txn_id', { grain: true }),
    col('deposits', 'transactions', 'account_id'),
    { // pending stub: declared join target, NO catalog_source key
      id: 'deposits:public.ledger.entry_id', kind: 'column',
      object_ref: 'public.ledger.entry_id', table: 'ledger', column: 'entry_id',
      grain: false, as_of: false, stale: false, resolved: false,
    },
    {
      id: 'feature:feat_01HZX', kind: 'feature', feature_id: 'feat_01HZX',
      name: 'avg_eod_balance_30d', grain: false, as_of: false, stale: false, resolved: true,
    },
    {
      id: 'consumer:churn_risk_model', kind: 'consumer', name: 'churn_risk_model',
      grain: false, as_of: false, stale: false, resolved: true,
    },
  ],
  edges: [
    contains('deposits', 'accounts', 'id'),
    contains('deposits', 'accounts', 'posted_at'),
    contains('deposits', 'accounts', 'balance'),
    contains('deposits', 'accounts', 'cust_id'),
    contains('deposits', 'accounts', 'ledger_id'),
    contains('deposits', 'customers', 'cust_id'),
    contains('deposits', 'customers', 'email'),
    contains('deposits', 'transactions', 'txn_id'),
    contains('deposits', 'transactions', 'account_id'),
    {
      from: 'deposits:public.accounts.cust_id', to: 'deposits:public.customers.cust_id',
      layer: 'joins', kind: 'join', cardinality: 'N:1', resolved: true,
    },
    { // reverse traversal: fan inverted (M7)
      from: 'deposits:public.accounts.id', to: 'deposits:public.transactions.account_id',
      layer: 'joins', kind: 'join', cardinality: '1:N', resolved: true,
    },
    { // declared, target not uploaded, no cardinality
      from: 'deposits:public.accounts.ledger_id', to: 'deposits:public.ledger.entry_id',
      layer: 'joins', kind: 'join', resolved: false,
    },
    {
      from: 'deposits:public.accounts.balance', to: 'feature:feat_01HZX',
      layer: 'features', kind: 'derives', resolved: true,
    },
    {
      from: 'feature:feat_01HZX', to: 'consumer:churn_risk_model',
      layer: 'features', kind: 'consumes', resolved: true,
    },
  ],
  truncated: false,
}

// The cards catalog reached over an entity bridge: a STALE source, shown and marked.
const CARDS_NODES: api.LineageNode[] = [
  tbl('cards', 'card_holders', true),
  { ...col('cards', 'card_holders', 'holder_id', { entity: 'Customer' }), stale: true },
]
const BRIDGE: api.LineageEdge = {
  from: 'deposits:public.customers.cust_id', to: 'cards:public.card_holders.holder_id',
  layer: 'entity', kind: 'entity_bridge', resolved: false,
}
const WITH_CARDS: api.LineageGraph = {
  nodes: [...BASE.nodes, ...CARDS_NODES],
  edges: [...BASE.edges, contains('cards', 'card_holders', 'holder_id'), BRIDGE],
  truncated: false,
}

describe('lineage view', () => {
  it('renders table cards with source lines, column flags, and the anchor match highlight', async () => {
    lineageGraph.mockResolvedValue(BASE)
    render(<LineageView anchor={ANCHOR} />)
    expect(await screen.findByText('accounts')).toBeInTheDocument()
    expect(lineageGraph).toHaveBeenCalledWith('public.accounts.balance', 'deposits', {
      direction: 'both', depth: 1,
    })
    expect(screen.getByText('customers')).toBeInTheDocument()
    expect(screen.getByText('transactions')).toBeInTheDocument()
    // fresh sources say so on every card
    expect(screen.getAllByText('fresh').length).toBeGreaterThanOrEqual(3)
    // column flags: grain, as-of, pii (visible because the wire included the column)
    expect(screen.getAllByText('grain').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('as-of')).toBeInTheDocument()
    expect(screen.getByText('pii')).toBeInTheDocument()
    // the anchor column carries the match highlight
    expect(screen.getByRole('button', { name: 'balance' })).toHaveAttribute('aria-current', 'true')
    // feature and consumer nodes render with their marks (kind micro-label + feature flag)
    expect(screen.getByText('avg_eod_balance_30d')).toBeInTheDocument()
    expect(screen.getAllByText('feature').length).toBe(2)
    expect(screen.getByText('churn_risk_model')).toBeInTheDocument()
    expect(screen.getByText('reads 1 feature in view')).toBeInTheDocument()
    // the pending stub renders dashed-and-labeled as data, not an error
    expect(screen.getByText('public.ledger.entry_id')).toBeInTheDocument()
    expect(screen.getByText(/declared join target; not uploaded yet/i)).toBeInTheDocument()
  })

  it('shows stale sources greyed with a stale marker and not-vouched guidance', async () => {
    lineageGraph.mockResolvedValue(WITH_CARDS)
    const { container } = render(<LineageView anchor={ANCHOR} />)
    expect(await screen.findByText('card_holders')).toBeInTheDocument()
    expect(screen.getByText('stale')).toBeInTheDocument()
    expect(
      screen.getByText(/not currently vouched\. re-upload the cards source/i),
    ).toBeInTheDocument()
    expect(container.querySelector('.ln-card--stale')).not.toBeNull()
  })

  it('renders read-scoped responses as-is: a hidden pii column is simply absent', async () => {
    // Read-scope is enforced server-side; the node is ABSENT from the wire, so nothing in the
    // canvas, drawer, or edge list may invent it.
    const scrubbed: api.LineageGraph = {
      nodes: BASE.nodes.filter(n => n.id !== 'deposits:public.customers.email'),
      edges: BASE.edges.filter(e => e.to !== 'deposits:public.customers.email'),
      truncated: false,
    }
    lineageGraph.mockResolvedValue(scrubbed)
    render(<LineageView anchor={ANCHOR} />)
    expect(await screen.findByText('customers')).toBeInTheDocument()
    expect(screen.queryByText('email')).not.toBeInTheDocument()
    expect(screen.queryByText('pii')).not.toBeInTheDocument()
  })

  it('filters layers client-side, dropping nodes only reachable through a toggled-off layer', async () => {
    lineageGraph.mockResolvedValue(WITH_CARDS)
    render(<LineageView anchor={ANCHOR} />)
    await screen.findByText('accounts')
    expect(screen.getByText('avg_eod_balance_30d')).toBeInTheDocument()

    await userEvent.click(screen.getByLabelText('Feature lineage'))
    expect(screen.queryByText('avg_eod_balance_30d')).not.toBeInTheDocument()
    expect(screen.queryByText('churn_risk_model')).not.toBeInTheDocument()
    const list = screen.getByRole('region', { name: 'Edges as text' })
    expect(within(list).queryByText(/derives feature/)).not.toBeInTheDocument()

    await userEvent.click(screen.getByLabelText('Entity bridges'))
    expect(screen.queryByText('card_holders')).not.toBeInTheDocument()

    await userEvent.click(screen.getByLabelText('Feature lineage'))
    expect(await screen.findByText('avg_eod_balance_30d')).toBeInTheDocument()
  })

  it('traces a column through its feature to its consumer and opens the drawer', async () => {
    lineageGraph.mockResolvedValue(BASE)
    const { container } = render(<LineageView anchor={ANCHOR} />)
    await screen.findByText('accounts')
    await userEvent.click(screen.getByRole('button', { name: 'balance' }))

    const drawer = screen.getByRole('complementary', { name: 'Details' })
    expect(within(drawer).getByText('public.accounts.balance')).toBeInTheDocument()
    // anchor drawer reuses the search hit's card content
    expect(within(drawer).getByText('end-of-day ledger balance')).toBeInTheDocument()
    expect(within(drawer).getByText('semi_additive')).toBeInTheDocument()
    expect(
      within(drawer).getByText(
        'Lineage traced: this column derives avg_eod_balance_30d, read by churn_risk_model.',
      ),
    ).toBeInTheDocument()
    // the traced path is highlighted on the canvas
    expect(container.querySelectorAll('.ln-edge--trace').length).toBe(2)
    expect(screen.getByRole('button', { name: 'balance' })).toHaveAttribute(
      'aria-pressed', 'true',
    )

    // clicking the same column again clears the trace
    await userEvent.click(screen.getByRole('button', { name: 'balance' }))
    expect(container.querySelectorAll('.ln-edge--trace').length).toBe(0)
  })

  it('expands one more depth around a frontier table and merges the result', async () => {
    lineageGraph.mockResolvedValueOnce(BASE)
    const expansion: api.LineageGraph = {
      nodes: [
        tbl('deposits', 'customers'),
        col('deposits', 'customers', 'cust_id', { grain: true, entity: 'Customer' }),
        col('deposits', 'customers', 'email', { sensitivity: 'pii' }),
        ...CARDS_NODES,
      ],
      edges: [
        contains('deposits', 'customers', 'cust_id'),
        contains('deposits', 'customers', 'email'),
        contains('cards', 'card_holders', 'holder_id'),
        BRIDGE,
      ],
      truncated: false,
    }
    lineageGraph.mockResolvedValueOnce(expansion)
    render(<LineageView anchor={ANCHOR} />)
    await screen.findByText('accounts')
    expect(screen.queryByText('card_holders')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Expand neighbors of customers' }))
    expect(await screen.findByText('card_holders')).toBeInTheDocument()
    expect(lineageGraph).toHaveBeenLastCalledWith('public.customers', 'deposits', {
      direction: 'both', depth: 1,
    })
    // the fetched-around table loses its chip; the new frontier table gains one
    expect(
      screen.queryByRole('button', { name: 'Expand neighbors of customers' }),
    ).not.toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Expand neighbors of card_holders' }),
    ).toBeInTheDocument()
  })

  it('says plainly when an expansion finds nothing new', async () => {
    lineageGraph.mockResolvedValueOnce(BASE)
    lineageGraph.mockResolvedValueOnce({
      nodes: [
        tbl('deposits', 'transactions'),
        col('deposits', 'transactions', 'txn_id', { grain: true }),
        col('deposits', 'transactions', 'account_id'),
      ],
      edges: [
        contains('deposits', 'transactions', 'txn_id'),
        contains('deposits', 'transactions', 'account_id'),
      ],
      truncated: false,
    })
    render(<LineageView anchor={ANCHOR} />)
    await screen.findByText('accounts')
    await userEvent.click(
      screen.getByRole('button', { name: 'Expand neighbors of transactions' }),
    )
    expect(
      await screen.findByText('No further neighbors around transactions.'),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Expand neighbors of transactions' }),
    ).not.toBeInTheDocument()
  })

  it('lists every visible edge as plain text for assistive tech', async () => {
    lineageGraph.mockResolvedValue(WITH_CARDS)
    render(<LineageView anchor={ANCHOR} />)
    await screen.findByText('accounts')
    const list = screen.getByRole('region', { name: 'Edges as text' })
    const lines = within(list)
      .getAllByRole('listitem')
      .map(li => li.textContent)
    expect(lines).toEqual([
      'accounts.cust_id joins customers.cust_id · N:1 · verified',
      'accounts.id joins transactions.account_id · 1:N · verified',
      'accounts.ledger_id joins ledger.entry_id · declared, target not uploaded',
      'accounts.balance derives feature avg_eod_balance_30d · registered',
      'avg_eod_balance_30d is read by churn_risk_model · consumer',
      'customers is Customer entity bridge to cards.card_holders · declared, not value-verified',
    ])
  })

  it('opens the feature drawer with a registry link from the node payload alone', async () => {
    lineageGraph.mockResolvedValue(BASE)
    render(<LineageView anchor={ANCHOR} />)
    await screen.findByText('accounts')
    await userEvent.click(screen.getByRole('button', { name: /avg_eod_balance_30d/ }))
    const drawer = screen.getByRole('complementary', { name: 'Details' })
    expect(within(drawer).getByText('feat_01HZX')).toBeInTheDocument()
    expect(within(drawer).getByRole('link', { name: 'View in registry' })).toHaveAttribute(
      'href', '#/registry?id=feat_01HZX',
    )
  })

  it('shows a calm alert when the graph fetch fails, teaching the 404 ambiguity', async () => {
    lineageGraph.mockRejectedValue(
      new api.ApiError(404, "unknown object 'public.accounts.balance' in source 'deposits'"),
    )
    render(<LineageView anchor={ANCHOR} />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("unknown object 'public.accounts.balance'")
    expect(screen.getByText(/refs your roles cannot see look the same/i)).toBeInTheDocument()
  })

  it('reports a truncated map', async () => {
    lineageGraph.mockResolvedValue({ ...BASE, truncated: true })
    render(<LineageView anchor={ANCHOR} />)
    await screen.findByText('accounts')
    expect(screen.getByText(/cut at the node limit/i)).toBeInTheDocument()
  })
})
