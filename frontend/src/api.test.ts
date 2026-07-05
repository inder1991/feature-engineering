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
