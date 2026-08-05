// Catalog lineage graph view: an @xyflow/react canvas with @dagrejs/dagre auto-layout around
// one anchor (a search hit). The wire (GET /graph/lineage) is the single source of truth:
// read-scope-hidden nodes are ABSENT from the response, stale sources arrive flagged stale
// (shown, not hidden), and declared-but-unresolved joins and entity bridges arrive as
// resolved=false data. This component renders exactly what it is given.
//
// Expanders: the endpoint does not report degree, so every resolved table unit that has not
// been fetched around yet carries a quiet "+" chip. Clicking it fetches ONE more depth
// anchored on that table (same direction, all layers) and merges the result into the graph;
// a merge that adds nothing marks the table exhausted and says so. Feature, consumer, and
// pending-stub nodes have no chip: the endpoint anchors only on catalog tables/columns.
import dagre from '@dagrejs/dagre'
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ApiError,
  lineageGraph,
  type LineageDirection,
  type LineageEdge,
  type LineageGraph,
  type LineageLayer,
  type LineageNode,
  type SearchHit,
} from '../api'
import { useHashRoute } from '../nav'
import { useColumnSuggestions } from './columnSuggestions'

// The view traverses both ways from the anchor (the mockup has no direction control); the
// constant is threaded through every fetch so expander clicks stay direction-aware.
const DIRECTION: LineageDirection = 'both'

// ---- node geometry (dagre needs sizes before the DOM exists, so they are computed, not
// measured; the same numbers go on the node as inline style so the canvas agrees) -----------
const W_TABLE = 240
const W_FEATURE = 250
const W_CONSUMER = 230
const HEAD_H = 40
const SRC_H = 24
const ROW_H = 40 // column rows are real buttons: hit targets >= 32px (PRODUCT.md).
// 40 rather than the minimum: the artifact's rows breathe, and a name plus a right-aligned
// chip at 32px left no vertical air between them.
const PAD_H = 8
const MORE_H = 32 // the "+N more columns" row, same hit-target height as a column row
const COL_CAP = 8 // an expanded card caps its visible rows; the rest sits behind "+N more"
// Full-list ceiling: with head + src + a stale note the card stays under the 640px canvas,
// so even a 127-column table can never render as a viewport-dwarfing tower.
const LIST_MAX_H = 440

const SYMMETRIC = new Set<string>(['join', 'entity_bridge'])

function dedupeKey(e: LineageEdge): string {
  const ends = SYMMETRIC.has(e.kind) ? [e.from, e.to].sort() : [e.from, e.to]
  return `${e.kind}|${ends[0]}|${ends[1]}`
}

// Merge a lazily fetched depth-1 graph into the accumulated one. Node PAYLOADS are last-wins: a
// re-fetch may carry fresher flags (a source that went stale between fetches), so the newer node
// replaces the stored one IN PLACE (order preserved via the Map), while `grew` counts only NEW
// ids so a same-shape re-fetch of known nodes still reports no growth. Edges dedupe by kind +
// endpoints, unordered for the symmetric kinds (first orientation wins, matching the backend's
// first-BFS-discovery rule) so a re-fetch from the other side cannot duplicate a join. `grew`
// reports whether anything new arrived.
function mergeGraph(
  base: LineageGraph,
  add: LineageGraph,
): { graph: LineageGraph; grew: boolean } {
  const nodeIndex = new Map(base.nodes.map(n => [n.id, n] as const))
  const keys = new Set(base.edges.map(dedupeKey))
  const edges = [...base.edges]
  let grew = false
  for (const n of add.nodes) {
    if (!nodeIndex.has(n.id)) grew = true
    nodeIndex.set(n.id, n) // last-wins on payload; Map.set keeps the original slot for known ids
  }
  for (const e of add.edges) {
    const k = dedupeKey(e)
    if (!keys.has(k)) {
      keys.add(k)
      edges.push(e)
      grew = true
    }
  }
  return {
    graph: { nodes: [...nodeIndex.values()], edges, truncated: base.truncated || add.truncated },
    grew,
  }
}

// ---- custom node data ----------------------------------------------------------------------
// (type aliases, not interfaces: xyflow's Node<T> needs the implicit index signature)

type TableData = {
  node: LineageNode
  rows: LineageNode[] // the rows actually rendered (priority-ordered, capped or full)
  total: number // the table's true column count, for the compact card's count chip
  more: number // rows hidden by the cap; > 0 renders the "+N more columns" expander
  scroll: boolean // the full list overflows LIST_MAX_H: scroll inside the card
  countChip: boolean // compact anchor-table under a column anchor: show "N columns"
  collapsed: boolean
  matchId: string | null
  traceId: string | null
  expandable: boolean
  expanding: boolean
  onToggle: (id: string) => void
  onShowAll: (id: string) => void
  onColumn: (col: LineageNode) => void
  onOpen: (node: LineageNode) => void
  onExpand: (node: LineageNode) => void
  // Column ids that are an endpoint of a visible cross-catalog mapping.
  mapped: Set<string>
}
type AnchorColData = {
  node: LineageNode
  traceId: string | null
  onColumn: (col: LineageNode) => void
  // From the SearchHit, not the graph payload: LineageNode carries no declared type and no
  // business term, so the artifact's meta line is unbuildable from /graph/lineage alone.
  meta: string | null
}
type StubData = { node: LineageNode }
type FeatureData = { node: LineageNode; onOpen: (node: LineageNode) => void }
type ConsumerData = { node: LineageNode; reads: number; onOpen: (node: LineageNode) => void }

type TableNT = Node<TableData, 'lnTable'>
type AnchorColNT = Node<AnchorColData, 'lnAnchorCol'>
type StubNT = Node<StubData, 'lnStub'>
type FeatureNT = Node<FeatureData, 'lnFeature'>
type ConsumerNT = Node<ConsumerData, 'lnConsumer'>

// A node after dagre has placed it: geometry only (position + size + its column rows). The cheap
// `flow` memo turns these into xyflow nodes with the interaction-dependent data on top.
type PlacedNode = {
  node: LineageNode
  type: 'lnTable' | 'lnAnchorCol' | 'lnStub' | 'lnFeature' | 'lnConsumer'
  x: number
  y: number
  w: number
  h: number
  rows: LineageNode[]
  total: number
  more: number
  scroll: boolean
}

function Ports() {
  // Default (id-less) handles: edges that anchor on the unit itself, or on a collapsed
  // table's hidden columns. Purely structural, never interactive.
  return (
    <>
      <Handle type="target" position={Position.Left} className="ln-port" isConnectable={false} />
      <Handle type="source" position={Position.Right} className="ln-port" isConnectable={false} />
    </>
  )
}

function Flag({ tone, children }: { tone: string; children: string }) {
  return <span className={`ln-flag ln-flag--${tone}`}>{children}</span>
}

