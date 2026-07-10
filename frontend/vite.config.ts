import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

const API = 'http://localhost:8000'
// Dev proxy: forward the API surface to the backend. The two-tier connector lives at
// /integrations (instances + their syncs + service discovery) and /syncs (preview + import) —
// the flat /connectors path was removed in the two-tier restructure. /contract(s) is the governed
// feature-contract flow; /graph is the lineage graph.
const API_PATHS = ['/uploads', '/search', '/sources', '/columns', '/join-path', '/features',
  '/contract', '/contracts', '/graph', '/health', '/integrations', '/syncs']

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(API_PATHS.map(p => [p, API])),
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test-setup.ts',
  },
})
