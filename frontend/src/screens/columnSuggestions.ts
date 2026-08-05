import { useEffect, useMemo, useState } from 'react'
import {
  ApiError,
  type AssetIdentity,
  type FeatureSuggestionPageV2,
  SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION,
  getTableSuggestionsV2,
} from '../api'
import { useIdentityKey } from '../session'

// ---- suggestions read ------------------------------------------------------------------------
// Exactly one outcome is true at a time, and each is stored WITH the read scope it belongs to.
// Mirrors the suggestions screen's own union — the two surfaces read the same route under the same
// scope rules, so they must not disagree about what "no answer yet" looks like.
export type SuggestionsOutcome =
  | { kind: 'loading' }
  | { kind: 'ok'; page: FeatureSuggestionPageV2 }
  | { kind: 'forbidden' }
  | { kind: 'unsupported' }
  | { kind: 'error'; detail: string }

// The scope-key separator: a NUL, the one character that cannot occur in a principal, a catalog
// source or a table ref. Built with fromCharCode rather than typed inline — a raw NUL byte in the
// source makes git treat the file as binary (diffs vanish) and hides the line from every plain
// grep, which is exactly how this line was written wrong the first time.
const SCOPE_SEP = String.fromCharCode(0)

// Lifted out of the recommendations section so the summary strip and the card read ONE fetch.
// Two components each calling getTableSuggestionsV2 would double the request and could disagree
// with each other on screen while one of them was still in flight.
export function useColumnSuggestions(source: string, identity: AssetIdentity | null) {
  // A table asset has `table` set too, but suggestions bind COLUMN operands, so the match set is
  // empty by construction. Gate on the anchor being a column: otherwise the table page pays for a
  // request it cannot use and reports "0 · use this column" on a page that has no column.
  // A null identity is the pre-load state — same empty `table`, so the effect below reads nothing.
  const table = identity?.kind === 'column' ? (identity.table ?? '') : ''
  const objectRef = identity?.object_ref ?? ''
  // Read scope decides which suggestions exist at all, so a result read under other claims is not
  // an answer here. Keyed on principal + claims, never on the URL alone.
  const identityKey = useIdentityKey()
  // Joined on SCOPE_SEP: no pair of different scopes can collide into the same key.
  const requestKey = [identityKey, source, table].join(SCOPE_SEP)
  // Stored WITH the key it was read under, and trusted below only while the two still match. An
  // effect cannot give that guarantee — it runs AFTER the render the session-store update triggers,
  // so clearing there would paint the previous scope's cards once first.
  const [result, setResult] = useState<{ key: string; outcome: SuggestionsOutcome }>(
    { key: requestKey, outcome: { kind: 'loading' } })
  const outcome: SuggestionsOutcome =
    result.key === requestKey ? result.outcome : { kind: 'loading' }

  useEffect(() => {
    if (!table) return
    let live = true
    const settle = (o: SuggestionsOutcome) => {
      if (live) setResult({ key: requestKey, outcome: o })
    }
    settle({ kind: 'loading' })
    getTableSuggestionsV2(source, table)
      .then(body => settle({ kind: 'ok', page: body }))
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 403) settle({ kind: 'forbidden' })
        else if (e instanceof ApiError
          && e.errorCode === SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION) settle({ kind: 'unsupported' })
        else settle({ kind: 'error', detail: e instanceof ApiError ? e.detail : String(e) })
      })
    return () => {
      live = false
    }
  }, [source, table, requestKey])

  const page = outcome.kind === 'ok' ? outcome.page : null
  const matching = useMemo(() => {
    if (!page) return []
    const ref = objectRef.toLowerCase()
    return page.hits.filter(hit =>
      hit.suggestion.operands.some(o => o.graph_object_ref.toLowerCase() === ref),
    )
  }, [page, objectRef])

  return { outcome, page, matching, table }
}
