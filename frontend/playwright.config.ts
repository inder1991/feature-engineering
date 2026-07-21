import { defineConfig, devices } from 'playwright/test'

// Playwright DESKTOP e2e gate for the asset experience (Delivery G). This is the GO-LIVE path: the
// spec hits the REAL FastAPI backend (NO page.route mocking) and SEEDS its own catalog asset through
// the real POST /uploads API before searching + navigating to Details. See e2e/asset-experience.spec.ts.
//
// DESKTOP ONLY — one project (Desktop Chrome). NO mobile project/viewport (explicit user scope
// decision). The three desktop assertions are: search -> Details navigation renders the real asset
// detail; the SVG neighborhood graph is nonblank; and the document never scrolls horizontally.
//
// The `playwright` package (1.61.1) bundles the test runner, so `test`/`expect`/`defineConfig`/
// `devices` all import from `playwright/test` — there is NO separate `@playwright/test` dependency.
//
// ---- The real stack this config expects ------------------------------------------------------
// `webServer` is an ARRAY that brings up BOTH tiers for the run:
//   (1) the backend  — uvicorn --factory over the app, with FEATUREGEN_AUTH_STUB=1 (so the browser's
//       X-User/X-Roles headers authenticate — the stub is OFF by default), FEATUREGEN_AUTO_MIGRATE=1
//       (apply pending migrations at startup so /health is 200 ok), and a TEST FEATUREGEN_DSN.
//   (2) the frontend — the Vite dev server, whose proxy forwards /uploads, /search, /catalog, … to
//       the backend, so the browser (and the seed request) reach the API same-origin.
// Playwright waits for each `url` before running the seed + tests. Postgres is the ONE dependency
// `webServer` cannot itself start — it must already be running and reachable at FEATUREGEN_DSN.
//
// reuseExistingServer is on OUTSIDE CI, so locally you can leave a backend + `npm run dev` already
// up and Playwright will attach to them instead of spawning new ones.
const FRONTEND_PORT = Number(process.env.E2E_FRONTEND_PORT ?? 5173)
const BACKEND_PORT = Number(process.env.E2E_BACKEND_PORT ?? 8000)
const BASE_URL = process.env.E2E_BASE_URL ?? `http://127.0.0.1:${FRONTEND_PORT}`
// A TEST database — NEVER a production DSN. Override via FEATUREGEN_DSN in CI.
const DSN = process.env.FEATUREGEN_DSN ?? `postgresql://localhost:5432/featuregen_e2e`

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: 'list',
  timeout: 60_000,
  expect: { timeout: 10_000 },

  use: {
    baseURL: BASE_URL,
    // The Playwright context (browser navigations, page fetch/XHR, AND the `request` fixture the
    // seed uses) carries the app's auth headers as an authorized platform_admin session — the same
    // X-User/X-Roles the API resolves roles from (deps.get_identity, stub mode). platform_admin
    // grants every permission the flow touches: catalog:write (seed upload), catalog:read (search +
    // asset detail). The app's own fetch also stamps its dev-session role (data_owner, which itself
    // grants catalog:read), so the browser reads are authorized under any header-merge outcome.
    extraHTTPHeaders: {
      'X-User': 'e2e-platform-admin',
      'X-Roles': 'platform_admin',
    },
    trace: 'on-first-retry',
  },

  // DESKTOP ONLY. No mobile project (user scope decision).
  projects: [
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: [
    {
      // The REAL backend. Run from the repo root (the `featuregen` package must be installed, e.g.
      // `uv sync` / `pip install -e .`, so `featuregen.api.app` imports). No LLM provider is set, so
      // ingest runs un-enriched — sufficient for a real graph_node + asset detail.
      command:
        `uvicorn --factory featuregen.api.app:create_app_from_env `
        + `--host 127.0.0.1 --port ${BACKEND_PORT}`,
      cwd: '..',
      url: `http://127.0.0.1:${BACKEND_PORT}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
      env: {
        FEATUREGEN_DSN: DSN,
        FEATUREGEN_AUTH_STUB: '1',
        FEATUREGEN_AUTO_MIGRATE: '1',
      },
    },
    {
      // The frontend (Vite dev server + its API proxy to the backend).
      command: `npm run dev -- --host 127.0.0.1 --port ${FRONTEND_PORT} --strictPort`,
      url: BASE_URL,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
})
