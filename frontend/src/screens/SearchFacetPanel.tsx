import { useState } from 'react'
import { SEARCH_FACET_KEYS, type FacetBucket, type SearchFacetKey, type SearchFilters } from '../api'

// The label a facet key wears in the sidebar. A key that is not here still renders — humanized —
// because the server is allowed to grow a facet without the UI shipping first.
const FACET_LABELS: Record<string, string> = {
  source: 'Source',
  kind: 'Kind',
  domain: 'Domain',
  sub_domain: 'Sub-domain',
  entity: 'Entity',
  // The backend's `data_role` is the TABLE role projection (crosswalk, reference, event…). The
  // column axis — grain / as-of — is the flags group below, titled "Column role". Two different
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

// Known keys lead, in reading order; anything the server adds later follows, alphabetically.
const FACET_ORDER: string[] = [
  'source', 'kind', 'domain', 'sub_domain', 'entity', 'data_role', 'authority_role',
  'temporal_storage_model', 'additivity', 'sensitivity_display', 'sensitivity', 'bian_path',
  'process_path',
]

const NONE = '(none)'          // the server's NULL bucket, and the token that selects IS NULL
const COLLAPSED = 6            // values shown before "Show all"

const FLAG_OPTIONS: { key: 'grain' | 'as_of'; label: string }[] = [
  { key: 'grain', label: 'Grain key' },
  { key: 'as_of', label: 'As-of field' },
]

/**
 * The sidebar label for a facet key. EXPORTED because the screen's active-filter chips name the
 * same facets: without one owner of this vocabulary a chip reads "sensitivity display: restricted"
 * while the group above it reads "Sensitivity".
 */
export function facetLabel(key: string): string {
  const known = FACET_LABELS[key]
  if (known) return known
  const spaced = key.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

function isFacetKey(key: string): key is SearchFacetKey {
  return (SEARCH_FACET_KEYS as readonly string[]).includes(key)
}

/**
 * The filter sidebar, driven by the facet map the SERVER returned rather than a hardcoded list.
 *
 * Two rules make it readable on a real catalog. First, the NULL bucket is a value, not a
 * headline: on a 150K-column catalog "(none)" is usually the largest count in the group, and
 * letting it lead makes every group look like it holds nothing. It is pinned after the named
 * values, labelled "Not classified", and never consumes one of the six collapsed slots — but it
 * stays selectable without expanding, because "show me the unclassified columns" is a real
 * question. Second, groups collapse to six named values, because a fifty-value group is a wall,
 * not a filter.
 */
export function SearchFacetPanel({
  facets,
  filters,
  onToggleFacet,
  onToggleFlag,
}: {
  facets: Record<string, FacetBucket[]>
  filters: SearchFilters
  onToggleFacet: (key: SearchFacetKey, value: string) => void
  onToggleFlag: (key: 'grain' | 'as_of') => void
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const valueKeys = Object.keys(facets)
    .filter(key => key !== 'grain' && key !== 'as_of' && (facets[key]?.length ?? 0) > 0)
    .sort((a, b) => {
      const ai = FACET_ORDER.indexOf(a)
      const bi = FACET_ORDER.indexOf(b)
      if (ai !== -1 && bi !== -1) return ai - bi
      if (ai !== -1) return -1
      if (bi !== -1) return 1
      return a.localeCompare(b)
    })

  const flagBuckets = { grain: facets.grain?.[0], as_of: facets.as_of?.[0] }
  const showFlags = Boolean(flagBuckets.grain || flagBuckets.as_of)
  if (valueKeys.length === 0 && !showFlags) return null

  return (
    <aside className="facet-panel" aria-label="Filters">
      {valueKeys.map(key => {
        const buckets = facets[key] ?? []
        // The server orders by count desc; the NULL bucket is pulled out of that order entirely.
        const named = buckets.filter(bucket => bucket.value !== NONE)
        const none = buckets.find(bucket => bucket.value === NONE)
        const isOpen = expanded[key] ?? false
        const shown = isOpen ? named : named.slice(0, COLLAPSED)
        const hidden = named.length - shown.length
        const selected = isFacetKey(key) ? (filters[key] ?? []) : []
        return (
          <fieldset className="facet-group" key={key}>
            <legend className="facet-group-title">{facetLabel(key)}</legend>
            {[...shown, ...(none ? [none] : [])].map(bucket => {
              // The declared pii tag keeps its danger dot. It is decoration on top of the label,
              // which is what actually carries the meaning — hence aria-hidden.
              const isPii = key === 'sensitivity' && bucket.value === 'pii'
              return (
                <label className="facet-option" key={bucket.value}>
                  <input
                    type="checkbox"
                    checked={selected.includes(bucket.value)}
                    disabled={!isFacetKey(key)}
                    onChange={() => isFacetKey(key) && onToggleFacet(key, bucket.value)}
                  />
                  {isPii && <span className="facet-pii-dot" aria-hidden="true" />}
                  <span className="facet-name">
                    {bucket.value === NONE ? 'Not classified' : bucket.value}
                  </span>{' '}
                  <span className="facet-count tabular-nums">{bucket.count}</span>
                </label>
              )
            })}
            {!isOpen && hidden > 0 && (
              <button
                type="button"
                className="facet-more"
                onClick={() => setExpanded(state => ({ ...state, [key]: true }))}
              >
                {/* The count names the whole group, NULL bucket included, because that is what
                    expanding reveals. */}
                Show all {named.length + (none ? 1 : 0)}
              </button>
            )}
          </fieldset>
        )
      })}

      {showFlags && (
        <fieldset className="facet-group">
          {/* The COLUMN axis. Named apart from the server's `data_role`, which is a table role. */}
          <legend className="facet-group-title">Column role</legend>
          {FLAG_OPTIONS.map(flag => {
            const count = flagBuckets[flag.key]?.count ?? 0
            const checked = Boolean(filters[flag.key])
            // A flag with no matching rows and not already picked cannot narrow further.
            const disabled = count === 0 && !checked
            return (
              <label
                className={disabled ? 'facet-option facet-option--disabled' : 'facet-option'}
                key={flag.key}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={disabled}
                  onChange={() => onToggleFlag(flag.key)}
                />
                <span className="facet-name">{flag.label}</span>{' '}
                <span className="facet-count tabular-nums">{count}</span>
              </label>
            )
          })}
        </fieldset>
      )}
    </aside>
  )
}
