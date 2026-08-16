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
 * The data type the catalog actually attests, or nothing.
 *
 * `graph_node.data_type` holds the literal string "unknown" for a large share of the deployed
 * catalog: it is the ingest's canonical placeholder for a column whose type nobody has established
 * (type attestation fills it in later from the engine). It is not a fabricated value — but it is an
 * ABSENCE of a type wearing the costume of one, and a meta line reading "unknown · Customer" states
 * a type where the catalog holds none. Absence renders as absence, so it does not ride.
 *
 * Only the one sentinel that is actually in the data. An empty string is already dropped by the
 * caller's truthiness filter; no other placeholder is guessed at.
 */
function attestedDataType(dataType: string | null): string | null {
  return dataType && dataType.toLowerCase() !== 'unknown' ? dataType : null
}

/**
 * The quiet meta line: what the row still owes the reader after name, definition and capability.
 *
 * `concept` rides WITHOUT an authority claim. It is advisory enrichment (migration 0951), but the
 * hit carries no authority marker, so search can prove neither "AI proposed" nor "confirmed".
 */
export function hitMeta(hit: SearchHit): string[] {
  // The grain capability line reads "Grain key for Account", so repeating the entity here would
  // say it twice. On every OTHER row the entity has nowhere else to go — and `entity` is a facet
  // people filter by, so a row that matched `entity=Account` has to be able to say so.
  const namedByCapability = hit.is_grain && Boolean(hit.entity)
  return [
    // The kind is deliberately absent: the row badges a table as `table` already.
    attestedDataType(hit.data_type),
    hit.domain,
    hit.entity && !namedByCapability ? `entity: ${hit.entity}` : null,
    hit.concept ? `concept: ${hit.concept}` : null,
  ].filter((part): part is string => Boolean(part))
}
