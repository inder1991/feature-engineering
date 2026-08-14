import { execFileSync } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test } from 'playwright/test'

// E2 — the Workbench journey against the REAL backend (the e2e factory: real routes, real
// Postgres, real activation policy; only the MODEL client is scripted — zero provider spend).
//
// The journey is the E0 walkthrough as a HUMAN drives it: hypothesis → recognition → confirm
// scope → cards served with REAL blockers → the blocked selection is disabled with the named
// next step → clear the blockers through the REAL surfaces (funnel + reviews, via the same
// HTTP routes) → regenerate with the UOA one-click yes → select → govern → the signed
// contract renders. Keyboard/focus assertions ride the selection, the blocker tooltip, and
// the decision-record drawer.
//
// Prereqs (documented, same as asset-experience): Postgres reachable at FEATUREGEN_DSN;
// `uv sync` done at the repo root. The seed runs the repo's own script via `uv run`.

// A FRESH source per run: the funnel confirmations and reviews are durable (append-only by
// design), so a reused test DB would arrive pre-cleared and the blocked-state assertions
// would be vacuous. A per-run source restores the freshly-ingested state every time.
const SOURCE = `e2e_semantic_${Date.now()}`
const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = join(here, '..', '..')

const GOV_HEADERS = { 'X-User': 'e2e-platform-admin', 'X-Roles': 'platform_admin,platform-admin' }

test.describe('workbench journey — real backend, scripted model, real blockers', () => {
  test.beforeAll(async () => {
    execFileSync('uv', ['run', 'python', 'scripts/e2e_seed_semantic.py'], {
      cwd: repoRoot,
      env: {
        ...process.env,
        E2E_SEMANTIC_SOURCE: SOURCE,
        FEATUREGEN_DSN: process.env.FEATUREGEN_DSN
          ?? 'postgresql://localhost:5432/featuregen_e2e',
      },
      stdio: 'inherit',
    })
  })

  test('the whole journey: blocked → cleared through real surfaces → governed', async ({ page, request }) => {
    test.setTimeout(180_000)

    // ── 1. Hypothesis in; the recognition panel proposes the scope. ──────────────────────
    await page.goto('/#workbench')
    await page.getByLabel('Hypothesis').fill(
      'complaints in the last 90 days precede churn')
    await page.getByLabel('Prediction goal').fill('predict churn')
    await page.getByLabel('Catalog source').fill(SOURCE)
    await page.getByRole('button', { name: /generate candidate sets/i }).click()
    await page.getByRole('button', { name: /confirm scope and generate/i }).click()

    // ── 2. Cards serve with REAL blockers: the hero's checkbox is DISABLED and its
    // tooltip names the funnel step. Keyboard: the disabled control is skipped by tab
    // order; the tooltip text is the server's own next step. ─────────────────────────────
    const heroCheckbox = page.getByRole('checkbox', { name: /Select Complaints/i }).first()
    await expect(heroCheckbox).toBeDisabled()
    await expect(heroCheckbox).toHaveAttribute(
      'title', /confirm the AI-proposed concept/i)

    // ── 3. Clear every blocker through its REAL surface — the same HTTP routes a human's
    // clicks call: the funnel batch, then the three-role review at the live revision. ────
    const queue = await (await request.get(
      `/governance/concept-confirmations?source=${SOURCE}`,
      { headers: GOV_HEADERS })).json()
    const items = queue.groups.flatMap((group: { columns: Array<Record<string, string>> }) =>
      group.columns.map(col => ({
        object_ref: col.object_ref, action: 'confirm_existing',
        evidence_id: col.evidence_id,
        expected_latest_decision_id: col.latest_decision_id,
        expected_evidence_set_hash: col.evidence_set_hash,
        expected_policy_version: col.policy_version,
      })))
    expect(items.length).toBeGreaterThan(0)
    const confirmed = await (await request.post('/governance/concept-confirmations', {
      headers: GOV_HEADERS,
      data: { source: SOURCE, reason: 'e2e journey', items },
    })).json()
    expect(confirmed.accepted_count).toBe(items.length)

    const summary = await (await request.get('/recipes/complaint_count/reviews')).json()
    const liveHash = summary.recipe_revision_hash
    const reviewers = [
      ['banking_sme', 'e2e-platform-admin'],
      ['data_semantic_owner', 'e2e-second-reviewer'],
      ['formula_engineering', 'e2e-platform-admin'],
    ] as const
    for (const [role, user] of reviewers) {
      const res = await request.post('/recipes/complaint_count/reviews', {
        headers: { 'X-User': user, 'X-Roles': 'platform_admin' },
        data: { decision: 'approved', reviewer_role: role,
                reviewed_revision_hash: liveHash,
                rationale: 'e2e journey — definition matches the banking meaning' },
      })
      expect(res.status(), await res.text()).toBe(201)
    }

    // ── 4. Regenerate as the human does: the same objective again, the recognition
    // panel returns, and this time the UOA proposal gets its one-click YES. ──────────────
    await page.getByRole('button', { name: /generate candidate sets/i }).click()
    await expect(page.getByText(/You're predicting per/)).toBeVisible()
    await page.getByRole('button', { name: 'Yes', exact: true }).click()
    await page.getByRole('button', { name: /confirm scope and generate/i }).click()

    // ── 5. The cleared card is selectable — take it with the KEYBOARD, open its decision
    // record, then govern. Focus stays visible and the drawer announces its state. ───────
    const cleared = page.getByRole('checkbox', { name: /Select Complaints/i }).first()
    await expect(cleared).toBeEnabled({ timeout: 15_000 })
    await cleared.focus()
    await expect(cleared).toBeFocused()
    await page.keyboard.press('Space')
    await expect(cleared).toBeChecked()

    const drawerButton = page.getByRole('button', { name: 'Decision record' }).first()
    await drawerButton.click()
    await expect(drawerButton).toHaveAttribute('aria-expanded', 'true')
    await expect(page.getByText('Bound roles (frozen at serving)').first()).toBeVisible()
    await expect(page.getByText(/request [0-9a-f]{16}/).first()).toBeVisible()

    await page.getByRole('button', { name: /^Govern 1$/ }).click()
    await page.getByRole('button', { name: 'Confirm govern' }).click()
    await expect(page.getByText(/Governed\s/).first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('DESIGN-CHECKED').first()).toBeVisible()

    // ── 6. The document never scrolls sideways at any point of the journey. ──────────────
    const metrics = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }))
    expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth)
  })
})
