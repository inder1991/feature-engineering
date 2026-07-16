import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ApiError, contractConfirm, contractConsideredSet, contractDraft, type ContractDraft,
  createIntegration, createSync, deleteIntegration, deleteSync, discoverServices,
  type DiscoveredService, type FeatureIdea, getIntegration, getSync, importSync,
  type Integration, type LineageGraph, lineageGraph, listContracts, listIntegrations,
  listSyncs, patchIntegration, patchSync, previewSync, recommendFeatures, recommendFeatureSets,
  refineCandidate, searchCatalog, type Sync, type SyncPreview, uploadFile,
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

  it('builds /search with repeated facet params + boolean flags, session headers attached', async () => {
    fetchMock.mockImplementation(ok({ hits: [], facets: {}, total: 0 }))
    await searchCatalog('balance', {
      source: ['deposits', 'cards'],
      additivity: ['semi_additive'],
      grain: true,
    })
    const [url, init] = fetchMock.mock.calls[0]
    // Pinned byte-for-byte: q, then each facet group in contract order with values REPEATED
    // (AND across groups, OR within one), then grain=true, then limit. A filtered search is a URL.
    expect(url).toBe(
      '/search?q=balance&source=deposits&source=cards&additivity=semi_additive&grain=true&limit=20',
    )
    expect(init.headers['X-User']).toBe('ana')
    expect(init.headers['X-Roles']).toBe('data_owner,pii_reader')
  })

  it('omits empty facet groups and unset flags, and honors a custom limit and empty q', async () => {
    fetchMock.mockImplementation(ok({ hits: [], facets: {}, total: 0 }))
    // Empty q browses all; an empty facet array and an unset flag never reach the wire.
    await searchCatalog('', { source: [], grain: false, as_of: true }, 50)
    const [url] = fetchMock.mock.calls[0]
    expect(url).toBe('/search?q=&as_of=true&limit=50')
  })

  it('returns the SearchResult shape (hits, facets, total) untouched', async () => {
    const payload = {
      hits: [], total: 3,
      facets: {
        source: [{ value: 'deposits', count: 2 }, { value: 'cards', count: 1 }],
        sensitivity: [{ value: '(none)', count: 3 }],
        grain: [{ value: 'true', count: 0 }],
        as_of: [{ value: 'true', count: 1 }],
      },
    }
    fetchMock.mockImplementation(ok(payload))
    const result = await searchCatalog('balance')
    expect(result).toEqual(payload)
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

  it('keeps the X-Ingestion-Run-Id header on a failed request so the run stays inspectable', async () => {
    // POST /uploads (and /syncs/{id}/import) attach the run id header to post-open 4xx/5xx too;
    // dropping it on the error path would orphan the very run record that explains the failure.
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail: 'upload failed at the ingest stage' }),
        { status: 500, headers: { 'X-Ingestion-Run-Id': 'igr_01HZZC' } }))
    const file = new File(['source,table\n'], 'd.csv', { type: 'text/csv' })
    await expect(uploadFile(file, 'deposits')).rejects.toMatchObject({
      status: 500, detail: 'upload failed at the ingest stage', ingestionRunId: 'igr_01HZZC' })
  })

  it('leaves ingestionRunId null when a failure carries no run header', async () => {
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail: 'nope' }), { status: 404 }))
    await expect(searchCatalog('x')).rejects.toMatchObject({
      status: 404, detail: 'nope', ingestionRunId: null })
  })

  it('uploads multipart form data without forcing a content type', async () => {
    fetchMock.mockImplementation(ok({
      status: 'ingested', reason: null, asserted: 4, changed_objects: 0, quarantined: 0, flagged: null }))
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

describe('lineage client', () => {
  const GRAPH: LineageGraph = {
    nodes: [
      {
        id: 'deposits:public.accounts', kind: 'table', object_ref: 'public.accounts',
        table: 'accounts', catalog_source: 'deposits', grain: false, as_of: false,
        stale: false, resolved: true,
      },
    ],
    edges: [],
    truncated: false,
  }

  it('requests the exact contract URL with the documented defaults', async () => {
    fetchMock.mockImplementation(ok(GRAPH))
    const result = await lineageGraph('public.accounts.balance', 'deposits')
    expect(result).toEqual(GRAPH)
    const [url] = fetchMock.mock.calls[0]
    // Pinned byte-for-byte: direction=both, depth=1, all three layers, commas unencoded.
    expect(url).toBe(
      '/graph/lineage?ref=public.accounts.balance&source=deposits'
        + '&direction=both&depth=1&layers=joins,entity,features',
    )
  })

  it('carries direction, depth, and a layers subset when given', async () => {
    fetchMock.mockImplementation(ok(GRAPH))
    await lineageGraph('public.accounts', 'deposits', {
      direction: 'up', depth: 3, layers: ['joins', 'features'],
    })
    const [url] = fetchMock.mock.calls[0]
    expect(url).toBe(
      '/graph/lineage?ref=public.accounts&source=deposits'
        + '&direction=up&depth=3&layers=joins,features',
    )
  })

  it('percent-encodes hostile ref and source values', async () => {
    fetchMock.mockImplementation(ok(GRAPH))
    await lineageGraph('public.a&b', 'dep osits')
    const [url] = fetchMock.mock.calls[0]
    expect(url).toBe(
      '/graph/lineage?ref=public.a%26b&source=dep%20osits'
        + '&direction=both&depth=1&layers=joins,entity,features',
    )
  })

  it('surfaces the 404 for unknown or read-scope-hidden anchors as ApiError', async () => {
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail: "unknown object 'public.x' in source 'deposits'" }),
        { status: 404 }))
    await expect(lineageGraph('public.x', 'deposits')).rejects.toMatchObject({
      status: 404, detail: "unknown object 'public.x' in source 'deposits'" })
  })
})

