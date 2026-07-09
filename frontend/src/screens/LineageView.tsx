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
  Panel,
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
const ROW_H = 32 // column rows are real buttons: hit targets >= 32px (PRODUCT.md)
const PAD_H = 8
const NOTE_H = 58

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
  columns: LineageNode[]
  collapsed: boolean
  matchId: string | null
  traceId: string | null
  expandable: boolean
  expanding: boolean
  onToggle: (id: string) => void
  onColumn: (col: LineageNode) => void
  onOpen: (node: LineageNode) => void
  onExpand: (node: LineageNode) => void
}
type StubData = { node: LineageNode }
type FeatureData = { node: LineageNode; onOpen: (node: LineageNode) => void }
type ConsumerData = { node: LineageNode; reads: number; onOpen: (node: LineageNode) => void }

type TableNT = Node<TableData, 'lnTable'>
type StubNT = Node<StubData, 'lnStub'>
type FeatureNT = Node<FeatureData, 'lnFeature'>
type ConsumerNT = Node<ConsumerData, 'lnConsumer'>

// A node after dagre has placed it: geometry only (position + size + its column rows). The cheap
// `flow` memo turns these into xyflow nodes with the interaction-dependent data on top.
type PlacedNode = {
  node: LineageNode
  type: 'lnTable' | 'lnStub' | 'lnFeature' | 'lnConsumer'
  x: number
  y: number
  w: number
  h: number
  cols: LineageNode[]
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
  const { node, columns, collapsed, matchId, traceId } = data
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
      <div className="ln-src">
        {node.catalog_source} ·{' '}
        {node.stale ? <Flag tone="stale">stale</Flag> : <span className="ln-fresh">fresh</span>}
      </div>
      {node.stale && (
        // Generic phrasing (no source name) so the fixed-height note never clips: the source is
        // named on the src line right above, and the drawer carries the fully named guidance.
        <div className="ln-note">
          Not currently vouched. Re-upload this source to serve its facts.
        </div>
      )}
      {!collapsed && (
        <ul className="ln-cols">
          {columns.map(col => (
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
    const entity = from?.entity ?? to?.entity ?? 'shared'
    const target = to ? `${to.catalog_source ?? idSource(e.to)}.${to.table}` : e.to
    return `${from?.table ?? e.from} is ${entity} entity bridge to ${target} · declared, not value-verified`
  }
  if (e.kind === 'derives') {
    return `${shortRef(from, e.from)} derives feature ${shortRef(to, e.to)} · registered`
  }
  return `${shortRef(from, e.from)} is read by ${shortRef(to, e.to)} · consumer`
}

// ---- the view --------------------------------------------------------------------------------

export function LineageView({ anchor }: { anchor: SearchHit }) {
  const [graph, setGraph] = useState<LineageGraph | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [layersOn, setLayersOn] = useState<Record<LineageLayer, boolean>>({
    joins: true,
    entity: true,
    features: true,
  })
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set())
  const [expandedUnits, setExpandedUnits] = useState<ReadonlySet<string>>(new Set())
  const [exhausted, setExhausted] = useState<ReadonlySet<string>>(new Set())
  const [expanding, setExpanding] = useState<ReadonlySet<string>>(new Set())
  const [traceId, setTraceId] = useState<string | null>(null)
  const [drawerId, setDrawerId] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [expandError, setExpandError] = useState('')

  // Out-of-order guard: only the latest anchor fetch may apply. Expander merges go through
  // graphRef (always the latest committed graph) so two in-flight expansions merge in the
  // order their responses land, never clobbering each other.
  const seq = useRef(0)
  const graphRef = useRef<LineageGraph | null>(null)
  // In-flight expansion fetches, aborted on unmount so an orphaned promise never resolves into
  // setState on a gone component. The anchor fetch owns its own controller (aborted on re-anchor).
  const expandCtrls = useRef<Set<AbortController>>(new Set())
  const rf = useRef<ReactFlowInstance | null>(null)

  useEffect(() => {
    const id = ++seq.current
    const ctrl = new AbortController()
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
      })
      .catch((err: unknown) => {
        if (id !== seq.current || ctrl.signal.aborted) return
        setError(err instanceof ApiError ? err.detail : String(err))
        setLoading(false)
      })
    return () => ctrl.abort()
  }, [anchor.object_ref, anchor.catalog_source])

  useEffect(() => () => {
    for (const c of expandCtrls.current) c.abort()
  }, [])

  // The anchor's own units: its table (already expanded by the initial depth-1 fetch) and,
  // when the hit is a column, the column row to highlight as the match.
  const anchorUnitId = `${anchor.catalog_source}:public.${anchor.table}`
  const matchId = anchor.column ? `${anchor.catalog_source}:${anchor.object_ref}` : null

  const byId = useMemo(
    () => new Map((graph?.nodes ?? []).map(n => [n.id, n] as const)),
    [graph],
  )

  // Column -> owning table unit (columns render as rows inside their table card).
  const unitOf = useMemo(() => {
    const tableByKey = new Map<string, string>()
    for (const n of graph?.nodes ?? []) {
      if (n.kind === 'table') tableByKey.set(`${n.catalog_source}|${n.table}`, n.id)
    }
    return (id: string): string => {
      const n = byId.get(id)
      if (n?.kind === 'column' && n.resolved) {
        return tableByKey.get(`${n.catalog_source}|${n.table}`) ?? id
      }
      return id
    }
  }, [graph, byId])

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
    const queue = [anchorUnitId]
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
  }, [drawnEdges, unitOf, anchorUnitId])

  const visibleEdges = useMemo(
    () => drawnEdges.filter(e => visibleUnits.has(unitOf(e.from)) && visibleUnits.has(unitOf(e.to))),
    [drawnEdges, visibleUnits, unitOf],
  )

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
    if (!graph) return { placed: [] as PlacedNode[] }
    const columnsOf = new Map<string, LineageNode[]>()
    for (const n of graph.nodes) {
      if (n.kind === 'column' && n.resolved) {
        const unit = unitOf(n.id)
        if (unit !== n.id) columnsOf.set(unit, [...(columnsOf.get(unit) ?? []), n])
      }
    }
    const placed: PlacedNode[] = []
    for (const n of graph.nodes) {
      if (!visibleUnits.has(n.id)) continue
      if (n.kind === 'table') {
        const cols = columnsOf.get(n.id) ?? []
        const h =
          HEAD_H +
          SRC_H +
          (n.stale ? NOTE_H : 0) +
          (collapsed.has(n.id) ? 0 : cols.length * ROW_H + PAD_H)
        placed.push({ node: n, type: 'lnTable', x: 0, y: 0, w: W_TABLE, h, cols })
      } else if (n.kind === 'column' && !n.resolved) {
        placed.push({ node: n, type: 'lnStub', x: 0, y: 0, w: W_TABLE, h: HEAD_H + 64, cols: [] })
      } else if (n.kind === 'feature') {
        placed.push({
          node: n, type: 'lnFeature', x: 0, y: 0, w: W_FEATURE, h: HEAD_H + SRC_H + PAD_H, cols: [],
        })
      } else if (n.kind === 'consumer') {
        placed.push({ node: n, type: 'lnConsumer', x: 0, y: 0, w: W_CONSUMER, h: HEAD_H + 30, cols: [] })
      }
    }

    const g = new dagre.graphlib.Graph()
    g.setGraph({ rankdir: 'LR', nodesep: 36, ranksep: 110, marginx: 24, marginy: 24 })
    g.setDefaultEdgeLabel(() => ({}))
    for (const p of placed) g.setNode(p.node.id, { width: p.w, height: p.h })
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
    return { placed }
  }, [graph, visibleEdges, visibleUnits, collapsed, unitOf])

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
            columns: p.cols,
            collapsed: collapsed.has(n.id),
            matchId,
            traceId,
            expandable:
              n.id !== anchorUnitId && !expandedUnits.has(n.id) && !exhausted.has(n.id),
            expanding: expanding.has(n.id),
            onToggle: toggleTable,
            onColumn: openColumn,
            onOpen: openNode,
            onExpand: expand,
          } satisfies TableData,
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
      let stroke = 'var(--ln-join)'
      let label: string
      if (e.kind === 'join') {
        label = e.resolved
          ? (e.cardinality ?? 'join')
          : [e.cardinality, 'declared'].filter(Boolean).join(' · ')
      } else if (e.kind === 'entity_bridge') {
        stroke = 'var(--warn)'
        const entity = byId.get(e.from)?.entity ?? byId.get(e.to)?.entity
        label = entity ? `entity: ${entity}` : 'entity'
      } else {
        stroke = 'var(--proposal)'
        label = e.kind
      }
      return {
        id: `${e.kind}|${e.from}|${e.to}`,
        source: sourceUnit,
        target: targetUnit,
        sourceHandle:
          e.from !== sourceUnit && !collapsed.has(sourceUnit) ? e.from : undefined,
        targetHandle: e.to !== targetUnit && !collapsed.has(targetUnit) ? e.to : undefined,
        label,
        className: isTrace ? 'ln-edge ln-edge--trace' : 'ln-edge',
        animated: false, // reduced-motion safe: no marching-ants edges anywhere
        style: isTrace
          ? { stroke: 'var(--accent)', strokeWidth: 3 }
          : {
              stroke,
              strokeWidth: 1.5,
              ...(e.resolved ? {} : { strokeDasharray: '6 5' }),
            },
      }
    })
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

  // Refit after the visible node set changes: an expansion merge (or a layer toggle) can leave
  // freshly placed nodes off-screen, and the `fitView` prop only fires on the first render.
  // duration 0 keeps it reduced-motion safe, matching the static (never-animated) edges.
  const nodeCount = flow.nodes.length
  useEffect(() => {
    rf.current?.fitView({ duration: 0 })
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

  const empty = flow.nodes.length <= 1 && flow.edges.length === 0

  return (
    <>
      <div className="ln-wrap">
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
          <Background gap={22} size={1} color="oklch(0.88 0.01 212)" />
          <Panel position="top-left">
            <fieldset className="ln-layers">
              <legend className="micro-label">Layers</legend>
              {(
                [
                  ['joins', 'Joins', 'var(--ln-join)'],
                  ['entity', 'Entity bridges', 'var(--warn)'],
                  ['features', 'Feature lineage', 'var(--proposal)'],
                ] as const
              ).map(([layer, title, swatch]) => (
                <label key={layer} className="ln-layer">
                  <input
                    type="checkbox"
                    checked={layersOn[layer]}
                    onChange={e => {
                      setLayersOn(prev => ({ ...prev, [layer]: e.target.checked }))
                    }}
                  />
                  <span className="ln-swatch" style={{ background: swatch }} aria-hidden="true" />
                  {title}
                </label>
              ))}
            </fieldset>
          </Panel>
          <Controls showInteractive={false} position="bottom-right" />
          <MiniMap
            position="bottom-left"
            pannable={false}
            zoomable={false}
            nodeColor={n => (n.id === anchorUnitId ? 'var(--accent)' : 'var(--line-strong)')}
            maskColor="oklch(0.955 0.009 215 / 0.65)"
          />
        </ReactFlow>
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
      </div>

      {empty && (
        <p className="hint" role="status">
          No lineage recorded around this column yet. Declared joins, entity bridges, and
          features appear here as their uploads and registrations arrive.
        </p>
      )}
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

      <section className="ln-a11y" aria-label="Edges as text">
        <h3 className="micro-label">Edges (accessible parallel list)</h3>
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
      <button type="button" className="ln-drawer-close" ref={closeRef} onClick={onClose}>
        Close
      </button>
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
            <p className="ln-drawer-note">
              Not currently vouched. Re-upload the {node.catalog_source} source to serve its
              facts.
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
            <p className="ln-drawer-note">
              Not currently vouched. Re-upload the {node.catalog_source} source to serve its
              facts.
            </p>
          )}
        </>
      )}
    </aside>
  )
}
