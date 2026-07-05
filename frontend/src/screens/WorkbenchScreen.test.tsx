import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { WorkbenchScreen } from './WorkbenchScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    recommendFeatures: vi.fn(),
    featureRecipe: vi.fn(),
    leakageCheck: vi.fn(),
    registerFeature: vi.fn(),
    featureFreshness: vi.fn(),
  }
})
const recommendFeatures = vi.mocked(api.recommendFeatures)
const featureRecipe = vi.mocked(api.featureRecipe)
const registerFeature = vi.mocked(api.registerFeature)
const featureFreshness = vi.mocked(api.featureFreshness)

beforeEach(() => {
  recommendFeatures.mockReset()
  featureRecipe.mockReset()
  registerFeature.mockReset()
  featureFreshness.mockReset()
})

// derives_pairs deliberately names a catalog ('cards') that differs from the source the tests
// type into the scope row ('deposits'): registration lineage must come from the backend pairs,
// never from the typed source.
const IDEA: api.FeatureIdea = {
  name: 'avg_balance', description: 'average balance per customer',
  derives_from: ['public.accounts.balance'], aggregation: 'avg', grain_table: 'customers',
  derives_pairs: [['cards', 'public.accounts.balance']],
  verification: 'DESIGN-CHECKED',
  rationale: 'falling balances signal a customer preparing to leave',
}

const IDEA_SPEC: api.FeatureSpecIn = {
  name: 'avg_balance', description: 'average balance per customer',
  grain_table: 'customers', aggregation: 'avg', as_of_column: null,
  derives_from: [{ catalog_source: 'cards', object_ref: 'public.accounts.balance' }],
}

const OTHER_IDEA: api.FeatureIdea = {
  name: 'txn_count', description: 'transactions per customer',
  derives_from: ['public.transactions.id'], aggregation: 'count', grain_table: 'customers',
  derives_pairs: [['cards', 'public.transactions.id']],
  // rationale left blank: the LLM omitted a causal note, so no Why line should render for it.
  verification: 'DESIGN-CHECKED', rationale: '',
}

const OTHER_IDEA_SPEC: api.FeatureSpecIn = {
  name: 'txn_count', description: 'transactions per customer',
  grain_table: 'customers', aggregation: 'count', as_of_column: null,
  derives_from: [{ catalog_source: 'cards', object_ref: 'public.transactions.id' }],
}

const FRESH: api.FeatureFreshness = { fresh: true, stale_sources: [] }

function recipeWith(joinPath: api.JoinStep[]): api.Recipe {
  return {
    intent: 'total spend per customer', grain_table: 'customers',
    derives_from: ['public.transactions.amount'], aggregation: 'sum', as_of_column: null,
    join_path: joinPath,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => { resolve = res })
  return { promise, resolve }
}

interface Scope {
  source?: string
  entity?: string
  target?: string
}

async function renderAndGenerate(ideas: api.FeatureIdea[], scope: Scope = {}) {
  recommendFeatures.mockResolvedValue(ideas)
  render(<WorkbenchScreen />)
  if (scope.source) {
    await userEvent.type(screen.getByLabelText('Catalog source'), scope.source)
  }
  if (scope.entity) {
    await userEvent.type(screen.getByLabelText('Entity'), scope.entity)
  }
  if (scope.target) {
    await userEvent.type(screen.getByLabelText('Target column'), scope.target)
  }
  await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
  await userEvent.click(screen.getByRole('button', { name: 'Generate features' }))
}

async function selectCandidate(name: string) {
  await userEvent.click(await screen.findByRole('checkbox', { name: `Select ${name}` }))
}

async function registerSelection(count: number) {
  const plural = count === 1 ? 'feature' : 'features'
  await userEvent.click(screen.getByRole('button', { name: `Register ${count} ${plural}` }))
  await userEvent.click(screen.getByRole('button', { name: 'Confirm registration' }))
}

async function openDescribe() {
  await userEvent.click(screen.getByRole('button', { name: 'Or describe a feature yourself' }))
}

async function draftFeature(description: string) {
  await userEvent.type(screen.getByLabelText('Describe the feature you want'), description)
  await userEvent.click(screen.getByRole('button', { name: 'Draft candidate' }))
}

