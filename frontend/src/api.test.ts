import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ApiError, type FeatureIdea, recommendFeatures, recommendFeatureSets, refineCandidate,
  searchCatalog, uploadFile,
} from './api'
import { setSession } from './session'

const fetchMock = vi.fn()

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
  setSession({ user: 'ana', roles: ['data_owner', 'pii_reader'] })
})
afterEach(() => vi.unstubAllGlobals())

const ok = (body: unknown) => async () =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

describe('api client', () => {
  it('sends the stub session headers on every request', async () => {
    fetchMock.mockImplementation(ok([]))
    await searchCatalog('balance')
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/search?q=balance&limit=20')
    expect(init.headers['X-User']).toBe('ana')
    expect(init.headers['X-Roles']).toBe('data_owner,pii_reader')
  })

  it('percent-encodes the session user so non-Latin-1 names cannot break fetch', async () => {
    // fetch header values must be ISO-8859-1; a raw name like this throws a TypeError before
    // any request is sent. The dev stub accepts the percent-encoded form.
    setSession({ user: 'Łukasz 张伟', roles: ['data_owner'] })
    fetchMock.mockImplementation(ok([]))
    await searchCatalog('balance')
    const [, init] = fetchMock.mock.calls[0]
    expect(init.headers['X-User']).toBe(encodeURIComponent('Łukasz 张伟'))
  })

  it('maps FastAPI error bodies to ApiError', async () => {
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail: 'missing X-User header (stub auth)' }), { status: 401 }))
    await expect(searchCatalog('x')).rejects.toThrowError(ApiError)
    await expect(searchCatalog('x')).rejects.toMatchObject({
      status: 401, detail: 'missing X-User header (stub auth)' })
  })

  it('joins FastAPI 422 validation arrays into a readable detail', async () => {
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail: [
        { loc: ['body', 'name'], msg: 'String should have at least 1 character', type: 'string_too_short' },
        { loc: ['body', 'derives_from'], msg: 'Field required', type: 'missing' },
      ] }), { status: 422 }))
    await expect(searchCatalog('x')).rejects.toMatchObject({
      status: 422,
      detail: 'body.name: String should have at least 1 character; body.derives_from: Field required',
    })
  })

  it('falls back to the status text when the error body is not JSON', async () => {
    fetchMock.mockImplementation(async () =>
      new Response('<html>Bad Gateway</html>', { status: 502, statusText: 'Bad Gateway' }))
    await expect(searchCatalog('x')).rejects.toThrowError(ApiError)
    await expect(searchCatalog('x')).rejects.toMatchObject({ status: 502, detail: 'Bad Gateway' })
  })

  it('keeps the status text when a JSON body has no usable detail', async () => {
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ error: 'boom' }),
        { status: 500, statusText: 'Internal Server Error' }))
    await expect(searchCatalog('x')).rejects.toMatchObject({
      status: 500, detail: 'Internal Server Error' })
  })

  it('never surfaces a blank message when statusText is empty (HTTP/2)', async () => {
    fetchMock.mockImplementation(async () =>
      new Response('gateway timeout', { status: 504 }))
    await expect(searchCatalog('x')).rejects.toMatchObject({ status: 504, detail: 'HTTP 504' })
  })

  it('lets network failures propagate as the raw error, not ApiError', async () => {
    // Pinned behavior: fetch rejections (offline, DNS, CORS) pass through untouched; screens
    // render them via String(err).
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    const err = await searchCatalog('x').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(TypeError)
    expect(err).not.toBeInstanceOf(ApiError)
    expect((err as TypeError).message).toBe('Failed to fetch')
  })

  it('uploads multipart form data without forcing a content type', async () => {
    fetchMock.mockImplementation(ok({
      status: 'ingested', reason: null, asserted: 4, staled: 0, quarantined: 0, flagged: null }))
    const file = new File(['source,table\n'], 'd.csv', { type: 'text/csv' })
    const result = await uploadFile(file, 'deposits')
    expect(result.status).toBe('ingested')
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/uploads')
    expect(init.body).toBeInstanceOf(FormData)
    expect(init.body.get('source')).toBe('deposits')
    expect(init.headers['Content-Type']).toBeUndefined()
  })
})

// The FeatureIdea shape exactly as the backend serializes it in every assist response.
const IDEA: FeatureIdea = {
  name: 'avg_balance_30d', description: '30 day average balance',
  derives_from: ['public.accounts.balance'], aggregation: 'avg_30d', grain_table: 'customers',
  derives_pairs: [['deposits', 'public.accounts.balance']],
  verification: 'DESIGN-CHECKED', critic_note: '',
  rationale: 'a shorter window reacts faster',
}

const CAVEAT =
  'advisory only — a fit/coverage judgment over the metadata, not a performance prediction; '
  + 'confirm the winner with a backtest once features are computed'

