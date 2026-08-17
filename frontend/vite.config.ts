import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

const API = 'http://localhost:8000'
// Dev proxy: forward the API surface to the backend. The two-tier connector lives at
// /integrations (instances + their syncs + service discovery) and /syncs (preview + import) —
// the flat /connectors path was removed in the two-tier restructure. /contract(s) is the governed
// feature-contract flow; /graph is the lineage graph; /governance is the join confirm/reject
// surface (the list rides /sources/{source}/governance/joins, already covered by /sources);
// /gate is the authority-only Phase-3C.1 evaluation console (cohorts + evaluate). /catalog is the
// asset detail read model + field-correction command (GET/POST /catalog/assets/... — Delivery
// F/G); /ingestion-runs is the per-stage run record (GET /ingestion-runs/{id}). Both are real
// paths the client calls (api.ts getAssetDetail/postFieldDecision/getIngestionRun) — without them
// the browser's asset-detail request hits the Vite server itself and 404s instead of the backend.
const API_PATHS = ['/uploads', '/search', '/sources', '/columns', '/join-path', '/features',
  '/contract', '/contracts', '/graph', '/health', '/integrations', '/syncs', '/governance',
  '/gate', '/catalog', '/catalogs', '/ingestion-runs',
  // The data agent's planning surface, and the learning-gap queue.
  '/analysis', '/learning',
  // The connection/catalog registry (api.ts getDataSourceConnections/getDataSourceCatalogs).
  // nginx.conf has carried this since the screen shipped; this list did not, so the dev server
  // answered those calls itself instead of proxying them.
  '/data-sources',
  // Phase G's materialization trigger + status. No screen calls it yet — it is here because
  // nginx.conf and this list must agree, and because a missing prefix answers a POST with 405
  // from the SPA rather than a 404 anyone would recognise as routing.
  '/materialization-runs',
  // BR-23's recipe-review surface: the summary queue, the definition under review, and the
  // review history + decision POST (api.ts getRecipeReviewSummary/getRecipeDetail/
  // getRecipeReviews/postRecipeReview).
  '/recipes',
  // S11's execution workspace: authorize a generation, read the generated code, request a sandbox
  // verification, publish. Behind the SAME server switch as /materialization-runs, and listed here
  // for the same reason — a missing prefix answers a POST with 405 from the SPA rather than a 404
  // anyone would recognise as routing.
  '/feature-execution']

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(API_PATHS.map(p => [p, API])),
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test-setup.ts',
    // Unit tests live under src/ as *.test.ts(x). Scope collection there so Vitest's default glob
    // never picks up the Playwright e2e specs (e2e/*.spec.ts) — those run under `playwright test`,
    // not Vitest, and calling test.describe() from Playwright under Vitest throws.
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
