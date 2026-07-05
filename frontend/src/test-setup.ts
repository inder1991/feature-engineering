import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// RTL auto-cleanup only self-registers when a global afterEach exists (Vitest globals: true).
// This config runs without globals, so unmount rendered trees between tests explicitly —
// otherwise DOM from earlier tests accumulates and queries find multiple matches.
afterEach(cleanup)
