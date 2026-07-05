import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getSession, setSession, subscribe } from './session'

beforeEach(() => setSession({ user: 'dev', roles: ['data_owner'] }))

describe('session store', () => {
  it('holds the dev session and notifies subscribers', () => {
    const listener = vi.fn()
    const unsubscribe = subscribe(listener)
    setSession({ user: 'ana', roles: ['pii_reader'] })
    expect(getSession()).toEqual({ user: 'ana', roles: ['pii_reader'] })
    expect(listener).toHaveBeenCalledOnce()
    unsubscribe()
    setSession({ user: 'bo', roles: [] })
    expect(listener).toHaveBeenCalledOnce()
  })
})
