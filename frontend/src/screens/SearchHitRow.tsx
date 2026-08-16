import { Fragment, type ReactNode, useEffect, useRef, useState } from 'react'
import { ApiError, type SearchHit, featureImpact } from '../api'
import { hitBreadcrumb, hitCapabilities, hitDisplayName, hitMeta } from './searchHitDisplay'

/**
 * One search result.
 *
 * Anatomy, top to bottom: the name the reader is looking for, the physical address that name
 * resolves to, the definition (the business meaning the catalog actually holds), what the asset
 * can do, and the quiet remainder. Actions carry ONE primary — four equal buttons, which is what
 * this row used to be, is no hierarchy at all.
 */
export function SearchHitRow({
  hit,
  onOpen,
  onExplore,
  onSuggested,
}: {
  hit: SearchHit
  onOpen: (hit: SearchHit) => void
  onExplore: (hit: SearchHit) => void
  onSuggested: (hit: SearchHit) => void
}) {
  const [impact, setImpact] = useState<string[] | null>(null)
  const [impactError, setImpactError] = useState('')
  const [checking, setChecking] = useState(false)
  const [copyStatus, setCopyStatus] = useState('')

  async function copyReference() {
    try {
      await navigator.clipboard.writeText(hit.object_ref)
      setCopyStatus('Reference copied')
    } catch {
      // A browser may refuse clipboard access outright. Saying "copied" would be a lie, and
      // saying only "failed" leaves the reader with nothing, so hand them the reference itself.
      setCopyStatus(`Could not copy. The reference is ${hit.object_ref}`)
    }
  }

  async function checkImpact() {
    setChecking(true)
    setImpactError('')
    try {
      setImpact(await featureImpact(hit.object_ref, hit.catalog_source))
    } catch (err) {
      setImpact(null)
      setImpactError(err instanceof ApiError ? err.detail : String(err))
    } finally {
      setChecking(false)
    }
  }

  const breadcrumb = hitBreadcrumb(hit)
  const capabilities = hitCapabilities(hit)
  const meta = hitMeta(hit)

  return (
    <li className="row hit">
      <div className="hit-main">
        <div className="hit-title">
          <span className="hit-name mono" data-testid="hit-name">{hitDisplayName(hit)}</span>
          {hit.kind === 'table' && <span className="badge kindtable">table</span>}
          {hit.is_grain && <span className="badge grain">grain</span>}
          {hit.is_as_of && <span className="badge asof">as-of</span>}
          {hit.sensitivity && <span className="badge sensitivity">{hit.sensitivity}</span>}
          {/* The projected display label, its OWN badge — never merged with the tag above: the two
              speak different vocabularies ('pii' vs 'restricted'), and on a catalog that declares
              no tag this is the only sensitivity a column has. */}
          {hit.sensitivity_display && (
            <span className="badge sensitivity">{hit.sensitivity_display}</span>
          )}
        </div>

        {/* Keyed by position, not by value: the trail is positional and never reordered, and a
            catalog source that shares its name with the table it feeds (source `accounts`,
            table `accounts`) would otherwise collide on a value key.

            The separator's SPACES live outside the aria-hidden span, deliberately. aria-hidden
            removes the element AND its subtree from the accessibility tree, so spacing held
            inside the glyph span is not merely silent — it is gone, and a screen reader reads
            the three parts as one run-on token ("depositspublic.accountsbalance"). */}
        <p className="hit-breadcrumb mono" data-testid="hit-breadcrumb">
          {breadcrumb.map((part, i) => (
            <Fragment key={i}>
              {i > 0 && (
                <>
                  {' '}
                  <span aria-hidden="true">›</span>{' '}
                </>
              )}
              {part}
            </Fragment>
          ))}
        </p>

        {hit.definition && (
          <p className="hit-definition" data-testid="hit-definition">{hit.definition}</p>
        )}

        {meta.length > 0 && <p className="hint hit-meta">{meta.join(' · ')}</p>}

        {copyStatus && <p className="hint" role="status">{copyStatus}</p>}

        {checking && <p className="hint">Checking feature impact…</p>}
        {impactError && (
          <p role="alert" className="error">Impact check failed: {impactError}</p>
        )}
        {impact?.length === 0 && (
          <p className="hint" role="status">No features derive from this column.</p>
        )}
        {impact && impact.length > 0 && (
          <div className="hit-impact">
            <p className="micro-label">Derived features</p>
            <ul className="mono">
              {impact.map(id => <li key={id}>{id}</li>)}
            </ul>
          </div>
        )}
      </div>

      {capabilities.length > 0 && (
        <ul className="hit-capabilities">
          {capabilities.map(capability => (
            <li key={capability.key}>{capability.label}</li>
          ))}
        </ul>
      )}

      <div className="hit-actions">
        <button
          type="button"
          className="btn btn--primary"
          aria-label={`Open asset ${hit.object_ref}`}
          onClick={() => onOpen(hit)}
        >
          Open asset
        </button>
        <button
          type="button"
          className="btn"
          aria-label={`Explore relationships for ${hit.object_ref}`}
          onClick={() => onExplore(hit)}
        >
          Explore relationships
        </button>
        <RowOverflow label={`More actions for ${hit.object_ref}`}>
          <button
            type="button"
            aria-label={`Suggested features for ${hit.table}`}
            onClick={() => onSuggested(hit)}
          >
            Suggested features
          </button>
          <button
            type="button"
            aria-label={`Feature impact for ${hit.object_ref}`}
            disabled={checking}
            onClick={() => void checkImpact()}
          >
            Feature impact
          </button>
          <button
            type="button"
            aria-label={`Copy reference for ${hit.object_ref}`}
            onClick={() => void copyReference()}
          >
            Copy reference
          </button>
        </RowOverflow>
      </div>
    </li>
  )
}

/**
 * The row's overflow. A DISCLOSURE, not an ARIA menu: `role="menu"` promises arrow-key roving
 * that a three-item popover does not need and we would not implement honestly. What it does
 * promise it keeps — Escape closes and returns focus, a pointer elsewhere closes, and choosing an
 * item closes.
 */
function RowOverflow({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    function onPointerDown(event: PointerEvent) {
      if (!wrap.current?.contains(event.target as Node)) setOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      setOpen(false)
      trigger.current?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div className="hit-overflow" ref={wrap}>
      <button
        type="button"
        ref={trigger}
        className="btn btn--ghost hit-overflow-trigger"
        aria-label={label}
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
      >
        ···
      </button>
      {open && (
        // Choosing anything closes the popover: the click bubbles to this wrapper, so one handler
        // covers every item without each item having to remember to close.
        <div className="hit-overflow-items" onClick={() => setOpen(false)}>
          {children}
        </div>
      )}
    </div>
  )
}