// One OpenMetadata instance exactly as the wire returns it: the token env-var REFERENCE plus
// whether it is set — never the token value itself.
const INTEGRATION: Integration = {
  integration_id: 'intg_01HZXAAAAAAAAAAAAAAAAAAAAA',
  name: 'Corporate OpenMetadata',
  base_url: 'https://om.internal.test',
  token_env: 'FEATUREGEN_OM_TOKEN__CORP',
  tag_map: { 'PII.Sensitive': 'pii' },
  created_by: 'user:o',
  created_at: '2026-07-09T12:00:00+00:00',
  token_present: true,
}

const SYNC: Sync = {
  sync_id: 'sync_01HZYBBBBBBBBBBBBBBBBBBBBB',
  integration_id: INTEGRATION.integration_id,
  service_name: 'mysql_prod',
  database_filter: 'cards_db',
  schema_filter: 'public',
  target_source: 'cards',
  tag_map_override: { 'Confidential.Internal': 'restricted' },
  table_naming: 'table',
  created_by: 'user:o',
  created_at: '2026-07-09T12:05:00+00:00',
  last_import_at: null,
}

const SNAPSHOT_HASH = 'ab'.repeat(32)
const BASELINE_HASH = 'ef'.repeat(32)

const SYNC_PREVIEW: SyncPreview = {
  summary: {
    tables: 3, columns: 14, new: 3, changed: 0, unchanged: 0, removed: 0,
    would_quarantine: 1, semantics_pending: 13,
  },
  tag_map: [
    { om_tag: 'Confidential.Internal', mapped_to: '', unmapped: true, count: 1 },
    { om_tag: 'PII.Sensitive', mapped_to: 'pii', unmapped: false, count: 1 },
  ],
  tables: [
    {
      table: 'accounts', status: 'new', columns: 4,
      quarantine: [{
        column: 'ssn',
        reason: "unrecognized sensitivity 'Confidential.Internal' (expected one of: pii, restricted)",
      }],
      changes: [],
    },
    { table: 'cards', status: 'new', columns: 4, quarantine: [], changes: [] },
    { table: 'transactions', status: 'new', columns: 6, quarantine: [], changes: [] },
  ],
  collisions: [],
  dropped_joins: [],
  brake: { would_hold: false, reason: null },
  as_of_suggestions: [
    { table: 'accounts', column: 'opened_on', hint: 'partition column (TIME-UNIT)' },
    { table: 'transactions', column: 'posted_at', hint: 'timestamp column named like a time axis' },
  ],
  snapshot_hash: SNAPSHOT_HASH,
  local_baseline_hash: BASELINE_HASH,
}

