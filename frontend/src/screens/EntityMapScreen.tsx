import { useEffect, useState } from 'react'
import {
  ApiError,
  type EntityMap,
  type EntityMapEndpoint,
  type EntityMapLink,
  type EntityMapNode,
  getEntityMap,
} from '../api'
import type { Route } from '../nav'

// Entity Map v0 (ingestion-richness Task 3D): the READ-ONLY face of the ontology. Entities as
// nodes sized by how many columns carry them (read-scoped — a caller sees only what their roles
// allow), catalogs as groupings inside each node, and the AVAILABLE cross-catalog links as edges.
// The link set is served VERBATIM from the same governed reader the planner and governance use, so
// this screen can never disagree with them about which links exist.
//
// Honesty rules this screen owns:
//   * an empty map SAYS "no governed links yet" — never a blank canvas;
//   * a proposed link renders as proposed (the same usable-not-failure badge the asset sheet
//     uses) — never dimmed, never red;
//   * no percentage, no invented layout meaning: node size is column count, edge chips are the
//     reader's own status/strength, and everything else is a click-through to the surface that
//     owns it (search for a node, asset detail / governance for an edge).

type Navigate = (r: Route, params?: Record<string, string> | URLSearchParams) => void

// The column name — the ref's last segment; full refs stay in the title attribute.
function columnOf(ref: string): string {
  return ref.split('.').pop() ?? ref
}

// Node font scale by share of the biggest node: sqrt so a 100x count reads ~2x, not 100x. The
// numbers stay printed beside the name — size is a glance aid, never the datum.
function nodeScale(count: number, max: number): number {
  if (max <= 0 || count <= 0) return 1
  return 1 + 0.6 * Math.sqrt(count / max)
}