async function renderAndDraft(joinPath: api.JoinStep[] = []) {
  featureRecipe.mockResolvedValue(recipeWith(joinPath))
  render(<WorkbenchScreen />)
  await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
  await openDescribe()
  await draftFeature('total spend per customer')
  expect(await screen.findByText('Draft')).toBeInTheDocument()
}

describe('generation', () => {
  it('passes the goal and every scope field through to the recommend call', async () => {
    await renderAndGenerate([], {
      source: 'deposits', entity: 'customer', target: 'public.labels.churned',
    })
    expect(recommendFeatures).toHaveBeenCalledWith(
      'predict churn', 'deposits', 'public.labels.churned', 'customer')
  })

  it('sends null for scope fields left blank', async () => {
    await renderAndGenerate([])
    expect(recommendFeatures).toHaveBeenCalledWith('predict churn', null, null, null)
  })

  it('shows the empty note only after a generation round returns nothing', async () => {
    recommendFeatures.mockResolvedValue([])
    render(<WorkbenchScreen />)
    expect(screen.queryByText(/no grounded candidates/i)).not.toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: 'Generate features' }))
    expect(await screen.findByText(/no grounded candidates for that goal/i)).toBeInTheDocument()
  })

  it('applies only the latest generation round when responses arrive out of order', async () => {
    const first = deferred<api.FeatureIdea[]>()
    const second = deferred<api.FeatureIdea[]>()
    recommendFeatures
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    const generate = screen.getByRole('button', { name: 'Generate features' })
    await userEvent.click(generate)
    await userEvent.click(generate)
    await act(async () => {
      second.resolve([OTHER_IDEA])
    })
    expect(await screen.findByText('txn_count')).toBeInTheDocument()
    // The stale first response resolves late and must not overwrite the newer round.
    await act(async () => {
      first.resolve([IDEA])
    })
    expect(screen.getByText('txn_count')).toBeInTheDocument()
    expect(screen.queryByText('avg_balance')).not.toBeInTheDocument()
  })

  it('shows the honest 503 notice when assist is unconfigured', async () => {
    recommendFeatures.mockRejectedValue(new api.ApiError(503, 'not configured'))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: 'Generate features' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/ai assist is not configured/i)
  })

  it('the example chip fills the goal input and enables the primary action', async () => {
    render(<WorkbenchScreen />)
    expect(screen.getByRole('button', { name: 'Generate features' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'predict churn' }))
    expect(screen.getByLabelText('Prediction goal')).toHaveValue('predict churn')
    expect(screen.getByRole('button', { name: 'Generate features' })).toBeEnabled()
    expect(recommendFeatures).not.toHaveBeenCalled()
  })
})