describe('integration client (tier 1)', () => {
  it('listIntegrations GETs /integrations and the rows carry no token value, only the reference', async () => {
    fetchMock.mockImplementation(ok([INTEGRATION]))
    const result = await listIntegrations()
    expect(result).toEqual([INTEGRATION])
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/integrations')
    expect(init.method).toBeUndefined()
    expect(Object.keys(result[0])).not.toContain('token')
  })

  it('getIntegration GETs /integrations/{id} with the id percent-encoded', async () => {
    fetchMock.mockImplementation(ok(INTEGRATION))
    await getIntegration('intg a/b')
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/integrations/intg%20a%2Fb')
    expect(init.method).toBeUndefined()
  })

  it('createIntegration posts exactly name+base_url+tag_map — never a token field, token_env derived server-side', async () => {
    fetchMock.mockImplementation(ok(INTEGRATION))
    const result = await createIntegration({
      name: 'Corporate OpenMetadata',
      base_url: 'https://om.internal.test',
      tag_map: { 'PII.Sensitive': 'pii' },
    })
    expect(result).toEqual(INTEGRATION)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/integrations')
    expect(init.method).toBe('POST')
    // Pinned byte-for-byte: the declared fields only (the server 422s any extra field, precisely
    // so a plaintext token can never ride along), token_env omitted so the server derives it.
    expect(JSON.parse(init.body)).toEqual({
      name: 'Corporate OpenMetadata',
      base_url: 'https://om.internal.test',
      tag_map: { 'PII.Sensitive': 'pii' },
    })
    expect(init.body).not.toMatch(/"token"/)
  })

  it('createIntegration defaults tag_map to {} and carries token_env only when named', async () => {
    fetchMock.mockImplementation(ok(INTEGRATION))
    await createIntegration({
      name: 'Corporate OpenMetadata', base_url: 'https://om.internal.test',
      token_env: 'FEATUREGEN_OM_TOKEN__CORP',
    })
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init.body)).toEqual({
      name: 'Corporate OpenMetadata', base_url: 'https://om.internal.test', tag_map: {},
      token_env: 'FEATUREGEN_OM_TOKEN__CORP',
    })
  })

  it('patchIntegration PATCHes only the fields the caller changed', async () => {
    fetchMock.mockImplementation(ok(INTEGRATION))
    await patchIntegration(INTEGRATION.integration_id, { base_url: 'https://om2.internal.test' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`/integrations/${INTEGRATION.integration_id}`)
    expect(init.method).toBe('PATCH')
    // Undefined keys are dropped: the server merges each provided field over the current row and
    // re-validates the whole result.
    expect(JSON.parse(init.body)).toEqual({ base_url: 'https://om2.internal.test' })
  })

  it('deleteIntegration issues DELETE with the id percent-encoded', async () => {
    fetchMock.mockImplementation(ok({ deleted: true }))
    const result = await deleteIntegration('intg a/b')
    expect(result).toEqual({ deleted: true })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/integrations/intg%20a%2Fb')
    expect(init.method).toBe('DELETE')
  })

  it('surfaces a duplicate-name 409 as ApiError', async () => {
    const detail = "integration 'Corporate OpenMetadata' already exists"
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail }), { status: 409 }))
    await expect(createIntegration({
      name: 'Corporate OpenMetadata', base_url: 'https://om.internal.test',
    })).rejects.toMatchObject({ status: 409, detail })
  })
})

describe('service discovery', () => {
  it('discoverServices GETs the live-OM services list flagged with sync bindings', async () => {
    const services: DiscoveredService[] = [
      {
        service_name: 'mysql_prod', service_type: 'Mysql', fqn: 'mysql_prod',
        synced: true, sync_id: SYNC.sync_id,
      },
      {
        service_name: 'bq_marketing', service_type: 'BigQuery', fqn: 'bq_marketing',
        synced: false, sync_id: null,
      },
    ]
    fetchMock.mockImplementation(ok(services))
    const result = await discoverServices(INTEGRATION.integration_id)
    expect(result).toEqual(services)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`/integrations/${INTEGRATION.integration_id}/services`)
    expect(init.method).toBeUndefined()
  })

  it('surfaces an OM-unreachable 502 on discovery as ApiError', async () => {
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail: 'OpenMetadata request failed: connect timeout' }),
        { status: 502 }))
    await expect(discoverServices(INTEGRATION.integration_id)).rejects.toMatchObject({
      status: 502 })
  })
})