function TableNode({ data }: NodeProps<TableNT>) {
  const { node, rows, collapsed, matchId, traceId } = data
  // `nopan` (xyflow's own escape hatch): a drag that starts on a card must not pan the
  // canvas, exactly like the mockup's grab handler ignoring drags that start on a node.
  return (
    <div className={`ln-card nopan${node.stale ? ' ln-card--stale' : ''}`}>
      <Ports />
      {/* Two controls share the head: the title opens the details drawer (table provenance), the
          caret collapses the column list. Split (not one button) so both are reachable without
          nesting buttons — the title used to only toggle, leaving the table drawer unreachable. */}
      <div className="ln-head">
        <button
          type="button"
          className="ln-head-btn"
          onClick={() => data.onOpen(node)}
        >
          <span className="ln-kind">table</span>
          <span className="ln-name" title={node.table}>
            {node.table}
          </span>
        </button>
        {node.quarantine_pending ? (
          // Operational state at a glance: rows this table couldn't ingest, still in the review
          // queue. Label carries the count so color is never the only signal (WCAG); the drawer
          // spells it out. A solid warn chip, matching the stale flag's weight.
          <span
            className="ln-flag ln-flag--warn"
            title={`${node.quarantine_pending} ${
              node.quarantine_pending === 1 ? 'row' : 'rows'
            } in the review queue`}
          >
            {node.quarantine_pending} queued
          </span>
        ) : null}
        {data.countChip && (
          // Compact anchor-table: the anchored column is drawn as its own node, so the card
          // carries the honest total instead of a column tower.
          <span className="ln-flag ln-flag--count">
            {data.total} {data.total === 1 ? 'column' : 'columns'}
          </span>
        )}
        <button
          type="button"
          className="ln-caret-btn"
          aria-expanded={!collapsed}
          aria-label={`${collapsed ? 'Show' : 'Hide'} ${node.table} columns`}
          onClick={() => data.onToggle(node.id)}
        >
          <span className="ln-caret" aria-hidden="true">
            {collapsed ? '▸' : '▾'}
          </span>
        </button>
      </div>
      {/* One condensed meta row, as the artifact draws it: where the table lives and how wide it
          is, with freshness as a quiet dot at the end rather than a chip of its own line. */}
      <div className="ln-src">
        <span className="ln-src-text">
          {[node.catalog_source,
            data.total ? `${data.total} ${data.total === 1 ? 'column' : 'columns'}` : null]
            .filter(Boolean).join(' · ')}
        </span>
        {node.stale && <Flag tone="stale">stale</Flag>}
      </div>
      {/* No stale band: the src line above already carries a STALE chip, and a 58px amber
          restatement was the biggest element on the card and mostly empty space. Freshness is
          signalled once, quietly. */}
      {rows.length > 0 && (
        <ul className={data.scroll ? 'ln-cols ln-cols--scroll' : 'ln-cols'}>
          {rows.map(col => (
            <li key={col.id} className={col.id === matchId ? 'ln-colrow ln-colrow--match' : 'ln-colrow'}>
              <Handle
                type="target"
                position={Position.Left}
                id={col.id}
                className="ln-port"
                isConnectable={false}
              />
              <button
                type="button"
                className="ln-col"
                aria-current={col.id === matchId ? 'true' : undefined}
                aria-pressed={col.id === traceId}
                onClick={() => data.onColumn(col)}
              >
                <span className="ln-col-name">{col.column}</span>
                {/* The artifact names WHY a row is called out: the anchor row is the column the
                    map is built around, a mapped row is an endpoint of a cross-catalog link.
                    Highlighting alone left the reader to infer the reason from colour. */}
                {col.id === matchId && <Flag tone="anchor">anchor</Flag>}
                {col.id !== matchId && data.mapped.has(col.id) && <Flag tone="mapped">mapped</Flag>}
                {col.grain && <Flag tone="grain">grain</Flag>}
                {col.as_of && <Flag tone="asof">as-of</Flag>}
                {col.sensitivity && <Flag tone="pii">{col.sensitivity}</Flag>}
              </button>
              <Handle
                type="source"
                position={Position.Right}
                id={col.id}
                className="ln-port"
                isConnectable={false}
              />
            </li>
          ))}
        </ul>
      )}
      {data.more > 0 && (
        <button
          type="button"
          className="ln-more"
          onClick={() => data.onShowAll(node.id)}
        >
          +{data.more} more {data.more === 1 ? 'column' : 'columns'}
        </button>
      )}
      {data.expandable && (
        <button
          type="button"
          className="ln-expander"
          aria-label={`Expand neighbors of ${node.table}`}
          disabled={data.expanding}
          onClick={() => data.onExpand(node)}
        >
          {data.expanding ? '…' : '+'}
        </button>
      )}
    </div>
  )
}

function AnchorColNode({ data }: NodeProps<AnchorColNT>) {
  // The searched column as its own node at the map's center. The button mirrors a column
  // row's contract (match highlight, trace toggle, drawer) so flags stay outside it and its
  // accessible name is the bare column name.
  const { node } = data
  return (
    <div className="ln-card ln-card--anchor nopan">
      <Ports />
      <div className="ln-head">
        <span className="ln-kind">column</span>
        <button
          type="button"
          className="ln-head-btn"
          aria-current="true"
          aria-pressed={node.id === data.traceId}
          onClick={() => data.onColumn(node)}
        >
          <span className="ln-name" title={node.column}>
            {node.column}
          </span>
        </button>
        {node.grain && <Flag tone="grain">grain</Flag>}
        {node.as_of && <Flag tone="asof">as-of</Flag>}
        {node.sensitivity && <Flag tone="pii">{node.sensitivity}</Flag>}
      </div>
      {(data.meta ?? node.concept) && (
        <div className="ln-src">
          <span className="ln-src-text">{data.meta ?? node.concept}</span>
        </div>
      )}
    </div>
  )
}

function StubNode({ data }: NodeProps<StubNT>) {
  // A declared join target that is not uploaded yet: dashed, labeled, non-interactive.
  // It is data, not an error.
  return (
    <div className="ln-card ln-card--pending nopan">
      <Ports />
      <div className="ln-head ln-head--static">
        <span className="ln-kind">declared</span>
        <span className="ln-name" title={data.node.object_ref}>
          {data.node.object_ref}
        </span>
      </div>
      <div className="ln-body">
        Declared join target; not uploaded yet. The edge activates when its source arrives.
      </div>
    </div>
  )
}

function FeatureNode({ data }: NodeProps<FeatureNT>) {
  const { node } = data
  return (
    <div className="ln-card ln-card--feature nopan">
      <Ports />
      <button type="button" className="ln-head" onClick={() => data.onOpen(node)}>
        <span className="ln-kind">feature</span>
        <span className="ln-name" title={node.name}>
          {node.name}
        </span>
      </button>
      <div className="ln-src">
        registered <Flag tone="feat">feature</Flag>
        {node.stale && <Flag tone="stale">stale</Flag>}
      </div>
    </div>
  )
}

function ConsumerNode({ data }: NodeProps<ConsumerNT>) {
  const { node, reads } = data
  return (
    <div className="ln-card ln-card--consumer nopan">
      <Ports />
      <button type="button" className="ln-head" onClick={() => data.onOpen(node)}>
        <span className="ln-kind">consumer</span>
        <span className="ln-name" title={node.name}>
          {node.name}
        </span>
      </button>
      <div className="ln-body">
        reads {reads} {reads === 1 ? 'feature' : 'features'} in view
      </div>
    </div>
  )
}

const NODE_TYPES = {
  lnTable: TableNode,
  lnAnchorCol: AnchorColNode,
  lnStub: StubNode,
  lnFeature: FeatureNode,
  lnConsumer: ConsumerNode,
}

// ---- pure helpers over the wire graph -------------------------------------------------------

function idSource(id: string): string {
  // "gl:public.batches.batch_id" -> "gl" (a stub's declaring source lives only in its id).
  const i = id.indexOf(':')
  return i === -1 ? id : id.slice(0, i)
}

// A drift-vouch timestamp as a short relative phrase for the drawer; the exact instant rides along
// in the <time dateTime> attribute, so nothing is lost. Pure given Date.now() — it never invents
// precision (an unparseable value is echoed verbatim).
function relativeVouched(iso: string): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return iso
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (secs < 60) return 'just now'
  const mins = Math.round(secs / 60)
  if (mins < 60) return `${mins} minute${mins === 1 ? '' : 's'} ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`
  const days = Math.round(hours / 24)
  return `${days} day${days === 1 ? '' : 's'} ago`
}

function shortRef(n: LineageNode | undefined, id: string): string {
  if (!n) return id
  if (n.kind === 'column') return `${n.table}.${n.column}`
  if (n.kind === 'table') return n.table ?? id
  return n.name ?? id
}

