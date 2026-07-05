import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import App from './App'
import { getSession, setSession } from './session'

beforeEach(() => {
  setSession({ user: 'dev', roles: ['data_owner'] })
  window.location.hash = ''
})

describe('app shell', () => {
  it('renders five nav items and lands on Overview by default', () => {
    render(<App />)
    const nav = within(screen.getByRole('navigation'))
    for (const t of ['Overview', 'Upload', 'Search', 'Review queue', 'Workbench']) {
      expect(nav.getByRole('button', { name: t })).toBeInTheDocument()
    }
    expect(screen.getByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'The loop' })).toBeInTheDocument()
  })

  it('nav click navigates and updates location.hash', async () => {
    render(<App />)
    const nav = within(screen.getByRole('navigation'))
    await userEvent.click(nav.getByRole('button', { name: 'Workbench' }))
    expect(window.location.hash).toBe('#/workbench')
    expect(screen.getByRole('heading', { level: 1, name: 'Workbench' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /feature workbench/i })).toBeInTheDocument()
  })

  it('deep-links a screen from the hash', () => {
    window.location.hash = '#/search'
    render(<App />)
    expect(screen.getByRole('heading', { level: 1, name: 'Search' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /search the catalog/i })).toBeInTheDocument()
  })

  it('overview start-here button navigates to Upload', async () => {
    render(<App />)
    await userEvent.click(screen.getByRole('button', { name: 'Go to Upload' }))
    expect(window.location.hash).toBe('#/upload')
    expect(screen.getByRole('heading', { level: 1, name: 'Upload' })).toBeInTheDocument()
  })

  it('overview loop links navigate to their screens', async () => {
    render(<App />)
    await userEvent.click(screen.getByRole('link', { name: 'Review queue' }))
    expect(window.location.hash).toBe('#/review')
    expect(screen.getByRole('heading', { level: 1, name: 'Review queue' })).toBeInTheDocument()
  })

  it('session chips edit the stub session store', async () => {
    render(<App />)
    await userEvent.click(screen.getByRole('checkbox', { name: 'pii_reader' }))
    expect(getSession().roles).toContain('pii_reader')
    await userEvent.click(screen.getByRole('checkbox', { name: 'data_owner' }))
    expect(getSession().roles).not.toContain('data_owner')
  })
})
