import type { AssetIdentity, EffectiveMetadataField } from '../api'

// Shared field/authority rendering for the asset dossier. Extracted so the Overview tab can live in
// its own module without importing from AssetDetailScreen (which imports the Overview back — a
// cycle). Every helper here answers "who attested this value?", never "does a value exist?".

// The four named authorities map from provenance; tone comes from the C1 authority level. A field
// with a non-empty value but authority "missing" still reads as unattested — the badge is a fact
// about who attested the value, not about whether a value exists.
const PROVENANCE_LABEL: Record<string, string> = {
  source_declared: 'source declared',
  system_derived: 'system derived',
  llm_proposed: 'llm proposed',
  human_staged: 'human staged',
}

// Returns a LABEL only for a value that genuinely is one. `provenance` carries
// `decision_event_id or fact_event_id` (asset_detail.py) — an opaque audit id such as
// `fde_01KYM…` (fde = field decision event). Anything unrecognised is treated as an id, not a
// label: null, so the caller falls through to the real author.
function provenanceLabel(provenance: string | null): string | null {
  if (!provenance) return null
  return PROVENANCE_LABEL[provenance] ?? null
}

// The badge shows the value's author: the governed decision provenance if any, else the
// evidence-layer author (source attested / AI proposed / rulebook proposed), else "unattested"
// only when truly nothing.
export function attestedByLabel(field: EffectiveMetadataField): string {
  return provenanceLabel(field.provenance) ?? field.evidence_provenance ?? 'unattested'
}

// The decision id is NOT on the badge — not as the label, not as a tooltip. An opaque
// `fde_01KYM…` on hover is still an opaque id on screen. It lives in the Detail disclosure.
export function attributionTitle(field: EffectiveMetadataField): string {
  return `authority: ${field.authority} · c1: ${field.c1_status}`
}

// governed = a verified, load-bearing attestation (solid ok); hint = a proposal not yet governed
// (accent); missing = nothing attested (quiet). Unknown authorities stay quiet, never break.
const AUTHORITY_TONE: Record<string, string> = {
  governed: 'gj-verified',
  hint: 'gj-proposed',
  missing: 'gj-none',
}

export function authorityTone(authority: string): string {
  return AUTHORITY_TONE[authority] ?? 'gj-none'
}

// A field with no display value but a live proposal RENDERS the proposal (standing product
// direction: AI-proposed is usable, never framed as failure). The badge then says who proposed it
// and that nobody has confirmed it — "AI proposed · unconfirmed" — so the state is legible without
// reading as an error.
export function showsProposal(field: EffectiveMetadataField): boolean {
  return field.value == null && field.proposed_value != null
}

export function fieldValueText(field: EffectiveMetadataField): string {
  return field.value ?? field.proposed_value ?? '— not set'
}

// ---- type display policy (Task 3C) ----------------------------------------------------------
// The one word "unknown" appears ONLY when nothing at all is held. An operational (technical)
// type wins; else the source-DECLARED SQL type displays with its basis named; an attested type
// (Task 7) upgrades the basis automatically because it lands in operational_type.
export function typeDisplay(identity: AssetIdentity): { value: string; basis: string | null } {
  const op = identity.operational_type
  if (op && op.trim() !== '' && op.trim().toLowerCase() !== 'unknown') {
    return { value: op, basis: 'operational' }
  }
  if (identity.declared_type) return { value: identity.declared_type, basis: 'declared' }
  return { value: 'unknown', basis: null }
}

export function humanizeField(name: string): string {
  return name.replaceAll('_', ' ')
}