describe('sync client (tier 2)', () => {
  it('listSyncs GETs the integration-scoped syncs', async () => {
    fetchMock.mockImplementation(ok([SYNC]))
    const result = await listSyncs(INTEGRATION.integration_id)
    expect(result).toEqual([SYNC])
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`/integrations/${INTEGRATION.integration_id}/syncs`)
    expect(init.method).toBeUndefined()
  })

  it('getSync GETs the nested sync path with both ids percent-encoded', async () => {
    fetchMock.mockImplementation(ok(SYNC))
    await getSync('intg a', 'sync b')
    const [url] = fetchMock.mock.calls[0]
    expect(url).toBe('/integrations/intg%20a/syncs/sync%20b')
  })

  it('createSync posts the full declared sync body with optional-field defaults filled in', async () => {
    fetchMock.mockImplementation(ok(SYNC))
    const result = await createSync(INTEGRATION.integration_id, {
      service_name: 'mysql_prod',
      target_source: 'cards',
      database_filter: 'cards_db',
      schema_filter: 'public',
      tag_map_override: { 'Confidential.Internal': 'restricted' },
    })
    expect(result).toEqual(SYNC)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`/integrations/${INTEGRATION.integration_id}/syncs`)
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({
      service_name: 'mysql_prod',
      target_source: 'cards',
      database_filter: 'cards_db',
      schema_filter: 'public',
      tag_map_override: { 'Confidential.Internal': 'restricted' },
      table_naming: 'table',
    })
  })

  it('createSync defaults optional scope, override, and table naming', async () => {
    fetchMock.mockImplementation(ok(SYNC))
    await createSync(INTEGRATION.integration_id, {
      service_name: 'bq_marketing', target_source: 'marketing',
    })
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init.body)).toEqual({
      service_name: 'bq_marketing', target_source: 'marketing',
      database_filter: null, schema_filter: null, tag_map_override: null, table_naming: 'table',
    })
  })

  it('patchSync PATCHes only the changed fields on the nested path', async () => {
    fetchMock.mockImplementation(ok(SYNC))
    await patchSync(INTEGRATION.integration_id, SYNC.sync_id, {
      tag_map_override: { 'Confidential.Internal': 'restricted', 'Tier.Tier1': '' },
    })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`/integrations/${INTEGRATION.integration_id}/syncs/${SYNC.sync_id}`)
    expect(init.method).toBe('PATCH')
    expect(JSON.parse(init.body)).toEqual({
      tag_map_override: { 'Confidential.Internal': 'restricted', 'Tier.Tier1': '' },
    })
  })

  it('deleteSync issues DELETE on the nested path', async () => {
    fetchMock.mockImplementation(ok({ deleted: true }))
    const result = await deleteSync(INTEGRATION.integration_id, SYNC.sync_id)
    expect(result).toEqual({ deleted: true })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`/integrations/${INTEGRATION.integration_id}/syncs/${SYNC.sync_id}`)
    expect(init.method).toBe('DELETE')
  })

  it('surfaces the one-per-service 409 as ApiError', async () => {
    const detail = "a sync for service 'mysql_prod' already exists on this integration"
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail }), { status: 409 }))
    await expect(createSync(INTEGRATION.integration_id, {
      service_name: 'mysql_prod', target_source: 'cards',
    })).rejects.toMatchObject({ status: 409, detail })
  })
})