describe('selection and registration', () => {
  it('registers a selected candidate only after the explicit confirm, with lineage from the backend pairs', async () => {
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA], { source: 'deposits' })
    // Lineage display comes from derives_pairs ('cards'), not the typed source ('deposits').
    expect(await screen.findByText('cards:public.accounts.balance')).toBeInTheDocument()
    await selectCandidate('avg_balance')
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Register 1 feature' }))
    expect(registerFeature).not.toHaveBeenCalled()
    expect(
      screen.getByText('This feature will enter the catalog registry with its lineage.'),
    ).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm registration' }))
    expect(registerFeature).toHaveBeenCalledWith(IDEA_SPEC)
    expect(registerFeature).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/registered/i)).toBeInTheDocument()
    expect(screen.getByText('feat_01')).toBeInTheDocument()
    expect(featureFreshness).toHaveBeenCalledWith('feat_01')
    expect(screen.getByText('fresh')).toBeInTheDocument()
    // The registered row swaps its checkbox for the ok state.
    expect(screen.queryByRole('checkbox', { name: 'Select avg_balance' })).not.toBeInTheDocument()
  })

  it('sends exactly one register request when confirm is double-clicked in flight', async () => {
    const pending = deferred<string>()
    registerFeature.mockImplementation(() => pending.promise)
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    await userEvent.click(screen.getByRole('button', { name: 'Register 1 feature' }))
    const confirm = screen.getByRole('button', { name: 'Confirm registration' })
    await userEvent.click(confirm)
    await userEvent.click(confirm)
    expect(registerFeature).toHaveBeenCalledTimes(1)
    expect(confirm).toBeDisabled()
    await act(async () => {
      pending.resolve('feat_01')
    })
    expect(await screen.findByText('feat_01')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(1)
  })

  it('registers a batch of two sequentially, in candidate order, one request each', async () => {
    registerFeature.mockResolvedValueOnce('feat_01').mockResolvedValueOnce('feat_02')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA, OTHER_IDEA])
    await selectCandidate('avg_balance')
    await selectCandidate('txn_count')
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Register 2 features' }))
    expect(
      screen.getByText('These 2 features will enter the catalog registry with their lineage.'),
    ).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm registration' }))
    expect(await screen.findByText('feat_02')).toBeInTheDocument()
    expect(screen.getByText('feat_01')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(2)
    expect(registerFeature).toHaveBeenNthCalledWith(1, IDEA_SPEC)
    expect(registerFeature).toHaveBeenNthCalledWith(2, OTHER_IDEA_SPEC)
    expect(screen.getAllByText('fresh')).toHaveLength(2)
  })

  it('continues the batch past a failure and keeps the failed candidate selected for retry', async () => {
    registerFeature
      .mockRejectedValueOnce(new api.ApiError(409, 'feature name already registered'))
      .mockResolvedValueOnce('feat_02')
      .mockResolvedValueOnce('feat_03')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA, OTHER_IDEA])
    await selectCandidate('avg_balance')
    await selectCandidate('txn_count')
    await registerSelection(2)
    // First candidate failed inline; the second still registered.
    expect(await screen.findByText('feature name already registered')).toBeInTheDocument()
    expect(screen.getByText('feat_02')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(2)
    // The failed candidate stays selected, ready to retry.
    expect(screen.getByRole('checkbox', { name: 'Select avg_balance' })).toBeChecked()
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    await registerSelection(1)
    expect(await screen.findByText('feat_03')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(3)
    expect(registerFeature).toHaveBeenNthCalledWith(3, IDEA_SPEC)
    expect(screen.queryByText('feature name already registered')).not.toBeInTheDocument()
  })

  it('cancel backs out of the confirm step without registering', async () => {
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    await userEvent.click(screen.getByRole('button', { name: 'Register 1 feature' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(registerFeature).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Register 1 feature' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Select avg_balance' })).toBeChecked()
  })

  it('does not resurrect registered state when a regeneration reuses a name', async () => {
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    await registerSelection(1)
    expect(await screen.findByText(/registered/i)).toBeInTheDocument()
    // Second round returns a candidate with the same LLM-chosen name: it was never registered.
    await userEvent.click(screen.getByRole('button', { name: 'Generate features' }))
    const checkbox = await screen.findByRole('checkbox', { name: 'Select avg_balance' })
    expect(checkbox).not.toBeChecked()
    expect(screen.queryByText(/registered/i)).not.toBeInTheDocument()
  })

  it('marks a stale registration with its stale sources', async () => {
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue({ fresh: false, stale_sources: ['cards'] })
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    await registerSelection(1)
    expect(await screen.findByText('stale: cards')).toBeInTheDocument()
    expect(screen.queryByText('fresh')).not.toBeInTheDocument()
  })

  it('omits the freshness chip silently when the freshness call fails', async () => {
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockRejectedValue(new api.ApiError(500, 'freshness unavailable'))
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    await registerSelection(1)
    expect(await screen.findByText(/registered/i)).toBeInTheDocument()
    expect(screen.queryByText('fresh')).not.toBeInTheDocument()
    expect(screen.queryByText(/stale:/)).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('scope changes', () => {
  it('editing the goal keeps candidates', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Prediction goal'), ' next quarter')
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.queryByText(/scope changed/i)).not.toBeInTheDocument()
  })

  it('editing the catalog source clears generated candidates and drafts', async () => {
    featureRecipe.mockResolvedValue(recipeWith([]))
    await renderAndGenerate([IDEA], { source: 'deposits' })
    await openDescribe()
    await draftFeature('total spend per customer')
    expect(await screen.findByText('total_spend_per_customer')).toBeInTheDocument()
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    // Drafts were snapshotted against the previous source: a source edit clears everything.
    await userEvent.type(screen.getByLabelText('Catalog source'), 'x')
    expect(screen.queryByText('avg_balance')).not.toBeInTheDocument()
    expect(screen.queryByText('total_spend_per_customer')).not.toBeInTheDocument()
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Scope changed. Regenerate to refresh candidates.')
  })

  it('editing the entity clears generated candidates but keeps drafts', async () => {
    featureRecipe.mockResolvedValue(recipeWith([]))
    await renderAndGenerate([IDEA], { source: 'deposits' })
    await openDescribe()
    await draftFeature('total spend per customer')
    expect(await screen.findByText('total_spend_per_customer')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Entity'), 'c')
    expect(screen.queryByText('avg_balance')).not.toBeInTheDocument()
    expect(screen.getByText('total_spend_per_customer')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/scope changed/i)
  })

  it('editing the target clears generated candidates and the screening note', async () => {
    await renderAndGenerate([IDEA], { target: 'public.labels.churned' })
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByText(/leaky candidates were rejected/i)).toBeInTheDocument()
    // Candidates were screened against the previous target: any edit voids them.
    await userEvent.type(screen.getByLabelText('Target column'), '2')
    expect(screen.queryByText('avg_balance')).not.toBeInTheDocument()
    expect(screen.queryByText(/leaky candidates were rejected/i)).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/scope changed/i)
  })

  it('clears the selection when the scope changes', async () => {
    await renderAndGenerate([IDEA, OTHER_IDEA])
    await selectCandidate('avg_balance')
    await selectCandidate('txn_count')
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Entity'), 'c')
    expect(screen.queryByText('2 selected')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Register 2 features' })).not.toBeInTheDocument()
  })
})

