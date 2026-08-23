import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { parseHash, useHashRoute } from './nav'

beforeEach(() => {
  window.location.hash = ''
})

describe('useHashRoute', () => {
  it('defaults to overview for an empty hash', () => {
    const { result } = renderHook(() => useHashRoute())
    expect(result.current.route).toBe('overview')
  })

  it('defaults to overview for an unknown hash', () => {
    window.location.hash = '#/nope'
    const { result } = renderHook(() => useHashRoute())
    expect(result.current.route).toBe('overview')
  })

  it('parses the route and query params from the hash', () => {
    window.location.hash = '#/review?source=deposits'
    const { result } = renderHook(() => useHashRoute())
    expect(result.current.route).toBe('review')
    expect(result.current.params.get('source')).toBe('deposits')
  })

  it('resolves the integrations route', () => {
    window.location.hash = '#/integrations'
    const { result } = renderHook(() => useHashRoute())
    expect(result.current.route).toBe('integrations')
  })

  it('resolves the semantics route with its ?source= param', () => {
    window.location.hash = '#/semantics?source=cards'
    const { result } = renderHook(() => useHashRoute())
    expect(result.current.route).toBe('semantics')
    expect(result.current.params.get('source')).toBe('cards')
  })

  it('reacts to hashchange events', () => {
    const { result } = renderHook(() => useHashRoute())
    expect(result.current.route).toBe('overview')
    act(() => {
      window.location.hash = '#/search'
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(result.current.route).toBe('search')
  })

  it('navigate sets the hash, including params', () => {
    const { result } = renderHook(() => useHashRoute())
    act(() => {
      result.current.navigate('review', { source: 'deposits' })
    })
    expect(window.location.hash).toBe('#/review?source=deposits')
    expect(result.current.route).toBe('review')
    expect(result.current.params.get('source')).toBe('deposits')
  })

  it('navigate without params writes a bare route hash', () => {
    const { result } = renderHook(() => useHashRoute())
    act(() => {
      result.current.navigate('workbench')
    })
    expect(window.location.hash).toBe('#/workbench')
    expect(result.current.route).toBe('workbench')
  })

  it('resolves the asset route with its source + object_ref params', () => {
    window.location.hash = '#/asset?source=deposits&object_ref=public.accounts.balance'
    const { result } = renderHook(() => useHashRoute())
    expect(result.current.route).toBe('asset')
    expect(result.current.params.get('source')).toBe('deposits')
    expect(result.current.params.get('object_ref')).toBe('public.accounts.balance')
  })

  it('navigate round-trips the asset route carrying source + object_ref', () => {
    const { result } = renderHook(() => useHashRoute())
    act(() => {
      result.current.navigate('asset', {
        source: 'deposits', object_ref: 'schema/accounts.balance',
      })
    })
    expect(result.current.route).toBe('asset')
    expect(result.current.params.get('source')).toBe('deposits')
    // object_ref rides the query string — URLSearchParams encodes its slash on the way out and
    // decodes it back on read, so the pathful ref round-trips intact.
    expect(result.current.params.get('object_ref')).toBe('schema/accounts.balance')
  })

  it('resolves the suggested route with its source + table params', () => {
    window.location.hash = '#/suggested?source=core_banking&table=public.comp_fin_tran'
    const { result } = renderHook(() => useHashRoute())
    expect(result.current.route).toBe('suggested')
    expect(result.current.params.get('source')).toBe('core_banking')
    expect(result.current.params.get('table')).toBe('public.comp_fin_tran')
  })

  it('navigate accepts a URLSearchParams with repeated values for faceted deep links', () => {
    const { result } = renderHook(() => useHashRoute())
    act(() => {
      const p = new URLSearchParams()
      p.set('q', 'balance')
      p.append('source', 'deposits')
      p.append('source', 'cards')
      result.current.navigate('search', p)
    })
    expect(window.location.hash).toBe('#/search?q=balance&source=deposits&source=cards')
    expect(result.current.route).toBe('search')
    expect(result.current.params.getAll('source')).toEqual(['deposits', 'cards'])
    expect(result.current.params.get('q')).toBe('balance')
  })
})

// The runs route is the ONE path-param route: '#/runs' is the list, '#/runs/<id>' the detail.
// It is unflagged — GET /feature-runs is always-on, like the asset route's reads.
describe('the runs route', () => {
  it('parses #/runs as the runs list', () => {
    expect(parseHash('#/runs').route).toBe('runs')
  })

  it('parses #/runs/grun_x as detail with run_id param', () => {
    const { route, params } = parseHash('#/runs/grun_x')
    expect(route).toBe('runs')
    expect(params.get('run_id')).toBe('grun_x')
  })

  it('leaves the list with no run_id, so the list and the detail never collide', () => {
    expect(parseHash('#/runs').params.get('run_id')).toBeNull()
  })

  it('percent-decodes the id and keeps any query params alongside it', () => {
    const { route, params } = parseHash('#/runs/fgr%2Fa.b?tab=evidence')
    expect(route).toBe('runs')
    expect(params.get('run_id')).toBe('fgr/a.b')
    expect(params.get('tab')).toBe('evidence')
  })

  it('survives a malformed escape instead of crashing the render', () => {
    // decodeURIComponent throws on '%zz'; parseHash runs inside a render, so a corrupt link must
    // degrade to a 404-able id, never to a blank app.
    const { route, params } = parseHash('#/runs/grun_%zz')
    expect(route).toBe('runs')
    expect(params.get('run_id')).toBe('grun_%zz')
  })

  it('resolves #/runs through the hash hook too', () => {
    window.location.hash = '#/runs/grun_7'
    const { result } = renderHook(() => useHashRoute())
    expect(result.current.route).toBe('runs')
    expect(result.current.params.get('run_id')).toBe('grun_7')
  })
})

// Entity map v0 is flag-gated (VITE_ENTITY_MAP), same call-time pattern as the gate console:
// flag-off the hash parses like any unknown route — the screen is ABSENT, not broken.
describe('entity-map flag gating', () => {
  it('refuses #/entity-map when the flag is off', () => {
    vi.stubEnv('VITE_ENTITY_MAP', '')
    window.location.hash = '#/entity-map'
    const { result } = renderHook(() => useHashRoute())
    expect(result.current.route).toBe('overview')
    vi.unstubAllEnvs()
  })

  it('resolves #/entity-map when the flag is on', () => {
    vi.stubEnv('VITE_ENTITY_MAP', '1')
    window.location.hash = '#/entity-map'
    const { result } = renderHook(() => useHashRoute())
    expect(result.current.route).toBe('entity-map')
    vi.unstubAllEnvs()
  })
})
