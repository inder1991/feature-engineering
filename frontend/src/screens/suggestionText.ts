// Text helpers shared by the suggestion CARD and the suggestion SCREEN. Deliberately a module of
// its own rather than exports on `SuggestionCard.tsx`: a file that exports both components and
// plain functions breaks React Fast Refresh, and these two are needed on both sides of that split.

// The column name — the ref's last segment. A full schema.table.column ref is unreadable inline,
// and the full ref stays available in the drawer and in the element's title.
export function columnOf(ref: string): string {
  return ref.split('.').pop() ?? ref
}

// Tooltips and accessible names are BOUNDED. Catalog prose can be arbitrarily long, and a 4,000
// character `title` or `aria-label` is unusable in a screen reader and unreadable as a tooltip.
// The complete value is never lost: it renders as text in the detail drawer.
export const TITLE_LIMIT = 140
export function bounded(value: string, limit = TITLE_LIMIT): string {
  return value.length <= limit ? value : `${value.slice(0, limit - 1)}…`
}