describe('described drafts', () => {
  it('drafts a candidate and registers it with the snapshot-source pairs', async () => {
    registerFeature.mockResolvedValue('feat_09')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndDraft([
      { from_ref: 'public.transactions.account_id', to_ref: 'public.accounts.id', cardinality: 'N:1' },
    ])
    expect(featureRecipe).toHaveBeenCalledWith('total spend per customer', 'deposits')
    // The suggested name is a slug of the description, editable before selection.
    expect(screen.getByLabelText('Name')).toHaveValue('total_spend_per_customer')
    // Lineage display uses the drafted-against snapshot, not live context.
    expect(screen.getByText('deposits:public.transactions.amount')).toBeInTheDocument()
    await selectCandidate('total_spend_per_customer')
    await registerSelection(1)
    expect(registerFeature).toHaveBeenCalledWith({
      name: 'total_spend_per_customer', description: 'total spend per customer',
      grain_table: 'customers', aggregation: 'sum', as_of_column: null,
      derives_from: [{ catalog_source: 'deposits', object_ref: 'public.transactions.amount' }],
    })
    expect(registerFeature).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('feat_09')).toBeInTheDocument()
    expect(screen.getByText('fresh')).toBeInTheDocument()
  })

  it('requires a name before a draft can be selected, and registers under the edited name', async () => {
    registerFeature.mockResolvedValue('feat_10')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndDraft()
    await selectCandidate('total_spend_per_customer')
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    // Blanking the name deselects the draft and blocks selection until it is named again.
    await userEvent.clear(screen.getByLabelText('Name'))
    expect(screen.queryByText('1 selected')).not.toBeInTheDocument()
    const checkbox = screen.getByRole('checkbox', { name: 'Select unnamed draft' })
    expect(checkbox).toBeDisabled()
    expect(checkbox).not.toBeChecked()
    expect(screen.getByText('Name this draft to select it for registration.')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Name'), 'spend_90d')
    await selectCandidate('spend_90d')
    await registerSelection(1)
    expect(registerFeature).toHaveBeenCalledWith(expect.objectContaining({ name: 'spend_90d' }))
    expect(await screen.findByText('feat_10')).toBeInTheDocument()
  })

  it('disables drafting until a catalog source is set', async () => {
    render(<WorkbenchScreen />)
    await openDescribe()
    await userEvent.type(
      screen.getByLabelText('Describe the feature you want'), 'total spend per customer')
    expect(screen.getByRole('button', { name: 'Draft candidate' })).toBeDisabled()
    expect(screen.getByText(/set catalog source above/i)).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    expect(screen.getByRole('button', { name: 'Draft candidate' })).toBeEnabled()
    expect(featureRecipe).not.toHaveBeenCalled()
  })

  it('accumulates drafts so several described features register together', async () => {
    featureRecipe.mockResolvedValue(recipeWith([]))
    registerFeature.mockResolvedValueOnce('feat_11').mockResolvedValueOnce('feat_12')
    featureFreshness.mockResolvedValue(FRESH)
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await openDescribe()
    await draftFeature('total spend per customer')
    await draftFeature('active days per customer')
    expect(await screen.findByText('active_days_per_customer')).toBeInTheDocument()
    expect(screen.getAllByText('Draft')).toHaveLength(2)
    await selectCandidate('total_spend_per_customer')
    await selectCandidate('active_days_per_customer')
    await registerSelection(2)
    expect(await screen.findByText('feat_11')).toBeInTheDocument()
    expect(screen.getByText('feat_12')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(2)
  })

  it('gates the describe path behind the same missing-provider notice', async () => {
    featureRecipe.mockRejectedValue(new api.ApiError(503, 'not configured'))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await openDescribe()
    await draftFeature('total spend per customer')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/ai assist is not configured/i)
    expect(screen.queryByText('Draft')).not.toBeInTheDocument()
  })

  it('renders the draft join path with a fan-out warning', async () => {
    await renderAndDraft([
      { from_ref: 'public.customers.cust_id', to_ref: 'public.accounts.cust_id', cardinality: '1:N' },
      { from_ref: 'public.accounts.id', to_ref: 'public.transactions.account_id', cardinality: '1:N' },
    ])
    expect(screen.getAllByText('(1:N)')).toHaveLength(2)
    expect(screen.getByText(/aggregate before joining/i)).toBeInTheDocument()
  })

  it('flags a lowercase 1:n hop as fan-out', async () => {
    await renderAndDraft([
      { from_ref: 'public.customers.cust_id', to_ref: 'public.accounts.cust_id', cardinality: '1:n' },
    ])
    expect(screen.getByText(/aggregate before joining/i)).toBeInTheDocument()
  })

  it('names unknown cardinality instead of rendering it as calm', async () => {
    await renderAndDraft([
      { from_ref: 'public.accounts.cust_id', to_ref: 'public.customers.cust_id', cardinality: 'N:1' },
      { from_ref: 'public.customers.cust_id', to_ref: 'public.segments.cust_id', cardinality: null },
    ])
    expect(screen.queryByText(/aggregate before joining/i)).not.toBeInTheDocument()
    expect(screen.getByText(/cardinality unknown/)).toBeInTheDocument()
    expect(screen.getByText(/cannot be ruled out/i)).toBeInTheDocument()
  })

  it('stays calm on an all-N:1 join path', async () => {
    await renderAndDraft([
      { from_ref: 'public.accounts.cust_id', to_ref: 'public.customers.cust_id', cardinality: 'N:1' },
    ])
    expect(screen.queryByText(/aggregate before joining/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/cannot be ruled out/i)).not.toBeInTheDocument()
  })
})

