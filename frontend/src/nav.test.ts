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