export function EntityMapScreen({ navigate }: { navigate: Navigate }) {
  const [map, setMap] = useState<EntityMap | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [forbidden, setForbidden] = useState(false)

  useEffect(() => {
    let live = true
    getEntityMap()
      .then(body => {
        if (live) setMap(body)
      })
      .catch((e: unknown) => {
        if (!live) return
        if (e instanceof ApiError && e.status === 403) setForbidden(true)
        else setError(e instanceof ApiError ? e.detail : String(e))
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
    }
  }, [])

  if (loading) {
    return (
      <section className="emap">
        <p role="status" className="hint">Reading the catalog’s entities and links…</p>
      </section>
    )
  }

  if (forbidden) {
    return (
      <section className="emap">
        <div className="callout callout--warn">
          <div className="callout-body">
            <p role="status">
              <strong>You don’t have access to the entity map.</strong> This view needs the{' '}
              <code>catalog:read</code> permission and this session’s roles don’t carry it.
            </p>
          </div>
        </div>
      </section>
    )
  }

  if (error || !map) {
    return (
      <section className="emap">
        <p role="alert" className="error">
          Could not load the entity map: {error || 'no payload returned'}
        </p>
      </section>
    )
  }

  const maxCount = Math.max(0, ...map.entities.map(n => n.column_count))

  return (
    <section className="emap">
      <p className="hint">
        Read-only. Every node and edge below is the catalog’s current governed answer — nothing on
        this screen changes it. Column counts respect your read scope.
      </p>

      <section className="panel">
        <h2>Entities</h2>
        {map.entities.length === 0 ? (
          <p className="hint">
            No entities in the graph yet — columns gain an entity through enrichment and the
            Semantics screen.
          </p>
        ) : (
          <ul className="emap-nodes" aria-label="Entities">
            {map.entities.map(node => (
              <EntityNode
                key={node.entity_id}
                node={node}
                scale={nodeScale(node.column_count, maxCount)}
                onOpen={() => navigate('search', { entity: node.entity_id })}
              />
            ))}
          </ul>
        )}
      </section>

      <section className="panel">
        <h2>Links</h2>
        {map.links.length === 0 ? (
          <div className="callout callout--accent">
            <div className="callout-body">
              <p role="status">
                <strong>No governed links yet.</strong> When ingestion derives a cross-catalog
                identifier link — confirmed or not — it appears here as an edge.
              </p>
              <p className="hint">
                Links are proposed by derivation and reviewed on Governance review; a proposed link
                is already usable.
              </p>
            </div>
          </div>
        ) : (
          <ul className="rows" aria-label="Cross-catalog links">
            {map.links.map(link => (
              <LinkRow key={link.candidate_id} link={link} navigate={navigate} />
            ))}
          </ul>
        )}
      </section>
    </section>
  )
}

function EntityNode({ node, scale, onOpen }: {
  node: EntityMapNode
  scale: number
  onOpen: () => void
}) {
  return (
    <li>
      <button
        type="button"
        className="emap-node"
        onClick={onOpen}
        title={`Search columns carrying ${node.entity_id}`}
      >
        <span className="emap-node-name" style={{ fontSize: `${scale}em` }}>
          {node.entity_id}
        </span>
        <span className="hint">
          {node.column_count} {node.column_count === 1 ? 'column' : 'columns'}
          {!node.registered && ' · not in the concept registry'}
        </span>
        {node.catalogs.length > 0 && (
          <span className="emap-node-catalogs">
            {node.catalogs.map(group => (
              <span
                key={group.catalog_source}
                className="badge"
                title={group.sample_refs.map(columnOf).join(', ')}
              >
                {group.catalog_source} · {group.column_count}
              </span>
            ))}
          </span>
        )}
      </button>
    </li>
  )
}

// One endpoint: "catalog · column", full refs in the tooltip. A click opens the asset detail
// (its Relationships section holds the same link with its evidence).
function EndpointRef({ endpoint, navigate }: { endpoint: EntityMapEndpoint; navigate: Navigate }) {
  const ref = endpoint.column_refs[0] ?? endpoint.table_ref
  return (
    <button
      type="button"
      className="btn btn--ghost mono"
      title={endpoint.column_refs.join(', ') || endpoint.table_ref}
      onClick={() => navigate('asset', {
        source: endpoint.catalog_source, object_ref: ref,
      })}
    >
      {endpoint.catalog_source} · {columnOf(ref)}
    </button>
  )
}

function LinkRow({ link, navigate }: { link: EntityMapLink; navigate: Navigate }) {
  const entity = link.left.entity_id ?? link.right.entity_id
  const namespace = link.left.namespace ?? link.right.namespace
  return (
    <li className="row q-item">
      <div className="q-head emap-edge">
        {entity && <span className="gj-kind">{entity}</span>}
        <EndpointRef endpoint={link.left} navigate={navigate} />
        <span aria-hidden="true">↔</span>
        <EndpointRef endpoint={link.right} navigate={navigate} />
        {/* proposed is usable output — the same badge vocabulary as the asset sheet, never a
            failure tone */}
        <span className={`badge ${link.status === 'confirmed' ? 'gj-verified' : 'gj-proposed'}`}>
          {link.status}
        </span>
        <span className="gj-score" title="Ranking strength from stored evidence — not a probability">
          strength {link.strength}
        </span>
        {namespace && (
          <span className="badge" title="Identifier namespace from the concept registry">
            namespace {namespace}
          </span>
        )}
      </div>
      {link.realizations.length > 0 && (
        <ul className="emap-realizations">
          {link.realizations.map(real => (
            <li
              key={`${real.from_table_ref}->${real.to_table_ref}`}
              className="hint"
            >
              <span className="mono">
                {real.from_catalog_source}.{real.from_table_ref} → {real.to_catalog_source}.
                {real.to_table_ref}
              </span>{' '}
              {real.sandbox_eligible ? 'sandbox-eligible' : 'not sandbox-eligible'}
              {' · '}
              {real.production_eligible ? 'production-eligible' : 'not production-eligible'}
              {' · '}{real.safety_status}
            </li>
          ))}
        </ul>
      )}
      <p className="hint">
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => navigate('governance', { source: link.left.catalog_source })}
        >
          Review on Governance
        </button>
      </p>
    </li>
  )
}