describe('batch describe composer', () => {
  const THREE_LINES =
    'total spend per customer{Enter}days since last transaction{Enter}active accounts per customer'

  async function typeDescribe(text: string) {
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await openDescribe()
    await userEvent.type(screen.getByLabelText('Describe the feature you want'), text)
  }

  it('drafts one candidate per line, in line order, against the snapshot source', async () => {
    featureRecipe.mockResolvedValue(recipeWith([]))
    await typeDescribe(THREE_LINES)
    // The live label counts the non-empty lines before submit.
    expect(screen.getByRole('button', { name: 'Draft 3 candidates' })).toBeEnabled()
    await userEvent.click(screen.getByRole('button', { name: 'Draft 3 candidates' }))
    expect(await screen.findByText('active_accounts_per_customer')).toBeInTheDocument()
    expect(featureRecipe).toHaveBeenCalledTimes(3)
    expect(featureRecipe).toHaveBeenNthCalledWith(1, 'total spend per customer', 'deposits')
    expect(featureRecipe).toHaveBeenNthCalledWith(2, 'days since last transaction', 'deposits')
    expect(featureRecipe).toHaveBeenNthCalledWith(3, 'active accounts per customer', 'deposits')
    expect(screen.getAllByText('Draft')).toHaveLength(3)
    // Candidates append in line order.
    const list = screen.getByRole('list').textContent ?? ''
    expect(list.indexOf('total_spend_per_customer'))
      .toBeLessThan(list.indexOf('days_since_last_transaction'))
    expect(list.indexOf('days_since_last_transaction'))
      .toBeLessThan(list.indexOf('active_accounts_per_customer'))
    // A clean batch clears the textarea fully; the composer stays open.
    expect(screen.getByLabelText('Describe the feature you want')).toHaveValue('')
  })

  it('isolates a failed line: the rest still draft and only the failed line stays to retry', async () => {
    featureRecipe
      .mockResolvedValueOnce(recipeWith([]))
      .mockRejectedValueOnce(new api.ApiError(422, 'no column matches that description'))
      .mockResolvedValueOnce(recipeWith([]))
    await typeDescribe(THREE_LINES)
    await userEvent.click(screen.getByRole('button', { name: 'Draft 3 candidates' }))
    expect(await screen.findByText('active_accounts_per_customer')).toBeInTheDocument()
    expect(screen.getByText('total_spend_per_customer')).toBeInTheDocument()
    expect(screen.queryByText('days_since_last_transaction')).not.toBeInTheDocument()
    expect(screen.getAllByText('Draft')).toHaveLength(2)
    // The rejected line is called out inline and left in the textarea for a retry.
    expect(screen.getByText('Line 2: no column matches that description')).toBeInTheDocument()
    expect(screen.getByLabelText('Describe the feature you want'))
      .toHaveValue('days since last transaction')
  })

  it('drafts each line once when the submit is double-clicked in flight', async () => {
    const first = deferred<api.Recipe>()
    const second = deferred<api.Recipe>()
    featureRecipe
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    await typeDescribe('total spend per customer{Enter}days since last transaction')
    const button = screen.getByRole('button', { name: 'Draft 2 candidates' })
    await userEvent.click(button)
    // Line 1's recipe is pending; the button is disabled and a second submit is a no-op.
    expect(button).toBeDisabled()
    expect(button).toHaveTextContent('Drafting')
    await userEvent.click(button)
    expect(featureRecipe).toHaveBeenCalledTimes(1)
    await act(async () => {
      first.resolve(recipeWith([]))
    })
    await act(async () => {
      second.resolve(recipeWith([]))
    })
    expect(await screen.findByText('days_since_last_transaction')).toBeInTheDocument()
    // Exactly one call per line: the in-flight double-submit never started a second batch.
    expect(featureRecipe).toHaveBeenCalledTimes(2)
    expect(featureRecipe).toHaveBeenNthCalledWith(1, 'total spend per customer', 'deposits')
    expect(featureRecipe).toHaveBeenNthCalledWith(2, 'days since last transaction', 'deposits')
  })
})

