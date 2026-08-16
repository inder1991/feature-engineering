import type { SearchHit } from '../api'

/**
 * The name a result row leads with.
 *
 * NEVER a prettified identifier: `business_dt` stays `business_dt`. The search projection carries
 * no attested business name (`graph_node` has no business_term column), and inventing one —
 * "Business Date" — would put a machine-written label exactly where this platform's authority
 * model expects an attested value. When a business term is projected onto the search index (see
 * the deferred charter in the plan's §0.4), this is the ONE function that changes.
 */
export function hitDisplayName(hit: SearchHit): string {
  return hit.column ?? hit.table
}

/**
 * source › schema-qualified table › column — the physical address, demoted under the name but
 * never hidden: it is what a reader pastes into a query.
 */
export function hitBreadcrumb(hit: SearchHit): string[] {
  if (hit.column) {
    const cut = hit.object_ref.lastIndexOf('.')
    return [hit.catalog_source, cut > 0 ? hit.object_ref.slice(0, cut) : hit.table, hit.column]
  }
  return [hit.catalog_source, hit.object_ref]
}

export interface HitCapability {
  key: 'grain' | 'as_of' | 'measure'
  label: string
}

const ADDITIVITY_PROSE: Record<string, string> = {
  additive: 'Additive',
  semi_additive: 'Semi-additive',
  non_additive: 'Non-additive',
}

// An unknown additivity value is shown as it arrived, only made readable: underscores to spaces,
// first letter capitalized. Never dropped — a value we do not recognize is still a value the
// catalog asserted.
function additivityLabel(value: string): string {
  const known = ADDITIVITY_PROSE[value]
  if (known) return known
  const spaced = value.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

/**
 * What this asset can DO, derived only from fields the search hit carries.
 *
 * Deliberately absent: "N mappings need review", "temporal role needs review", "snapshot cutoff".
 * The /search response carries no review state, so those lines would be invented.
 */
export function hitCapabilities(hit: SearchHit): HitCapability[] {
  const capabilities: HitCapability[] = []
  if (hit.is_grain) {
    capabilities.push({
      key: 'grain',
      label: hit.entity ? `Grain key for ${hit.entity}` : 'Grain key',
    })
  }
  if (hit.is_as_of) capabilities.push({ key: 'as_of', label: 'As-of field' })
  if (hit.additivity) {
    const qualifier = hit.currency ?? hit.unit
    const label = additivityLabel(hit.additivity)
    capabilities.push({ key: 'measure', label: qualifier ? `${label} · ${qualifier}` : label })
  }
  return capabilities
}

/**
 * The quiet meta line: what the row still owes the reader after name, definition and capability.
 *
 * `concept` rides WITHOUT an authority claim. It is advisory enrichment (migration 0951), but the
 * hit carries no authority marker, so search can prove neither "AI proposed" nor "confirmed".
 */
export function hitMeta(hit: SearchHit): string[] {
  return [
    hit.kind === 'table' ? 'table' : hit.data_type,
    hit.domain,
    hit.concept ? `concept: ${hit.concept}` : null,
  ].filter((part): part is string => Boolean(part))
}
