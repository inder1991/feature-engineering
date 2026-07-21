import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { type Page, expect, test } from 'playwright/test'

// DESKTOP e2e for the asset experience (Delivery G) against the REAL backend — NO page.route
// mocking. The go-live path: beforeAll SEEDS a real catalog asset by uploading a fixture CSV
// through the REAL POST /uploads API (via the app origin, so the Vite proxy forwards it to the
// backend); the test then searches for it and navigates to Details against the REAL
// GET /catalog/assets read model. Three desktop assertions: search -> Details navigation renders
// the real asset detail; the SVG neighborhood graph is nonblank; the document never scrolls
// horizontally. DESKTOP ONLY (see playwright.config.ts — one project, no mobile viewport).

const SOURCE = 'e2e_asset_experience'
const TABLE = 'e2e_accounts'

const here = dirname(fileURLToPath(import.meta.url))
const fixtureCsv = readFileSync(join(here, 'fixtures', 'e2e_accounts.csv'))

// A desktop invariant: the page body must never scroll sideways. Asserted on each screen visited.
async function expectNoHorizontalOverflow(page: Page, where: string): Promise<void> {
  const metrics = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }))
  expect(
    metrics.scrollWidth,
    `${where}: document should not overflow horizontally on desktop `
      + `(scrollWidth ${metrics.scrollWidth} > clientWidth ${metrics.clientWidth})`,
  ).toBeLessThanOrEqual(metrics.clientWidth)
}

test.describe('asset experience — desktop, real backend, seeded via upload', () => {
  // SEED through the REAL upload API. The `request` fixture inherits the config's baseURL (the app
  // origin) + extraHTTPHeaders (platform_admin), so this is exactly the app's own upload call:
  // real ingest -> real graph_node -> real asset detail. Idempotent (a re-upload rebuilds the
  // source), so repeated runs against a persistent test DB stay clean.
  test.beforeAll(async ({ request }) => {
    const res = await request.post('/uploads', {
      multipart: {
        source: SOURCE,
        file: { name: 'e2e_accounts.csv', mimeType: 'text/csv', buffer: fixtureCsv },
      },
    })
    expect(
      res.ok(),
      `seed upload failed: HTTP ${res.status()} — ${await res.text()}`,
    ).toBeTruthy()
    // POST /uploads returns 200 for 'held'/'rejected' too, so res.ok() alone would let a seed that
    // parsed-but-did-not-ingest through — leaving the e2e asserting stale data. Assert the ingest
    // actually LANDED (status 'ingested'), surfacing the whole body if it did not.
    const body = await res.json()
    expect(body.status, JSON.stringify(body)).toBe('ingested')
  })

  test('search -> Details renders the real asset detail, a nonblank graph, and no overflow', async ({
    page,
  }) => {
    // --- search for the seeded asset (REAL GET /search) ---
    await page.goto('/#/search')
    await page.getByRole('textbox', { name: 'Query' }).fill(TABLE)
    await page.getByRole('button', { name: 'Search' }).click()

    // A real hit for the seeded asset surfaces its Details action (aria-label carries the object_ref,
    // e.g. "Details for public.e2e_accounts.balance").
    const details = page.getByRole('button', { name: new RegExp(`^Details for .*${TABLE}`) })
    await expect(details.first()).toBeVisible()
    await details.first().click()

    // --- navigation: the asset route + the REAL asset detail rendered ---
    // The hash carries the hit's own catalog_source; the detail heading is the asset's table[.column]
    // built from the BACKEND response (identity), not a fixture.
    await expect(page).toHaveURL(new RegExp(`#/asset\\?.*source=${SOURCE}`))
    await expect(page.getByRole('heading', { name: new RegExp(TABLE) })).toBeVisible()

    // no horizontal overflow on the asset detail (default overview tab)
    await expectNoHorizontalOverflow(page, 'asset detail (overview)')

    // --- relationships tab: nonblank SVG neighborhood graph ---
    await page
      .getByRole('group', { name: /asset sections/i })
      .getByRole('button', { name: 'Relationships' })
      .click()

    const graph = page.getByRole('img', { name: /neighborhood graph/i })
    await expect(graph).toBeVisible()

    // Nonblank: the anchor node always renders (>=1 node with a non-zero bounding box), so the canvas
    // is never empty.
    const anchor = page.locator('.adg-node--anchor')
    await expect(anchor).toBeVisible()
    const box = await anchor.boundingBox()
    expect(box, 'neighborhood graph anchor node should have a bounding box').not.toBeNull()
    expect(box?.width ?? 0).toBeGreaterThan(0)
    expect(box?.height ?? 0).toBeGreaterThan(0)
    // The parallel a11y list mirrors the anchor, confirming rendered content (not just an empty svg).
    await expect(page.locator('.adg-graph-a11y')).toContainText(TABLE)

    // no horizontal overflow on the widest (relationships / graph) tab either
    await expectNoHorizontalOverflow(page, 'asset detail (relationships)')
  })
})