describe('feature assist client', () => {
  it('recommendFeatures posts the full round body and returns proposals with rejections', async () => {
    const rejections = [{ name: 'avg_balance', reason: 'leaks target', code: 'LEAKAGE' }]
    fetchMock.mockImplementation(ok({ proposals: [IDEA], rejections }))
    const result = await recommendFeatures(
      'predict customer churn in the next 90 days', 'deposits', 'public.labels.churned',
      'customer', 'more behavioral signals, fewer balance aggregates')
    expect(result).toEqual({ proposals: [IDEA], rejections })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/features/recommend')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({
      objective: 'predict customer churn in the next 90 days',
      catalog_source: 'deposits',
      target_ref: 'public.labels.churned',
      entity: 'customer',
      feedback: 'more behavioral signals, fewer balance aggregates',
    })
  })

  it('recommendFeatures sends null for every optional field left out', async () => {
    fetchMock.mockImplementation(ok({ proposals: [], rejections: [] }))
    await recommendFeatures('predict churn', null)
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init.body)).toEqual({
      objective: 'predict churn', catalog_source: null, target_ref: null,
      entity: null, feedback: null,
    })
  })

  it('recommendFeatureSets posts the same body to the sets endpoint and returns sets, recommendation, and rejections', async () => {
    const payload = {
      sets: [
        { lens: 'temporal', features: [IDEA] },
        { lens: 'unary', features: [] },
      ],
      recommendation: {
        recommended_lens: 'temporal',
        reasoning: 'recency signals move earliest for a churn horizon',
        caveat: CAVEAT,
      },
      rejections: [
        { name: 'days_to_churn', reason: 'derives from the target column', code: 'LEAKAGE' },
      ],
    }
    fetchMock.mockImplementation(ok(payload))
    const result = await recommendFeatureSets(
      'predict customer churn in the next 90 days', 'deposits', 'public.labels.churned',
      'customer')
    expect(result).toEqual(payload)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/features/recommend-sets')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({
      objective: 'predict customer churn in the next 90 days',
      catalog_source: 'deposits',
      target_ref: 'public.labels.churned',
      entity: 'customer',
      feedback: null,
    })
  })

  it('recommendFeatureSets passes a null recommendation through untouched', async () => {
    // The backend sends null when every set came back empty: no recommendation over nothing.
    fetchMock.mockImplementation(ok({
      sets: [{ lens: 'unary', features: [] }], recommendation: null, rejections: [] }))
    const result = await recommendFeatureSets('predict churn', null)
    expect(result.recommendation).toBeNull()
    expect(result.sets).toEqual([{ lens: 'unary', features: [] }])
  })

  it('refineCandidate fills the candidate defaults on the wire and returns the revised idea', async () => {
    fetchMock.mockImplementation(ok({ revised: IDEA }))
    const result = await refineCandidate(
      { name: 'avg_balance_90d' }, 'use a 30 day window', 'deposits')
    expect('revised' in result && result.revised).toEqual(IDEA)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/features/refine')
    expect(JSON.parse(init.body)).toEqual({
      candidate: {
        name: 'avg_balance_90d', description: '', derives_from: [],
        aggregation: null, grain_table: null,
      },
      instruction: 'use a 30 day window',
      catalog_source: 'deposits', entity: null, target_ref: null,
    })
  })

  it('refineCandidate sends the full candidate fields when the UI holds them', async () => {
    fetchMock.mockImplementation(ok({ revised: IDEA }))
    await refineCandidate({
      name: 'avg_balance_90d', description: '90 day average balance',
      derives_from: ['public.accounts.balance'], aggregation: 'avg_90d',
      grain_table: 'customers',
    }, 'use a 30 day window')
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init.body).candidate).toEqual({
      name: 'avg_balance_90d', description: '90 day average balance',
      derives_from: ['public.accounts.balance'], aggregation: 'avg_90d',
      grain_table: 'customers',
    })
  })

  it('refineCandidate surfaces a gauntlet rejection as 200 data, not an error', async () => {
    fetchMock.mockImplementation(ok({
      rejected: { reason: 'no revision was produced', code: 'NO_REVISION' } }))
    const result = await refineCandidate({ name: 'avg_balance_90d' }, 'use a 30 day window')
    expect('rejected' in result && result.rejected).toEqual({
      reason: 'no revision was produced', code: 'NO_REVISION' })
  })

  it('maps the unconfigured-provider 503 to ApiError with the backend detail', async () => {
    const detail = 'no LLM provider is configured on this deployment '
      + '(set FEATUREGEN_LLM_PROVIDER=anthropic to enable feature-assist)'
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail }), { status: 503 }))
    await expect(recommendFeatureSets('predict churn', null)).rejects.toMatchObject({
      status: 503, detail })
  })
})
