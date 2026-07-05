import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, searchCatalog, uploadFile } from './api'
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

  it('maps FastAPI error bodies to ApiError', async () => {
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail: 'missing X-User header (stub auth)' }), { status: 401 }))
    await expect(searchCatalog('x')).rejects.toThrowError(ApiError)
    await expect(searchCatalog('x')).rejects.toMatchObject({
      status: 401, detail: 'missing X-User header (stub auth)' })
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