describe('verification stamp and rationale', () => {
  it('renders the causal rationale when present and omits it when the LLM left it blank', async () => {
    await renderAndGenerate([IDEA, OTHER_IDEA])
    expect(
      await screen.findByText(/falling balances signal a customer preparing to leave/i),
    ).toBeInTheDocument()
    // OTHER_IDEA carries an empty rationale, so exactly one Why line renders across the list.
    expect(screen.getAllByText(/^Why:/)).toHaveLength(1)
  })

  it('stamps generated candidates design-checked and shows the honest help line once', async () => {
    await renderAndGenerate([IDEA, OTHER_IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    // One soft stamp per generated candidate, from the backend verification field (lowercased).
    expect(screen.getAllByText('design-checked')).toHaveLength(2)
    // The explanation is one help line for the whole list, not repeated per row.
    expect(screen.getAllByText(/structurally safe against leakage/i)).toHaveLength(1)
  })

  it('never stamps drafts and hides the help line on a drafts-only list', async () => {
    // renderAndDraft never generates, so no candidate passed the gauntlet.
    await renderAndDraft()
    expect(screen.getByText('Draft')).toBeInTheDocument()
    expect(screen.queryByText('design-checked')).not.toBeInTheDocument()
    expect(screen.queryByText(/structurally safe against leakage/i)).not.toBeInTheDocument()
  })

  it('leaves a described draft as DRAFT only alongside a stamped generated candidate', async () => {
    featureRecipe.mockResolvedValue(recipeWith([]))
    await renderAndGenerate([IDEA], { source: 'deposits' })
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await openDescribe()
    await draftFeature('total spend per customer')
    expect(await screen.findByText('total_spend_per_customer')).toBeInTheDocument()
    // The generated candidate keeps its lone stamp; the draft carries none.
    expect(screen.getAllByText('design-checked')).toHaveLength(1)
    expect(screen.getByText('Draft')).toBeInTheDocument()
  })
})