describe('sync preview/import client', () => {
  it('previewSync POSTs /syncs/{id}/preview with NO body and returns the dry run untouched', async () => {
    fetchMock.mockImplementation(ok(SYNC_PREVIEW))
    const result = await previewSync(SYNC.sync_id)
    expect(result).toEqual(SYNC_PREVIEW)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`/syncs/${SYNC.sync_id}/preview`)
    expect(init.method).toBe('POST')
    // No request body: the sync and its integration carry URL, token, scope, and effective map.
    expect(init.body).toBeUndefined()
  })

  it('importSync POSTs the previewed snapshot hash to /syncs/{id}/import', async () => {
    fetchMock.mockImplementation(ok({
      result: {
        status: 'ingested', reason: null, asserted: 3, changed_objects: 0, quarantined: 1, flagged: null,
      },
      import_id: 'omimp_01HZY',
      semantics_pending: 13,
    }))
    const result = await importSync(SYNC.sync_id, SNAPSHOT_HASH, BASELINE_HASH)
    expect(result.import_id).toBe('omimp_01HZY')
    expect(result.semantics_pending).toBe(13)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`/syncs/${SYNC.sync_id}/import`)
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({
      snapshot_hash: SNAPSHOT_HASH,
      local_baseline_hash: BASELINE_HASH,
    })
  })

  it('surfaces the snapshot-mismatch 409 with the backend re-preview guidance', async () => {
    const detail = 'OpenMetadata changed since this preview (snapshot hash mismatch). '
      + 'Run preview again and approve the fresh dry run.'
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail }), { status: 409 }))
    await expect(importSync(SYNC.sync_id, SNAPSHOT_HASH, BASELINE_HASH)).rejects.toMatchObject({
      status: 409, detail })
  })

  it('surfaces the unconfigured-token 400 with the env-var instruction', async () => {
    const detail = 'integration token is not configured: set the FEATUREGEN_OM_TOKEN__CORP '
      + 'environment variable'
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ detail }), { status: 400 }))
    await expect(previewSync(SYNC.sync_id)).rejects.toMatchObject({ status: 400, detail })
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
  'advisory only: a fit/coverage judgment over the metadata, not a performance prediction; '
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
      catalog_source: 'deposits', entity: null, target_ref: null, objective: null,
    })
  })

  it('refineCandidate carries the round objective when given', async () => {
    fetchMock.mockImplementation(ok({ revised: IDEA }))
    await refineCandidate(
      { name: 'avg_balance_90d' }, 'use a 30 day window', 'deposits', null, null,
      'predict churn')
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init.body).objective).toBe('predict churn')
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

describe('governed contract flow client', () => {
  it('contractConsideredSet posts the brief to /contract/considered-set', async () => {
    fetchMock.mockImplementation(ok({ intent_id: 'int_1', anchor: null, alternatives: [],
      recommendation: null }))
    await contractConsideredSet('balance drains then they leave', 'predict retail churn',
      { entity: 'customer' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/contract/considered-set')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toMatchObject({
      hypothesis: 'balance drains then they leave', objective: 'predict retail churn',
      entity: 'customer' })
  })

  it('contractDraft posts the Gate-1 choice to /contract/draft', async () => {
    fetchMock.mockImplementation(ok({ draft: {}, unresolved: [], intent_id: 'int_1' }))
    await contractDraft('int_1', 'anchor', 'balance_trend_90d', 'best fit')
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/contract/draft')
    expect(JSON.parse(init.body)).toEqual({
      intent_id: 'int_1', chosen_source: 'anchor', chosen_option_id: 'balance_trend_90d',
      why: 'best fit' })
  })

  it('contractConfirm merges the draft with the intent_id', async () => {
    fetchMock.mockImplementation(ok({ contract_id: 'c1', feature_id: 'f1',
      feature_name: 'balance_trend_90d', version: 1 }))
    const draft: ContractDraft = { feature_name: 'balance_trend_90d', definition: 'slope of balance',
      grain_table: 'accounts', aggregation: 'trend', as_of_column: 'snapshot_date',
      derives_from: ['balance_gbp'], target_ref: 'churned',
      derives_pairs: [['retail_core', 'balance_gbp']], join_path: [] }
    const c = await contractConfirm(draft, 'int_1')
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/contract/confirm')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toMatchObject({ feature_name: 'balance_trend_90d',
      intent_id: 'int_1', derives_pairs: [['retail_core', 'balance_gbp']] })
    expect(c.contract_id).toBe('c1')
  })

  it('listContracts GETs the governed inventory', async () => {
    fetchMock.mockImplementation(ok([]))
    await listContracts(25)
    const [url] = fetchMock.mock.calls[0]
    expect(url).toBe('/contracts?limit=25')
  })
})
