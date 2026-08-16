import { Fragment, type ReactNode, useEffect, useRef, useState } from 'react'
import { ApiError, type SearchHit, featureImpact } from '../api'
import { hitBreadcrumb, hitCapabilities, hitDisplayName, hitMeta } from './searchHitDisplay'

/**
 * How long the copy acknowledgement stays on the row.
 *
 * It is an acknowledgement of an action, not a property of the asset, so it has to leave. Without
 * this every row a reader ever copied from stayed one line taller for the rest of the session, and
 * a refused copy left the full object_ref printed in the row permanently.
 *
 * A timer rather than "clear when the popover closes": choosing Copy reference IS what closes the
 * popover, so a close-driven clear would race the clipboard promise that sets the message — the
 * acknowledgement would appear or not depending on which settled first.
 *
 * Eight seconds: long enough to read the refusal sentence and select the reference out of it,
 * short enough that a row does not carry the line into the reader's next query.
 */
export const COPY_STATUS_MS = 8000

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
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // The row can be unmounted by the next query landing while the acknowledgement is still up.
  useEffect(() => () => {
    if (copyTimer.current !== null) clearTimeout(copyTimer.current)
  }, [])

  // Every announcement replaces the one before it AND its timer: copying twice in a row must give
  // the second message a full window, not whatever was left of the first one's.
  function announceCopy(message: string) {
    if (copyTimer.current !== null) clearTimeout(copyTimer.current)
    setCopyStatus(message)
    copyTimer.current = setTimeout(() => setCopyStatus(''), COPY_STATUS_MS)
  }

  async function copyReference() {
    try {
      await navigator.clipboard.writeText(hit.object_ref)
      announceCopy('Reference copied')
    } catch {
      // A browser may refuse clipboard access outright. Saying "copied" would be a lie, and
      // saying only "failed" leaves the reader with nothing, so hand them the reference itself.
      announceCopy(`Could not copy. The reference is ${hit.object_ref}`)
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
        {/* The noun follows the HIT, not the wording that suited the commonest row. Feature impact
            is offered on every row and the unfiltered browse leads with tables, so a fixed "column"
            told the reader a table hit was a column the catalog does not hold — an asserted fact
            with nothing behind it, on a screen whose first rule is that it never has one. */}
        {impact?.length === 0 && (
          <p className="hint" role="status">
            {`No features derive from this ${hit.column ? 'column' : 'table'}.`}
          </p>
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
 * promise it keeps — Escape closes and returns focus, a pointer elsewhere closes, focus leaving
 * the popover closes, and choosing an item closes AND returns focus.
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
    <div
      className="hit-overflow"
      ref={wrap}
      // The third exit: focus leaving the popover. Escape and an outside pointerdown miss every
      // path that moves focus without a key press here or a click there — the search screen's "/"
      // shortcut focuses the query field from anywhere on the page, which left this popover open
      // and floating over a row the reader had already left.
      //
      // React's onBlur IS `focusout`, so it reaches this wrapper from any descendant, and
      // `relatedTarget` is where focus is GOING: a hop between the popover's own items (or back to
      // the trigger) stays inside `wrap` and must not close it. A null relatedTarget is focus
      // leaving the DOCUMENT — the window losing focus — which is not the reader deciding they are
      // done with the popover, so it stays open for their return. Focus is NOT sent back to the
      // trigger here, unlike Escape: it has deliberately gone somewhere else.
      onBlur={event => {
        const next: Node | null = event.relatedTarget
        if (next && !wrap.current?.contains(next)) setOpen(false)
      }}
    >
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
        //
        // Focus goes back to the trigger for the same reason Escape sends it back. The chosen
        // button is about to unmount, and a keyboard user who pressed Enter on it would otherwise
        // be dropped on <body> — losing their place in a long result list at the exact moment the
        // row has something new to say, since Feature impact and Copy reference both render their
        // result INTO this row. Harmless for Suggested features, which navigates away.
        <div
          className="hit-overflow-items"
          onClick={() => {
            setOpen(false)
            trigger.current?.focus()
          }}
        >
          {children}
        </div>
      )}
    </div>
  )
}
