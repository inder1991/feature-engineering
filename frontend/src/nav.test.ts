import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { useHashRoute } from './nav'

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
