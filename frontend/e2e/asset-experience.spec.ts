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

// A SECOND seeded source whose table, columns and definitions are deliberately long. Catalog
// identifiers are unbounded strings from an upload, and they are what the suggestion card's layout
// rules exist to survive — see the `.sfc` block in index.css, which allows every badge to wrap,
// sets `min-width: 0` on every flex/grid track holding one, and breaks refs with
// `overflow-wrap: anywhere` so a hostile label makes a card TALLER, never wider.
const WIDE_SOURCE = 'e2e_wide_labels'
const WIDE_TABLE = 'e2e_counterparty_settlement_exposure_reconciliation_daily_snapshot'

const here = dirname(fileURLToPath(import.meta.url))
const fixtureCsv = readFileSync(join(here, 'fixtures', 'e2e_accounts.csv'))
const wideCsv = readFileSync(join(here, 'fixtures', 'e2e_wide_labels.csv'))

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
    // Two "Search" buttons exist since the nav gained one (drift caught 2026-08-14): scope
    // to the search FORM's own submit, not the nav entry.
    await page.locator('main').getByRole('button', { name: 'Search', exact: true }).click()

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

// The suggested-features surface, which the suite above never visits. Its own describe because it
// seeds a DIFFERENT source: one whose identifiers are long enough to break the card's wrapping
// rules if they ever regress.
//
// NOT a mobile project — the config's desktop-only scope decision stands, and no device emulation
// is used here. The widths below are ordinary desktop window sizes (a half-screen window is about
// 700px), chosen to sit either side of the card block's own `max-width: 720px` branch, which
// collapses the label columns and is otherwise entirely unexercised.
test.describe('suggested features — desktop, real backend, hostile-length catalog labels', () => {
  test.beforeAll(async ({ request }) => {
    const res = await request.post('/uploads', {
      multipart: {
        source: WIDE_SOURCE,
        file: { name: 'e2e_wide_labels.csv', mimeType: 'text/csv', buffer: wideCsv },
      },
    })
    expect(res.ok(), `seed upload failed: HTTP ${res.status()} — ${await res.text()}`).toBeTruthy()
    const body = await res.json()
    expect(body.status, JSON.stringify(body)).toBe('ingested')
  })

  test('long catalog labels never scroll the page sideways, at desktop or narrow', async ({
    page,
  }) => {
    await page.goto(`/#/suggested?source=${WIDE_SOURCE}&table=${WIDE_TABLE}`)

    // NON-VACUITY GUARD. The summary group renders only once the REAL v2 route has answered for a
    // table this catalog holds, so this fails loudly if the seed did not land, if the deployment
    // does not serve contract_version=2, or if the read was refused — rather than letting the
    // overflow assertions below pass against a blank page or an error callout.
    await expect(page.getByRole('group', { name: /suggestion summary/i })).toBeVisible()
    // The anchor line always carries the unbounded table ref and catalog source as mono text, so
    // there is always at least one hostile string on the page for the assertions to bite on.
    await expect(page.getByText(WIDE_TABLE).first()).toBeVisible()
    // DRIFT NOTE (2026-08-14): the legacy per-table template pass no longer yields hits on
    // an UN-ENRICHED upload (suggestions became meaning-driven), so the `.sfc` card block is
    // honestly absent here — the page states WHY ("no column carries the business meaning a
    // recipe needs"), which is itself the contract this seed now exercises. The overflow
    // rules this case exists for still bite: the anchor line renders the unbounded table ref
    // (asserted above), and the honest-empty explanation renders alongside it. The ≥1-card
    // variant returns with the shared-carrier page once a meaning-bearing seed exists (the
    // legacy pass itself retires at the E4 cutover).
    await expect(page.getByText(/no column on this table carries/i)).toBeVisible()

    await expectNoHorizontalOverflow(page, 'suggested features (default desktop)')

    // 900px: the app's other max-width:900 breakpoints are active, the card block's is not.
    // 700px: below the card block's own 720px collapse.
    for (const width of [900, 700]) {
      await page.setViewportSize({ width, height: 1000 })
      await expectNoHorizontalOverflow(page, `suggested features (${width}px wide)`)
    }

    // Open every disclosure: the drawer carries the widest content on the surface — full refs,
    // content hashes and revision ids — and none of it is on the page until it is expanded.
    await page.setViewportSize({ width: 700, height: 1000 })
    for (const toggle of await page.getByRole('button', { name: /show full detail/i }).all()) {
      await toggle.click()
    }
    await expectNoHorizontalOverflow(page, 'suggested features (700px, every drawer open)')
  })
})
