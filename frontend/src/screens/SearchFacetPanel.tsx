import { useState } from 'react'
import { SEARCH_FACET_KEYS, type FacetBucket, type SearchFacetKey, type SearchFilters } from '../api'
import { facetLabel } from './searchFacetLabels'

// FACET_ORDER stays here, unlike the labels: ORDERING the groups is something only this panel
// does, while NAMING a facet is shared with the screen's chips. A constant with one consumer
// belongs next to that consumer.
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
        const selected = isFacetKey(key) ? (filters[key] ?? []) : []
        // The server orders by count desc; the NULL bucket is pulled out of that order entirely.
        const named = buckets.filter(bucket => bucket.value !== NONE)
        const none = buckets.find(bucket => bucket.value === NONE)
        // A SELECTED value is hoisted to the front. The counts are exclude-own-facet, so choosing a
        // value does not float it up the server's count-desc order: without this, deep-linking the
        // ninth value of a group renders six unchecked boxes and the group reads as unfiltered
        // while that filter is applied — the group misstating its own state. Both partitions keep
        // the server's relative order.
        const chosen = named.filter(bucket => selected.includes(bucket.value))
        const ranked = [...chosen, ...named.filter(bucket => !selected.includes(bucket.value))]
        const isOpen = expanded[key] ?? false
        // The window is six, or wider if more than six values are selected — a checked box must
        // never be the thing hiding behind the disclosure.
        const shown = isOpen ? ranked : ranked.slice(0, Math.max(COLLAPSED, chosen.length))
        const hidden = ranked.length - shown.length
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
            {/* A two-way toggle, not a one-shot reveal. A button that unmounts itself on
                activation drops keyboard focus to <body>, throwing the user to the top of the
                document with no way back; staying mounted keeps focus where it was and lets
                aria-expanded announce the state. Same disclosure shape as the row's `···`
                overflow (292237ac). */}
            {(isOpen || hidden > 0) && (
              <button
                type="button"
                className="facet-more"
                aria-expanded={isOpen}
                onClick={() => setExpanded(state => ({ ...state, [key]: !isOpen }))}
              >
                {/* The count names the whole group, NULL bucket included, because that is what
                    expanding reveals. */}
                {isOpen ? 'Show fewer' : `Show all ${ranked.length + (none ? 1 : 0)}`}
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
