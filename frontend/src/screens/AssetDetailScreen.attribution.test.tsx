import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { AssetDetailScreen } from './AssetDetailScreen'
import { fixture } from './AssetDetailScreen.fixture'

// The attribution badge must name WHO attested a value — never an internal event id.
//
// `asset_detail.py` sets a field's `provenance` to `decision_event_id or fact_event_id`: an opaque
// audit id like `fde_01KYMVECPH7ER0VPC0A9DW4HSB` (fde = field decision event). The badge's fallback
// prettified any unrecognised string by swapping underscores for spaces, and the badge CSS uppercases
// it — so the id rendered as `FDE 01KYMVECPH7ER0VPC0A9DW4HSB`, looking entirely intentional.
//
// The value is real and worth keeping for auditors; it is just not a label. The author lives in
// `evidence_provenance` ("AI proposed", "source attested", …), which the badge was skipping.

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getAssetDetail: vi.fn(), postFieldDecision: vi.fn() }
})
const getAssetDetail = vi.mocked(api.getAssetDetail)

const DECISION_ID = 'fde_01KYMVECPH7ER0VPC0A9DW4HSB'

function field(over: Partial<api.EffectiveMetadataField> = {}): api.EffectiveMetadataField {
  return {
    value: 'bureau_inquiry', authority: 'hint', c1_status: 'no_value',
    provenance: null, evidence_provenance: 'AI proposed', selected_evidence_ids: [], ...over,
  }
}

/** The shared column fixture with ONE extra field whose attribution this file is about. */
function detail(concept: api.EffectiveMetadataField): api.AssetDetail {
  const base = fixture()
  return {
    ...base,
    effective_metadata: { fields: { ...(base.effective_metadata?.fields ?? {}), concept } },
  }
}

beforeEach(() => {
  getAssetDetail.mockReset()
})

/** Mount, open the metadata tab, and return JUST the `concept` field's card — the fixture carries
 *  sibling fields whose own badges use the same words. */
async function conceptCard(concept: api.EffectiveMetadataField): Promise<HTMLElement> {
  getAssetDetail.mockResolvedValue({ detail: detail(concept), etag: 'etag-1' })
  render(<AssetDetailScreen source="deposits" objectRef="public.accounts.balance" />)
  // The attested-metadata panel lives behind its own tab.
  await userEvent.click(await screen.findByRole('button', { name: 'Metadata & evidence' }))
  const value = await screen.findByText('bureau_inquiry')
  return value.closest('li.adg-field-card') as HTMLElement
}

/** The badge is the card's first `.badge`; the prose below it repeats the same label. */
function badgeOf(card: HTMLElement): HTMLElement {
  return card.querySelector('.badge') as HTMLElement
}

// ── the defect ───────────────────────────────────────────────────────────────────────────────────

it('never renders a decision event id as the attribution label', async () => {
  await conceptCard(field({ provenance: DECISION_ID }))
  expect(screen.queryByText(/fde[ _]01KYMVECPH7ER0VPC0A9DW4HSB/i)).toBeNull()
})

it('names the author instead, when a decision id is present', async () => {
  const card = await conceptCard(field({ provenance: DECISION_ID, evidence_provenance: 'AI proposed' }))
  expect(badgeOf(card)).toHaveTextContent('AI proposed')
})

it('says unattested rather than inventing a label from an unrecognised value', async () => {
  const card = await conceptCard(field({ provenance: DECISION_ID, evidence_provenance: null }))
  expect(badgeOf(card)).toHaveTextContent('unattested')
})

// ── a real provenance label still wins ───────────────────────────────────────────────────────────

it('renders a KNOWN provenance as its human label', async () => {
  const card = await conceptCard(field({ provenance: 'human_staged', evidence_provenance: 'AI proposed' }))
  expect(badgeOf(card)).toHaveTextContent('human staged')
})

it('falls back to the evidence author when there is no governed decision', async () => {
  const card = await conceptCard(field({ provenance: null, evidence_provenance: 'source attested' }))
  expect(badgeOf(card)).toHaveTextContent('source attested')
})

// ── the id is kept, where an auditor can use it ──────────────────────────────────────────────────

it('keeps the decision id OFF the badge entirely — label and tooltip', async () => {
  // An opaque `fde_01KYM…` revealed on hover is still an opaque id on screen. It belongs in the
  // Detail disclosure and the payload, not on a badge whose job is to name the author.
  const card = await conceptCard(field({ provenance: DECISION_ID }))
  const badge = badgeOf(card)
  expect(badge.textContent ?? '').not.toContain('KYMVECPH')
  expect(badge.getAttribute('title') ?? '').not.toContain('KYMVECPH')
})
