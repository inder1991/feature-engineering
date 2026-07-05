import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import App from './App'
import { getSession, setSession } from './session'

beforeEach(() => setSession({ user: 'dev', roles: ['data_owner'] }))

describe('app shell', () => {
  it('renders the four screens as tabs, search by default', () => {
    render(<App />)
    for (const t of ['Upload', 'Search', 'Review queue', 'Workbench']) {
      expect(screen.getByRole('button', { name: t })).toBeInTheDocument()
    }
    expect(screen.getByRole('heading', { name: 'Search' })).toBeInTheDocument()
  })

  it('switches tabs', async () => {
    render(<App />)
    await userEvent.click(screen.getByRole('button', { name: 'Workbench' }))
    expect(screen.getByRole('heading', { name: 'Workbench' })).toBeInTheDocument()
  })

  it('session bar edits the stub session store', async () => {
    render(<App />)
    await userEvent.click(screen.getByRole('checkbox', { name: 'pii_reader' }))
    expect(getSession().roles).toContain('pii_reader')
    await userEvent.click(screen.getByRole('checkbox', { name: 'data_owner' }))
    expect(getSession().roles).not.toContain('data_owner')
  })
})