function a11yLine(e: LineageEdge, byId: Map<string, LineageNode>): string {
  const from = byId.get(e.from)
  const to = byId.get(e.to)
  if (e.kind === 'join') {
    const parts = [`${shortRef(from, e.from)} joins ${shortRef(to, e.to)}`]
    if (e.cardinality) parts.push(e.cardinality)
    parts.push(e.resolved ? 'verified' : 'declared, target not uploaded')
    return parts.join(' · ')
  }
  if (e.kind === 'entity_bridge') {
    // Name the COLUMNS. Describing a bridge table-to-table made six genuinely different column
    // links render as six identical sentences, and hid the only one that matters
    // (cust_num <-> cif_id) among five weak branch pairs.
    //
    // `entity_id` comes off the EDGE. Reading it from the node's `entity` field always fell through
    // to "shared", because graph_node.entity is null on every column — the same never-populated
    // column that kept this expansion from firing at all.
    //
    const entity = e.entity_id ?? from?.entity ?? to?.entity ?? 'shared'
    const target = to
      ? `${to.catalog_source ?? idSource(e.to)}.${shortRef(to, e.to)}`
      : e.to
    const review = e.link_review_status === 'human_verified'
      ? 'endorsed by a person'
      : e.link_review_status === 'not_governed'
        ? 'advisory, not governed'
        : 'not yet reviewed'
    const safety = e.execution_eligible
      ? 'automatically validated for execution'
      : `execution ${e.realization_safety_status ?? 'not evaluated'}`
    // The VERDICT first, then the reason behind it. `why` explains the rank ("neither side is a
    // key — types match but this may not be a real join"); on the canvas that compresses to one
    // word, and here there is room for both.
    const rank = (e.strength ?? 0) >= 10 ? 'strong' : 'weak'
    const because = e.why ? ` · ${e.why}` : ''
    return `${shortRef(from, e.from)} links to ${target} on ${entity} · ${rank} · `
      + `${review} · ${safety}${because}`
  }
  if (e.kind === 'derives') {
    return `${shortRef(from, e.from)} derives feature ${shortRef(to, e.to)} · registered`
  }
  return `${shortRef(from, e.from)} is read by ${shortRef(to, e.to)} · consumer`
}


// ---- decomposed relationship trust (concept decisions 3 and 5) --------------------------------
// "Match strength, human review and execution validation are separate ideas. A single ambiguous
// 'governed' label cannot conceal missing execution evidence."
//
// Every badge below maps to a field the server actually sent. The three axes exist ONLY on
// entity_bridge edges (`strength`/`trust_kind`, `link_review_status`, `execution_eligible` +
// `realization_safety_status`); a join, derives or consumes edge carries none of them. For those we
// say so rather than render three empty axes, which would read as "evidence is missing" when the
// truth is "this axis does not apply to this kind of link".
function TrustAxes({ edge }: { edge: LineageEdge }) {
  if (edge.kind !== 'entity_bridge') {
    return (
      <p className="ln-trust-na hint">
        Match, review and execution axes apply to entity bridges. A {edge.kind} link carries
        {edge.cardinality ? ` cardinality ${edge.cardinality}` : ' no cardinality'} and
        {edge.resolved ? ' resolved endpoints' : ' unresolved endpoints'}.
      </p>
    )
  }
  const strong = (edge.strength ?? 0) >= 10
  const review = edge.link_review_status
  const reviewed = review === 'human_verified'
  return (
    <div className="ln-trustline" aria-label="Relationship trust">
      <span className={`badge ${strong ? 'gj-verified' : 'gj-partial'}`}>
        {strong ? 'Strong match' : 'Weak match'}
      </span>
      <span className={`badge ${reviewed ? 'gj-verified' : 'gj-none'}`}>
        {reviewed
          ? 'Reviewed by a person'
          : review === 'not_governed' ? 'Advisory, not governed' : 'Not yet reviewed'}
      </span>
      <span className={`badge ${edge.execution_eligible ? 'gj-verified' : 'gj-partial'}`}>
        {edge.execution_eligible ? 'Execution-validated' : 'Not execution-validated'}
      </span>
    </div>
  )
}

// Decision 3: "Capability is the narrative." The heading says what the link LETS YOU DO; the line
// under it names the two columns and the entity, and `why` (the server's own rationale) explains
// the rank rather than leaving "weak" unexplained.
function RelationshipBlock({
  edge,
  byId,
}: {
  edge: LineageEdge
  byId: Map<string, LineageNode>
}) {
  const from = byId.get(edge.from)
  const to = byId.get(edge.to)
  const entity = edge.entity_id ?? 'shared'
  const capability = edge.kind === 'entity_bridge'
    ? `Connect ${entity} records across catalogs`
    : edge.kind === 'join'
      ? 'Join these tables in a feature'
      : edge.kind === 'derives' ? 'Feeds a registered feature' : 'Read by a consumer'
  return (
    <div className="ln-relationship">
      <strong>{capability}</strong>
      <p>
        <span className="mono">{shortRef(from, edge.from)}</span> maps to{' '}
        <span className="mono">{shortRef(to, edge.to)}</span> for the {entity} entity.
      </p>
      <TrustAxes edge={edge} />
      {!edge.execution_eligible && edge.realization_safety_status && (
        <p className="ln-why hint">
          Execution safety: {edge.realization_safety_status.replaceAll('_', ' ')}.
        </p>
      )}
      {edge.why && <p className="ln-why hint">{edge.why}</p>}
    </div>
  )
}

// ---- the view --------------------------------------------------------------------------------

