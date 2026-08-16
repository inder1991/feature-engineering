/**
 * The display vocabulary for catalog-search facet keys.
 *
 * A module rather than an export off `SearchFacetPanel.tsx`, following `searchHitDisplay.ts` from
 * Task 1: two components name these facets — the sidebar's group legends and the screen's
 * active-filter chips — and one of them must not have to import the other to say the same word.
 * Ruling R2 asked for one OWNER of this vocabulary, and a module is a stricter owner than a
 * component that happens to also export a function (which is what the lone
 * `react(only-export-components)` warning in this repo was pointing at).
 */

// The label a facet key wears in the sidebar. A key that is not here still renders — humanized —
// because the server is allowed to grow a facet without the UI shipping first.
const FACET_LABELS: Record<string, string> = {
  source: 'Source',
  kind: 'Kind',
  domain: 'Domain',
  sub_domain: 'Sub-domain',
  entity: 'Entity',
  // The backend's `data_role` is the TABLE role projection (crosswalk, reference, event…). The
  // column axis — grain / as-of — is the flags group, titled "Column role". Two different
  // questions must not share one word.
  data_role: 'Table role',
  authority_role: 'Authority role',
  temporal_storage_model: 'Temporal storage',
  additivity: 'Additivity',
  // The projected display axis a user means by "sensitivity", and the raw tag a source file
  // declared, which is empty on catalogs that declare none. Named apart on purpose.
  sensitivity_display: 'Sensitivity',
  sensitivity: 'Declared tag',
  bian_path: 'BIAN path',
  process_path: 'Business process',
}

/**
 * The sidebar label for a facet key. The one owner of this vocabulary: without it a chip reads
 * "sensitivity display: restricted" while the group above it reads "Sensitivity".
 */
export function facetLabel(key: string): string {
  const known = FACET_LABELS[key]
  if (known) return known
  const spaced = key.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}