export function LineageView({
  anchor,
  onBackToResults,
}: {
  anchor: SearchHit
  onBackToResults?: () => void
}) {
  const { navigate } = useHashRoute()
  const [graph, setGraph] = useState<LineageGraph | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [layersOn, setLayersOn] = useState<Record<LineageLayer, boolean>>({
    joins: true,
    entity: true,
    features: true,
  })
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set())
  const [showAll, setShowAll] = useState<ReadonlySet<string>>(new Set())
  const [expandedUnits, setExpandedUnits] = useState<ReadonlySet<string>>(new Set())
  const [exhausted, setExhausted] = useState<ReadonlySet<string>>(new Set())
  const [expanding, setExpanding] = useState<ReadonlySet<string>>(new Set())
  const [traceId, setTraceId] = useState<string | null>(null)
  const [drawerId, setDrawerId] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [expandError, setExpandError] = useState('')

  // The anchor's own units: its table and, when the hit is a column, the column itself —
  // which renders as its OWN node (the map's center), not as a row inside the table card.
  const anchorUnitId = `${anchor.catalog_source}:public.${anchor.table}`
  const matchId = anchor.column ? `${anchor.catalog_source}:${anchor.object_ref}` : null

  // Out-of-order guard: only the latest anchor fetch may apply. Expander merges go through
  // graphRef (always the latest committed graph) so two in-flight expansions merge in the
  // order their responses land, never clobbering each other.
  const seq = useRef(0)
  const graphRef = useRef<LineageGraph | null>(null)
  // Set once per anchor load, when the anchor node has been centered at a readable zoom.
  const centered = useRef(false)
  // In-flight expansion fetches, aborted on unmount so an orphaned promise never resolves into
  // setState on a gone component. The anchor fetch owns its own controller (aborted on re-anchor).
  const expandCtrls = useRef<Set<AbortController>>(new Set())
  const rf = useRef<ReactFlowInstance | null>(null)
  // The same per-table suggestions read the asset dossier makes, filtered to this column's
  // operands — one implementation and one set of read-scope rules across both surfaces.
  const { matching } = useColumnSuggestions(anchor.catalog_source, {
    ...anchor,
    kind: anchor.column ? 'column' : 'table',
    graph_ref: anchor.object_ref,
    logical_ref: anchor.object_ref,
    source: anchor.catalog_source,
    schema_name: null,
    operational_type: anchor.data_type,
    declared_type: anchor.data_type,
  })

  useEffect(() => {
    const id = ++seq.current
    const ctrl = new AbortController()
    centered.current = false
    setLoading(true)
    setError('')
    lineageGraph(anchor.object_ref, anchor.catalog_source, {
      direction: DIRECTION,
      depth: 1,
      signal: ctrl.signal,
    })
      .then(g => {
        if (id !== seq.current) return
        graphRef.current = g
        setGraph(g)
        setLoading(false)
        // A column anchor starts its table compact (the column is the center, not the card);
        // a re-anchor resets both collapse and full-list state to that default.
        setCollapsed(anchor.column ? new Set([anchorUnitId]) : new Set())
        setShowAll(new Set())
      })
      .catch((err: unknown) => {
        if (id !== seq.current || ctrl.signal.aborted) return
        setError(err instanceof ApiError ? err.detail : String(err))
        setLoading(false)
      })
    return () => ctrl.abort()
    // anchorUnitId and anchor.column are derived from the same hit as object_ref; listing them
    // keeps the deps honest without ever adding a re-fetch.
  }, [anchor.object_ref, anchor.catalog_source, anchor.column, anchorUnitId])

  useEffect(() => () => {
    for (const c of expandCtrls.current) c.abort()
  }, [])

  const byId = useMemo(
    () => new Map((graph?.nodes ?? []).map(n => [n.id, n] as const)),
    [graph],
  )

  // Column -> owning table unit (columns render as rows inside their table card). The one
  // exception is the anchored column: it is its own unit (its own node), so relationship
  // edges that end on it attach to the anchor node, never to a row in the table card.
  const unitOf = useMemo(() => {
    const tableByKey = new Map<string, string>()
    for (const n of graph?.nodes ?? []) {
      if (n.kind === 'table') tableByKey.set(`${n.catalog_source}|${n.table}`, n.id)
    }
    return (id: string): string => {
      if (id === matchId) return id
      const n = byId.get(id)
      if (n?.kind === 'column' && n.resolved) {
        return tableByKey.get(`${n.catalog_source}|${n.table}`) ?? id
      }
      return id
    }
  }, [graph, byId, matchId])

  // Edges the canvas draws: everything except structural containment, filtered by the layer
  // toggles. Client-side only; the fetch always carries all permitted layers.
  const drawnEdges = useMemo(
    () => (graph?.edges ?? []).filter(e => e.kind !== 'contains' && layersOn[e.layer]),
    [graph, layersOn],
  )

  // A unit is visible when the anchor can still reach it over the toggled-on layers: turning
  // a layer off removes that class of relationship AND anything only known through it.
  const visibleUnits = useMemo(() => {
    const adj = new Map<string, string[]>()
    for (const e of drawnEdges) {
      const a = unitOf(e.from)
      const b = unitOf(e.to)
      adj.set(a, [...(adj.get(a) ?? []), b])
      adj.set(b, [...(adj.get(b) ?? []), a])
    }
    const seen = new Set<string>([anchorUnitId])
    if (matchId) seen.add(matchId) // the anchor column node is always in view
    const queue = [...seen]
    while (queue.length > 0) {
      const u = queue.shift() as string
      for (const v of adj.get(u) ?? []) {
        if (!seen.has(v)) {
          seen.add(v)
          queue.push(v)
        }
      }
    }
    return seen
  }, [drawnEdges, unitOf, anchorUnitId, matchId])

  const visibleEdges = useMemo(
    () => drawnEdges.filter(e => visibleUnits.has(unitOf(e.from)) && visibleUnits.has(unitOf(e.to))),
    [drawnEdges, visibleUnits, unitOf],
  )

  // Endpoints of every visible entity bridge, so a column row can say it is mapped rather than
  // leaving the reader to trace the line back to it.
  const mappedIds = useMemo(() => {
    const ids = new Set<string>()
    for (const e of visibleEdges) {
      if (e.kind !== 'entity_bridge') continue
      ids.add(e.from)
      ids.add(e.to)
    }
    return ids
  }, [visibleEdges])

  // Counts for the map label and the canvas summary, derived from the graph already in memory.
  // The artifact's decision 14 is explicit: "The UI derives simple counts locally; it does not
  // require a dashboard-summary endpoint."
  const assetCount = visibleUnits.size
  const bridgeCount = visibleEdges.filter(e => e.kind === 'entity_bridge').length
  const featureCount = visibleEdges.filter(e => e.kind === 'derives').length
  const joinCount = visibleEdges.filter(e => e.kind === 'join').length

  // Trace: the clicked column's feature-lineage path (derives -> feature -> consumers).
  const traced = useMemo(() => {
    const keys = new Set<string>()
    const features: string[] = []
    const consumers: string[] = []
    if (traceId) {
      const featureIds = new Set<string>()
      for (const e of visibleEdges) {
        if (e.kind === 'derives' && e.from === traceId) {
          keys.add(dedupeKey(e))
          featureIds.add(e.to)
          features.push(shortRef(byId.get(e.to), e.to))
        }
      }
      for (const e of visibleEdges) {
        if (e.kind === 'consumes' && featureIds.has(e.from)) {
          keys.add(dedupeKey(e))
          consumers.push(shortRef(byId.get(e.to), e.to))
        }
      }
    }
    return { keys, features, consumers }
  }, [traceId, visibleEdges, byId])

  function openColumn(col: LineageNode) {
    setTraceId(prev => (prev === col.id ? null : col.id))
    setDrawerId(col.id)
  }
  function openNode(n: LineageNode) {
    setDrawerId(n.id)
  }
  // Stable so the drawer's Escape-key listener subscribes once, not on every parent render.
  const closeDrawer = useCallback(() => setDrawerId(null), [])
  function toggleTable(id: string) {
    setCollapsed(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
    // Collapsing always resets the card to the capped view on its next expand.
    setShowAll(prev => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  }
  function showAllColumns(id: string) {
    setShowAll(prev => new Set(prev).add(id))
  }

  async function expand(n: LineageNode) {
    if (!n.object_ref || !n.catalog_source || expanding.has(n.id)) return
    const startSeq = seq.current
    const ctrl = new AbortController()
    expandCtrls.current.add(ctrl)
    setExpanding(prev => new Set(prev).add(n.id))
    setExpandError('')
    setNote('')
    try {
      const more = await lineageGraph(n.object_ref, n.catalog_source, {
        direction: DIRECTION,
        depth: 1,
        signal: ctrl.signal,
      })
      if (startSeq !== seq.current || !graphRef.current) return
      const { graph: merged, grew } = mergeGraph(graphRef.current, more)
      graphRef.current = merged
      setGraph(merged)
      setExpandedUnits(prev => new Set(prev).add(n.id))
      if (!grew) {
        setExhausted(prev => new Set(prev).add(n.id))
        setNote(`No further neighbors around ${n.table}.`)
      }
    } catch (err) {
      if (ctrl.signal.aborted || startSeq !== seq.current) return
      setExpandError(err instanceof ApiError ? err.detail : String(err))
    } finally {
      expandCtrls.current.delete(ctrl)
      setExpanding(prev => {
        const next = new Set(prev)
        next.delete(n.id)
        return next
      })
    }
  }

  // ---- geometry: sizes + dagre layout (the EXPENSIVE step) ---------------------------------
  // Keyed only on what changes SHAPE — the graph, the visible units/edges, and collapse (which
  // resizes cards). Trace clicks, expander-flag flips, and match highlighting do NOT re-run
  // dagre; they restyle the cheap `flow` memo below, which reuses these positions.
  const layout = useMemo(() => {
    if (!graph) return { placed: [] as PlacedNode[], rowIds: new Set<string>(), contain: false }
    const columnsOf = new Map<string, LineageNode[]>()
    for (const n of graph.nodes) {
      if (n.kind === 'column' && n.resolved) {
        const unit = unitOf(n.id)
        if (unit !== n.id) columnsOf.set(unit, [...(columnsOf.get(unit) ?? []), n])
      }
    }
    // Row priority under the cap: the matched column first (when it renders as a row), then
    // edge-endpoint columns (their handles anchor visible edges), then marker-carrying columns
    // (grain / as-of / entity key), then the wire order. Stable sort keeps ties in order.
    const endpoint = new Set<string>()
    for (const e of visibleEdges) {
      endpoint.add(e.from)
      endpoint.add(e.to)
    }
    const marked = (c: LineageNode) => c.grain || c.as_of || Boolean(c.entity)
    const rank = (c: LineageNode) =>
      c.id === matchId ? 0 : endpoint.has(c.id) ? 1 : marked(c) ? 2 : 3
    const rowIds = new Set<string>()
    const placed: PlacedNode[] = []
    for (const n of graph.nodes) {
      if (!visibleUnits.has(n.id)) continue
      if (n.kind === 'table') {
        const cols = [...(columnsOf.get(n.id) ?? [])].sort((a, b) => rank(a) - rank(b))
        const compactAnchor = matchId !== null && n.id === anchorUnitId
        let rows: LineageNode[] = []
        let more = 0
        let scroll = false
        if (!collapsed.has(n.id)) {
          if (showAll.has(n.id)) {
            rows = cols
            scroll = cols.length * ROW_H > LIST_MAX_H
          } else {
            rows = cols.slice(0, COL_CAP)
            more = cols.length - rows.length
          }
        } else if (compactAnchor) {
          // Compact card: keep the structural spine (grain / as-of / entity key) and every
          // column that anchors a visible edge; the rest stays behind the caret.
          rows = cols.filter(c => endpoint.has(c.id) || marked(c)).slice(0, COL_CAP)
        }
        for (const r of rows) rowIds.add(r.id)
        const listH = scroll ? LIST_MAX_H : rows.length * ROW_H
        const h =
          HEAD_H +
          SRC_H +
          /* the stale band is gone; its height must go with it or every stale card keeps a
             58px hole where it used to be */
          (rows.length > 0 ? listH + PAD_H : 0) +
          (more > 0 ? MORE_H : 0)
        placed.push({
          node: n, type: 'lnTable', x: 0, y: 0, w: W_TABLE, h, rows,
          // The count chip reports the table's true width; the anchored column is drawn as
          // its own node, so it is added back in.
          total: cols.length + (compactAnchor ? 1 : 0), more, scroll,
        })
      } else if (n.kind === 'column' && n.id === matchId && n.resolved) {
        placed.push({
          node: n, type: 'lnAnchorCol', x: 0, y: 0, w: W_TABLE,
          h: HEAD_H + (n.concept ? SRC_H : 0), rows: [], total: 0, more: 0, scroll: false,
        })
      } else if (n.kind === 'column' && !n.resolved) {
        placed.push({
          node: n, type: 'lnStub', x: 0, y: 0, w: W_TABLE, h: HEAD_H + 64,
          rows: [], total: 0, more: 0, scroll: false,
        })
      } else if (n.kind === 'feature') {
        placed.push({
          node: n, type: 'lnFeature', x: 0, y: 0, w: W_FEATURE, h: HEAD_H + SRC_H + PAD_H,
          rows: [], total: 0, more: 0, scroll: false,
        })
      } else if (n.kind === 'consumer') {
        placed.push({
          node: n, type: 'lnConsumer', x: 0, y: 0, w: W_CONSUMER, h: HEAD_H + 30,
          rows: [], total: 0, more: 0, scroll: false,
        })
      }
    }

    const placedIds = new Set(placed.map(p => p.node.id))
    // The structural containment edge (anchor column -> its table card) is drawable only when
    // both ends made it onto the canvas.
    const contain = matchId !== null && placedIds.has(matchId) && placedIds.has(anchorUnitId)
    const g = new dagre.graphlib.Graph()
    // The artifact's cards sit clearly apart; at nodesep 36 / ranksep 110 they crowded and the
    // edge labels had nowhere to sit without covering a card. Widened both, and the margin
    // with them so the outermost card is never flush against the canvas edge.
    g.setGraph({ rankdir: 'LR', nodesep: 64, ranksep: 160, marginx: 40, marginy: 40 })
    g.setDefaultEdgeLabel(() => ({}))
    for (const p of placed) g.setNode(p.node.id, { width: p.w, height: p.h })
    if (contain && matchId) g.setEdge(matchId, anchorUnitId)
    for (const e of visibleEdges) {
      const a = unitOf(e.from)
      const b = unitOf(e.to)
      if (a !== b) g.setEdge(a, b)
    }
    dagre.layout(g)
    for (const p of placed) {
      const gp = g.node(p.node.id)
      p.x = gp.x - p.w / 2
      p.y = gp.y - p.h / 2
    }
    return { placed, rowIds, contain }
  }, [graph, visibleEdges, visibleUnits, collapsed, showAll, unitOf, matchId, anchorUnitId])

  // ---- styling: turn placed geometry into xyflow nodes/edges (the CHEAP, per-interaction step)
  const flow = useMemo(() => {
    const consumerReads = new Map<string, number>()
    for (const e of visibleEdges) {
      if (e.kind === 'consumes') consumerReads.set(e.to, (consumerReads.get(e.to) ?? 0) + 1)
    }
    const nodes: Node[] = layout.placed.map(p => {
      const n = p.node
      const base = {
        id: n.id,
        position: { x: p.x, y: p.y },
        width: p.w,
        height: p.h,
        draggable: false,
      }
      if (p.type === 'lnTable') {
        return {
          ...base,
          type: 'lnTable',
          data: {
            node: n,
            rows: p.rows,
            total: p.total,
            more: p.more,
            scroll: p.scroll,
            countChip: matchId !== null && n.id === anchorUnitId,
            collapsed: collapsed.has(n.id),
            matchId,
            traceId,
            // Under a column anchor the compact table card keeps its expand-neighbors chip:
            // the initial fetch was anchored on the column, not the table.
            expandable:
              (n.id !== anchorUnitId || matchId !== null) &&
              !expandedUnits.has(n.id) &&
              !exhausted.has(n.id),
            expanding: expanding.has(n.id),
            onToggle: toggleTable,
            onShowAll: showAllColumns,
            onColumn: openColumn,
            onOpen: openNode,
            onExpand: expand,
            mapped: mappedIds,
          } satisfies TableData,
        }
      }
      if (p.type === 'lnAnchorCol') {
        return {
          ...base,
          type: 'lnAnchorCol',
          data: {
            node: n, traceId, onColumn: openColumn,
            // The NODE's own concept wins; the SearchHit only supplies the type the graph
            // payload does not carry. Preferring the hit would overwrite what the graph said
            // about this column with what the search index said about the anchor.
            meta: [n.concept ?? anchor.concept,
              anchor.data_type && anchor.data_type.toLowerCase() !== 'unknown'
                && `${anchor.data_type} · source declared`]
              .filter(Boolean).join(' · ') || null,
          } satisfies AnchorColData,
        }
      }
      if (p.type === 'lnStub') {
        return { ...base, type: 'lnStub', data: { node: n } satisfies StubData }
      }
      if (p.type === 'lnFeature') {
        return { ...base, type: 'lnFeature', data: { node: n, onOpen: openNode } satisfies FeatureData }
      }
      return {
        ...base,
        type: 'lnConsumer',
        data: { node: n, reads: consumerReads.get(n.id) ?? 0, onOpen: openNode } satisfies ConsumerData,
      }
    })

    const edges: Edge[] = visibleEdges.map(e => {
      const sourceUnit = unitOf(e.from)
      const targetUnit = unitOf(e.to)
      const isTrace = traced.keys.has(dedupeKey(e))
      // A type-only link is drawn faint and thin rather than removed — the owner's rule is that a
      // link is usable before anyone confirms it, so weakness is a matter of EMPHASIS.
      const weakLink = e.kind === 'entity_bridge' && (e.strength ?? 0) < 10
      let stroke = 'var(--ln-join)'
      let label: string
      if (e.kind === 'join') {
        label = e.resolved
          ? (e.cardinality ?? 'join')
          : [e.cardinality, 'declared'].filter(Boolean).join(' · ')
      } else if (e.kind === 'entity_bridge') {
        stroke = 'var(--warn)'
        // `entity_id` off the EDGE. Reading the NODE's `entity` always fell through to the bare
        // word "entity" — graph_node.entity is null on every column — so four different links drew
        // four identical labels stacked on top of each other.
        const entity = e.entity_id ?? byId.get(e.from)?.entity ?? byId.get(e.to)?.entity
        // A grain on either side (weight 10) means the column really is that table's key; below
        // that the two columns merely share a type, which is how `cust_prim_branch_nm` came to
        // "link" to `sol_desc` — a name to a description. Rank it, never hide it: the link is real
        // enough to show and weak enough to say so.
        // Both words, not just the negative one. Marking only the weak links left the strong one
        // unlabelled, so "no marker" had to be read as "good" — an absence is a poor way to state
        // a verdict, and it is invisible when only one link is on screen.
        const keyed = (e.strength ?? 0) >= 10
        // C8: two segments, not three. The third ("governed"/"advisory"/"executable") is now the
        // inspector's own review axis, so on the canvas it only made the label long enough to clip
        // mid-word over a node. The artifact labels this edge "customer · strong".
        label = `${entity ?? 'linked'} · ${keyed ? 'strong' : 'weak'}`
      } else {
        stroke = 'var(--proposal)'
        label = e.kind
      }
      return {
        id: `${e.kind}|${e.from}|${e.to}`,
        source: sourceUnit,
        target: targetUnit,
        // Attach to a column handle only when that row is actually rendered (capped and
        // compact cards hide rows); otherwise fall back to the card-level port.
        sourceHandle: e.from !== sourceUnit && layout.rowIds.has(e.from) ? e.from : undefined,
        targetHandle: e.to !== targetUnit && layout.rowIds.has(e.to) ? e.to : undefined,
        label,
        className: isTrace ? 'ln-edge ln-edge--trace' : 'ln-edge',
        animated: false, // reduced-motion safe: no marching-ants edges anywhere
        style: isTrace
          ? { stroke: 'var(--accent)', strokeWidth: 3 }
          : {
              stroke,
              // Thinner and semi-transparent for a type-only link, so a grain-backed one reads as
              // the stronger claim at a glance without the weak one disappearing.
              strokeWidth: weakLink ? 1 : 1.5,
              ...(weakLink ? { opacity: 0.45 } : {}),
              ...(e.resolved ? {} : { strokeDasharray: '6 5' }),
            },
      }
    })
    if (layout.contain && matchId) {
      // Structure, not lineage: the containment tie from the anchor column to its table
      // card draws hairline-quiet and unlabeled, and never joins a trace.
      edges.push({
        id: `contains|${matchId}|${anchorUnitId}`,
        source: matchId,
        target: anchorUnitId,
        className: 'ln-edge ln-edge--contain',
        animated: false,
        style: { stroke: 'var(--line-strong)', strokeWidth: 1, strokeDasharray: '2 5' },
      })
    }
    return { nodes, edges }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- handlers are stable per render
  }, [
    layout,
    visibleEdges,
    collapsed,
    traced,
    matchId,
    traceId,
    anchorUnitId,
    expandedUnits,
    exhausted,
    expanding,
    unitOf,
    byId,
  ])

  // On the anchor's first paint, center the anchor itself (the column node when column-anchored,
  // the table card otherwise) at a readable zoom: fitView over a big graph pins the anchor to the
  // viewport edge. Later node-set changes (expansion merges, layer toggles) refit around
  // everything so freshly placed nodes come into view. duration 0 keeps it reduced-motion safe.
  const nodeCount = flow.nodes.length
  useEffect(() => {
    const inst = rf.current
    if (!inst || nodeCount === 0) return
    // FIT, do not centre-at-zoom-1. The centring existed because fitView over a 188-node graph
    // pinned the anchor to the viewport edge; now that a neighbourhood is the participating columns
    // only, centring instead leaves a small cluster marooned in a large empty canvas. `maxZoom`
    // stops a two-node graph being blown up absurdly, and the padding keeps edge labels off the rim.
    inst.fitView({ padding: 0.18, maxZoom: 1.1, duration: 0 })
    centered.current = true
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reframe only when the node set changes
  }, [nodeCount])

  const drawerNode = drawerId ? byId.get(drawerId) : undefined

  if (loading) {
    return (
      <div className="ln-wrap ln-wrap--placeholder">
        <p role="status" className="hint">
          Mapping lineage around <code>{anchor.object_ref}</code>…
        </p>
      </div>
    )
  }
  if (error || !graph) {
    return (
      <div className="ln-wrap ln-wrap--placeholder">
        <p role="alert" className="error">
          Lineage failed: {error || 'no graph returned'}
        </p>
        <p className="hint">
          Unknown refs and refs your roles cannot see look the same: not found. The graph shows
          only objects you can read.
        </p>
      </div>
    )
  }

  // Zero drawable edges for the current layer toggles (a declared-but-pending join still
  // counts: it draws as a ghost card and dashed edge). The panel explains, per toggled-on
  // layer, what would create that lineage and where to act — never a bare canvas.
  const showWhyEmpty = visibleEdges.length === 0
  const refWord = anchor.column ? 'column' : 'table'

  return (
    <>
      {/* Concept decision 2: "Panels never float over the map — Layers, empty states and the
          inspector occupy dedicated columns, preventing the overlap visible in the latest
          implementation." The layers fieldset and the why-empty note were ReactFlow <Panel>s
          drawn ON the canvas, and the drawer was position:absolute over its right edge, so
          all three covered nodes and labels. They are columns now. */}
      {/* The artifact's context bar: the graph is anchored on something, and the anchor was only
          named in a hint sentence above the canvas. Kind chip, full ref, the human wording under
          it, and the two actions the artifact keeps — back to results, and the asset dossier. */}
      <section className="ln-contextbar" aria-label="Graph anchor">
        <div className="ln-context-main">
          <span className="ln-context-kind">{anchor.column ? 'COL' : 'TBL'}</span>
          <div className="ln-context-name">
            <strong>{anchor.object_ref}</strong>
            <small>
              {[anchor.concept, anchor.catalog_source, anchor.table]
                .filter(Boolean).join(' · ')}
            </small>
          </div>
          {/* Only badges backed by a field the search hit actually carried. */}
          {anchor.entity && <span className="badge gj-proposed">{anchor.entity}</span>}
          {anchor.is_grain && <span className="badge grain">grain</span>}
          {anchor.is_as_of && <span className="badge asof">as-of</span>}
        </div>
        <div className="ln-context-actions">
          {onBackToResults && (
            <button type="button" className="btn btn--ghost" onClick={onBackToResults}>
              ← Results
            </button>
          )}
          <a
            className="btn btn--ghost"
            href={`#/asset?${new URLSearchParams({
              source: anchor.catalog_source, object_ref: anchor.object_ref,
            }).toString()}`}
          >
            View details
          </a>
        </div>
      </section>

      <div className="ln-wrap">
        <aside className="ln-tools" aria-label="Graph controls">
            {/* The artifact's four tool sections. A layer is a name AND what it means: a bare
                "Joins" checkbox assumes the reader already knows what the platform counts as one. */}
            <section className="ln-tool-section ln-tool-section--first">
              <h3 className="ln-micro">Relationship layers</h3>
              {(
                [
                  ['joins', 'Governed joins', 'Approved structural joins', 'var(--ln-join)'],
                  ['entity', 'Entity mappings', 'Same business entity across catalogs',
                    'var(--warn)'],
                  ['features', 'Registered features', 'Existing production lineage',
                    'var(--proposal)'],
                ] as const
              ).map(([layer, title, blurb, swatch]) => (
                <label key={layer} className="ln-layer">
                  <input
                    type="checkbox"
                    /* The visible label is two lines (name + meaning); the accessible name stays
                       the layer name alone so it is announced as a control, not a paragraph. */
                    aria-label={title}
                    checked={layersOn[layer]}
                    onChange={e => {
                      setLayersOn(prev => ({ ...prev, [layer]: e.target.checked }))
                    }}
                  />
                  <span className="ln-swatch" style={{ background: swatch }} aria-hidden="true" />
                  <span><strong>{title}</strong><small>{blurb}</small></span>
                </label>
              ))}
            </section>

            {/* Line meaning: the canvas draws three different strokes and nothing said which was
                which. The samples are aria-hidden — the adjacent text IS the meaning. */}
            <section className="ln-tool-section">
              <h3 className="ln-micro">Line meaning</h3>
              <div className="ln-legend">
                <div className="ln-legend-row">
                  <span className="ln-line-sample" aria-hidden="true" /><span>Verified join</span>
                </div>
                <div className="ln-legend-row">
                  <span className="ln-line-sample ln-line-sample--dashed" aria-hidden="true" />
                  <span>Entity mapping</span>
                </div>
                <div className="ln-legend-row">
                  <span className="ln-line-sample ln-line-sample--dotted" aria-hidden="true" />
                  <span>Containment</span>
                </div>
              </div>
            </section>

            {/* Scope: what the map currently covers, so a sparse graph reads as "one hop" rather
                than "this column has nothing". */}
            <section className="ln-tool-section">
              <h3 className="ln-micro">Current scope</h3>
              <div className="ln-scope-note">
                One hop around <span className="mono">{anchor.column ?? anchor.table}</span>.
                Expand a table to fetch its next neighborhood.
              </div>
            </section>

            {/* Read scope is enforced server-side, so an absent object is absent from the wire.
                Saying so is the difference between "nothing exists" and "nothing you may see". */}
            <section className="ln-tool-section">
              <h3 className="ln-micro">Visibility</h3>
              <div className="ln-scope-note">
                Only objects permitted for the current session are shown. Hidden objects are not
                counted.
              </div>
            </section>
          {showWhyEmpty && (
              <aside className="ln-empty" aria-label="Why nothing is drawn">
                <h3 className="micro-label">Nothing to draw yet</h3>
                {layersOn.joins && (
                  <p>
                    No joins proposed or approved yet. Proposals appear here after uploads are
                    enriched; approvals happen on the{' '}
                    <a
                      href="#/governance"
                      onClick={e => {
                        e.preventDefault()
                        navigate('governance')
                      }}
                    >
                      Governance screen
                    </a>
                    .
                  </p>
                )}
                {layersOn.entity && (
                  <p>
                    No entity relationship is visible yet. Advisory mappings, governed identifier
                    links, and executable realizations appear here as separate trust levels. Review
                    is available on the{' '}
                    <a
                      href="#/governance"
                      onClick={e => {
                        e.preventDefault()
                        navigate('governance')
                      }}
                    >
                      Governance screen
                    </a>
                    .
                  </p>
                )}
                {layersOn.features && (
                  <p>
                    No features are derived from this {refWord} yet. Features are created in the{' '}
                    <a
                      href="#/workbench"
                      onClick={e => {
                        e.preventDefault()
                        navigate('workbench')
                      }}
                    >
                      Workbench
                    </a>
                    .
                  </p>
                )}
              </aside>
          )}
        </aside>

        <div className="ln-canvas">
          {/* What this map IS, stated over it. Counts are derived locally from the loaded graph —
              the artifact's decision 14 forbids requiring a summary endpoint for them. */}
          <div className="ln-map-label">
            <b>{anchor.entity ? `${anchor.entity} neighborhood` : 'Relationship neighborhood'}</b>
            <span>
              {assetCount} {assetCount === 1 ? 'asset' : 'assets'}
              {bridgeCount > 0
                ? ` · ${bridgeCount} cross-catalog mapping${bridgeCount === 1 ? '' : 's'}`
                : ' · no cross-catalog mapping'}
            </span>
          </div>
        <ReactFlow
          nodes={flow.nodes}
          edges={flow.edges}
          nodeTypes={NODE_TYPES}
          onInit={inst => {
            rf.current = inst
          }}
          fitView
          minZoom={0.3}
          maxZoom={2}
          nodesDraggable={false}
          nodesConnectable={false}
          nodesFocusable
          edgesFocusable={false}
        >
          <Background gap={22} size={1} color="oklch(0.84 0.014 212)" />
          <Controls showInteractive={false} position="bottom-right" />
          {/* Only when there is something to navigate. On a pruned neighbourhood the minimap was
              a large panel rendering three grey blocks — cost with no information. */}
          {flow.nodes.length > 14 && (
          <MiniMap
            position="bottom-left"
            pannable={false}
            zoomable={false}
            nodeColor={n => (n.id === anchorUnitId ? 'var(--accent)' : 'var(--line-strong)')}
            maskColor="oklch(0.955 0.009 215 / 0.65)"
          />
          )}
        </ReactFlow>
          <div className="ln-canvas-summary" aria-hidden="true">
            <div className="ln-canvas-stat">
              <strong>
                {bridgeCount} business mapping{bridgeCount === 1 ? '' : 's'}
              </strong>
              <span>{bridgeCount === 0 ? 'No cross-catalog link in view' : 'Across catalogs'}</span>
            </div>
            <div className="ln-canvas-stat">
              <strong>
                {featureCount} registered feature{featureCount === 1 ? '' : 's'}
              </strong>
              <span>Recommendations shown separately</span>
            </div>
            <div className="ln-canvas-stat">
              <strong>{joinCount} governed join{joinCount === 1 ? '' : 's'}</strong>
              <span>Approved structural joins in view</span>
            </div>
          </div>
        </div>

        {/* A layout column, not a landmark: the Drawer inside already carries
            role/aria-label="Details", and nesting a second identical landmark makes the
            region ambiguous to assistive tech. */}
        <div className="ln-inspector">
        {drawerNode && (
          <Drawer
            node={drawerNode}
            anchor={anchor}
            anchorColId={matchId}
            traceId={traceId}
            traced={traced}
            onClose={closeDrawer}
          />
        )}
          <section className="ln-selected" aria-label="Selected asset">
            <div className="ln-selected-label">
              <span className="ln-micro">
                {(drawerNode ?? { kind: anchor.column ? 'column' : 'table' }).kind === 'column'
                  ? 'Selected column' : 'Selected table'}
              </span>
              {(drawerNode?.concept ?? anchor.concept) && (
                <span className="badge gj-proposed">
                  {drawerNode?.concept ?? anchor.concept}
                </span>
              )}
            </div>
            {/* The SHORT name, as the artifact does. The full object ref is already on the
                context bar above the workspace; repeating it here spent the widest line in the
                column on something the reader has already been told. */}
            <h2 className="ln-selected-name">
              {drawerNode
                ? (drawerNode.column ?? drawerNode.table ?? drawerNode.name ?? drawerNode.id)
                : (anchor.column ?? anchor.table)}
            </h2>
            {anchor.definition
              ? <p className="ln-selected-def">{anchor.definition}</p>
              : <p className="ln-selected-def hint">No definition is held for this column.</p>}
            <div className="ln-pillrow">
              {anchor.data_type && <span className="badge gj-none">{anchor.data_type}</span>}
              {anchor.is_grain && <span className="badge grain">grain</span>}
              {anchor.is_as_of && <span className="badge asof">as-of</span>}
            </div>
            {/* Only axes the search hit actually carries. Grain-use and join-use readiness are
                not passed to this component, so they are absent rather than guessed. */}
            <div className="ln-fact-grid">
              {([
                ['Domain', anchor.domain],
                ['Entity', anchor.entity],
                ['Unit', anchor.unit],
                ['Sensitivity', anchor.sensitivity],
              ] as const).filter(([, v]) => !!v).map(([label, value]) => (
                <div className="ln-fact" key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
          </section>
          {drawerNode && (() => {
            const mine = visibleEdges.filter(e => e.from === drawerNode.id || e.to === drawerNode.id)
            if (mine.length === 0) return null
            return (
              <section className="ln-inspector-section" aria-label="Relationship trust">
                <h3 className="micro-label">Relationships</h3>
                {mine.map(e => (
                  <RelationshipBlock key={`${e.kind}|${e.from}|${e.to}`} edge={e} byId={byId} />
                ))}
              </section>
            )
          })()}
          {drawerNode && matching.length > 0 && (
            <section className="ln-inspector-section" aria-label="Recommended features">
              <h3 className="ln-micro">Recommended features using this column</h3>
              <div className="ln-recommendations">
                {matching.slice(0, 3).map(hit => (
                  <a
                    key={hit.suggestion.suggestion_id}
                    className="ln-recommendation"
                    href={`#/suggested?${new URLSearchParams({
                      source: anchor.catalog_source, table: anchor.table,
                    }).toString()}`}
                  >
                    <strong>{hit.suggestion.display_name || hit.suggestion.name}</strong>
                    <span>
                      {hit.suggestion.business_interpretation?.value
                        ?? hit.suggestion.business_value?.value
                        ?? hit.suggestion.recipe}
                    </span>
                    <em>Open recommendation →</em>
                  </a>
                ))}
              </div>
              {/* Decision 4: "explicitly separated from registered feature lineage". A discovery
                  candidate and a shipped feature are different claims about the world. */}
              <p className="ln-recommendation-note">
                Recommendations are discovery candidates — not registered lineage.
              </p>
            </section>
          )}
          {!drawerNode && (
            <p className="hint ln-inspector-empty">
              Select a node to see its identity, relationship trust and the features it can
              support.
            </p>
          )}
          {/* The artifact pins two actions to the foot of the inspector. */}
          <div className="ln-inspector-actions">
            <a
              className="btn btn--ghost"
              href={`#/asset?${new URLSearchParams({
                source: anchor.catalog_source, object_ref: anchor.object_ref,
              }).toString()}`}
            >
              View details
            </a>
            {anchor.table && (
              <a
                className="btn btn--primary"
                href={`#/suggested?${new URLSearchParams({
                  source: anchor.catalog_source, table: anchor.table,
                }).toString()}`}
              >
                All recommendations
              </a>
            )}
          </div>
        </div>
      </div>

      {graph.truncated && (
        <p className="hint" role="status">
          The map was cut at the node limit. Expand a node to fetch more around it.
        </p>
      )}
      {note && (
        <p className="hint" role="status">
          {note}
        </p>
      )}
      {expandError && (
        <p className="error" role="alert">
          Expand failed: {expandError}
        </p>
      )}

      {/* A readable text equivalent of the canvas. It exists for screen readers, but it is the
          clearer view for everyone when edges overlap — so it gets a name that means something to a
          banker rather than "accessible parallel list", which describes the MECHANISM. */}
      <section className="ln-a11y" aria-label="Links in this view, as text">
        <h3 className="micro-label">Links in this view</h3>
        {layout.contain && matchId && (
          // The structural containment tie, kept out of the relationship list so the list
          // stays a faithful mirror of the drawn lineage edges.
          <p className="hint">
            {byId.get(matchId)?.column ?? anchor.column} belongs to {anchor.table}
          </p>
        )}
        {visibleEdges.length === 0 ? (
          <p className="hint">No edges in view.</p>
        ) : (
          <ul>
            {visibleEdges.map(e => (
              <li key={`${e.kind}|${e.from}|${e.to}`}>{a11yLine(e, byId)}</li>
            ))}
          </ul>
        )}
      </section>
    </>
  )
}

// ---- detail drawer ---------------------------------------------------------------------------
// Rendered entirely from the lineage node payload (plus the anchor SearchHit for the anchor
// column itself, which is the same object the user searched). No extra fetches.

function Drawer({
  node,
  anchor,
  anchorColId,
  traceId,
  traced,
  onClose,
}: {
  node: LineageNode
  anchor: SearchHit
  anchorColId: string | null
  traceId: string | null
  traced: { features: string[]; consumers: string[] }
  onClose: () => void
}) {
  const closeRef = useRef<HTMLButtonElement>(null)
  // Capture the button that opened the drawer once, and return focus to it on close so keyboard
  // users are not dumped at the top of the document (WCAG 2.4.3). Runs before the focus-move
  // effect below, so document.activeElement is still the invoking column/node button.
  useEffect(() => {
    const invoker = document.activeElement as HTMLElement | null
    return () => invoker?.focus?.()
  }, [])
  useEffect(() => {
    closeRef.current?.focus()
  }, [node.id])
  // Escape closes the drawer, the standard dismissal for a transient detail panel.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const isAnchorCol = node.id === anchorColId
  const showTrace = traceId === node.id && traced.features.length > 0

  return (
    <aside className="ln-drawer" aria-label="Details">
      {/* Its own row rather than a float: a floated button reserves no space, so a long object ref
          ran underneath it and the two collided. */}
      <div className="ln-drawer-header">
        <button type="button" className="ln-drawer-close" ref={closeRef} onClick={onClose}>
          Close
        </button>
      </div>
      {node.kind === 'column' && (
        <>
          <h3 className="ln-drawer-title">{node.object_ref}</h3>
          <p className="ln-drawer-sub">
            {node.resolved
              ? `${node.catalog_source} · ${node.table}`
              : 'declared join target · not uploaded yet'}
          </p>
          {isAnchorCol && anchor.definition && (
            <p className="ln-drawer-sub">{anchor.definition}</p>
          )}
          <div className="ln-drawer-chips">
            {node.grain && <Flag tone="grain">grain</Flag>}
            {node.as_of && <Flag tone="asof">as-of</Flag>}
            {node.sensitivity && <Flag tone="pii">{node.sensitivity}</Flag>}
            {node.stale && <Flag tone="stale">stale</Flag>}
          </div>
          <dl className="ln-drawer-kv">
            {node.resolved && (
              <>
                <dt>source</dt>
                <dd>{node.catalog_source}</dd>
              </>
            )}
            <dt>table</dt>
            <dd>{node.table}</dd>
            <dt>column</dt>
            <dd>{node.column}</dd>
            {node.entity && (
              <>
                <dt>entity</dt>
                <dd>{node.entity}</dd>
              </>
            )}
            {node.concept && (
              <>
                <dt>concept</dt>
                <dd>{node.concept}</dd>
              </>
            )}
            {node.domain && (
              <>
                <dt>domain</dt>
                <dd>{node.domain}</dd>
              </>
            )}
            {node.as_of_basis && (
              <>
                <dt>as-of basis</dt>
                <dd>{node.as_of_basis}</dd>
              </>
            )}
            {isAnchorCol && anchor.data_type && (
              <>
                <dt>type</dt>
                <dd>{anchor.data_type}</dd>
              </>
            )}
            {isAnchorCol && anchor.additivity && (
              <>
                <dt>additivity</dt>
                <dd>{anchor.additivity}</dd>
              </>
            )}
            {isAnchorCol && anchor.unit && (
              <>
                <dt>unit</dt>
                <dd>
                  {anchor.unit}
                  {anchor.currency ? ` (${anchor.currency})` : ''}
                </dd>
              </>
            )}
          </dl>
          {node.sensitivity && (
            <p className="ln-drawer-note">
              Visible because your roles can read {node.sensitivity}-tagged columns. Roles
              without that scope do not see this node at all.
            </p>
          )}
          {node.stale && (
            <p className="ln-drawer-sub">
              Stale snapshot: <code>{node.catalog_source}</code> is not currently vouched.
            </p>
          )}
          {showTrace && (
            <p className="ln-drawer-extra" role="status">
              Lineage traced: this column derives {traced.features.join(', ')}
              {traced.consumers.length > 0 ? `, read by ${traced.consumers.join(', ')}` : ''}.
            </p>
          )}
        </>
      )}
      {node.kind === 'feature' && (
        <>
          <h3 className="ln-drawer-title">{node.name}</h3>
          <p className="ln-drawer-sub">registered feature</p>
          <div className="ln-drawer-chips">
            <Flag tone="feat">feature</Flag>
            {/* the honest verification stamp: gauntlet-passed, NOT a production-value claim — a soft
                ok chip, quieter than the solid state chips (predictive value stays unverified) */}
            {node.verification && <Flag tone="ok">{node.verification}</Flag>}
            {node.stale && <Flag tone="stale">stale</Flag>}
          </div>
          <dl className="ln-drawer-kv">
            <dt>feature id</dt>
            <dd>{node.feature_id}</dd>
            <dt>freshness</dt>
            <dd>{node.stale ? 'stale' : 'fresh'}</dd>
          </dl>
          {node.rationale && (
            // the causal WHY it was born (its hypothesis); absent for directly-registered features
            <p className="ln-drawer-extra">Why: {node.rationale}</p>
          )}
          <p>
            <a href={`#/registry?id=${encodeURIComponent(node.feature_id ?? '')}`}>
              View in registry
            </a>
          </p>
        </>
      )}
      {node.kind === 'consumer' && (
        <>
          <h3 className="ln-drawer-title">{node.name}</h3>
          <p className="ln-drawer-sub">registered consumer of features</p>
        </>
      )}
      {node.kind === 'table' && (
        <>
          <h3 className="ln-drawer-title">{node.object_ref}</h3>
          <p className="ln-drawer-sub">{node.catalog_source}</p>
          {node.last_vouched_at && (
            <p className="ln-drawer-sub">
              Last vouched:{' '}
              <time dateTime={node.last_vouched_at}>
                {relativeVouched(node.last_vouched_at)}
              </time>
            </p>
          )}
          {node.quarantine_pending ? (
            <p className="ln-drawer-note">
              {node.quarantine_pending} {node.quarantine_pending === 1 ? 'row' : 'rows'} in the
              review queue. Fix the source file and re-upload to clear them.
            </p>
          ) : null}
          {node.stale && (
            <p className="ln-drawer-sub">
              Stale snapshot: <code>{node.catalog_source}</code> is not currently vouched.
            </p>
          )}
        </>
      )}
    </aside>
  )
}
