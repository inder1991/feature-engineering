import { useEffect, useState } from 'react'
import {
  ApiError,
  type CatalogEngine,
  type DataSourceConnection,
  type DataSourceConnections,
  getCatalogEngines,
  getDataSourceConnections,
  putCatalogEngine,
  putDataSourceConnection,
} from '../api'

// Where each catalog's DATA lives — distinct from the OpenMetadata integrations above it, which are
// where the catalog DESCRIPTION comes from. Four rules the layout follows:
//
//   * UNROUTED CATALOGS COME FIRST. A list of what is configured hides exactly the thing an operator
//     opened this screen to fix. An undeclared catalog is the reason a question says it cannot run.
//   * ONE CATALOG IS ONE ENGINE, and one route serves every catalog on it. So a bank with an EDP
//     Hive and an ODS Oracle configures two rows here, not one per table.
//   * THE ENVIRONMENT IS SHOWN, always. A route belonging to another environment is visible and
//     inert, and saying so beats leaving someone to deduce it from a gap message.
//   * NO SECRET IS TYPED HERE. The field takes a reference into a secret manager; the server refuses
//     anything else, and the form says so before you find out.

const SECRET_SCHEMES = ['vault://', 'env://', 'aws-secrets://', 'azure-kv://', 'gcp-sm://']

function looksLikeReference(value: string): boolean {
  return SECRET_SCHEMES.some(scheme => value.startsWith(scheme))
}

function detail(err: unknown): string {
  if (err instanceof ApiError) {
    return err.status === 403
      ? 'Changing data sources needs the platform-admin role. You can see them, not edit them.'
      : err.detail
  }
  return String(err)
}

function CatalogRow(
  { row, engines, onDeclare, busy }:
  { row: CatalogEngine; engines: string[]; busy: boolean
    onDeclare: (source: string, engine: string, tier: string) => void },
) {
  const [engine, setEngine] = useState(row.engine ?? '')
  const [tier, setTier] = useState(row.tier ?? '')
  const routed = Boolean(row.engine)
  return (
    <li className={routed ? 'ds-catalog routed' : 'ds-catalog unrouted'}>
      <div className="ds-catalog-head">
        <span className="ds-catalog-name">{row.catalog_source}</span>
        <span className="ds-catalog-state">
          {routed ? `${row.engine} · ${row.tier}` : 'not routed — questions on this catalog cannot run'}
        </span>
      </div>
      <div className="ds-catalog-form">
        <label>
          Engine
          <select value={engine} onChange={e => setEngine(e.target.value)}>
            <option value="">choose…</option>
            {engines.map(e => <option key={e} value={e}>{e}</option>)}
          </select>
        </label>
        <label>
          Tier
          {/* Free text on purpose: edp/ods is the bank's taxonomy, not this tool's, and a closed
              list rejects a correct value the day someone adds one. */}
          <input value={tier} placeholder="edp" onChange={e => setTier(e.target.value)} />
        </label>
        <button
          type="button"
          disabled={busy || engine === '' || tier.trim() === ''}
          onClick={() => onDeclare(row.catalog_source, engine, tier.trim())}
        >
          {routed ? 'Update' : 'Declare'}
        </button>
      </div>
      {row.declared_by && <p className="ds-declared-by">Declared by {row.declared_by}</p>}
    </li>
  )
}

function ConnectionRow({ row }: { row: DataSourceConnection }) {
  return (
    <li className={row.usable_here ? 'ds-conn' : 'ds-conn inert'}>
      <div className="ds-conn-head">
        <span className="ds-conn-route">{row.engine} · {row.tier}</span>
        <span className="ds-conn-id">{row.connection_id}</span>
        {!row.usable_here && (
          <span className="ds-conn-inert-tag">
            {row.active ? `${row.environment} — not this environment` : 'switched off'}
          </span>
        )}
      </div>
      <dl className="ds-conn-fields">
        <dt>Host</dt><dd>{row.host}:{row.port}</dd>
        <dt>Reads as</dt><dd>{row.execution_principal}</dd>
        <dt>Auth</dt><dd>{row.auth_mechanism}</dd>
        <dt>Schemas</dt>
        <dd>{row.allowed_schemas.length ? row.allowed_schemas.join(', ') : 'none — authorizes nothing'}</dd>
        <dt>Secret</dt><dd className="ds-secret">{row.secret_ref}</dd>
      </dl>
    </li>
  )
}

function AddConnectionForm(
  { engines, environment, onAdd, busy }:
  { engines: string[]; environment: string; busy: boolean
    onAdd: (body: Omit<DataSourceConnection, 'environment' | 'usable_here'>) => void },
) {
  const [f, setF] = useState({
    connection_id: '', engine: '', tier: '', host: '', port: '10000',
    auth_mechanism: 'kerberos', secret_ref: '', execution_principal: '',
    allowed_schemas: '', database_name: '',
  })
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) =>
    setF(prev => ({ ...prev, [k]: e.target.value }))
  const secretOk = f.secret_ref === '' || looksLikeReference(f.secret_ref)
  const ready = f.connection_id && f.engine && f.tier && f.host && f.execution_principal
    && f.secret_ref && secretOk

  return (
    <form
      className="ds-add"
      onSubmit={e => {
        e.preventDefault()
        onAdd({
          connection_id: f.connection_id.trim(), engine: f.engine, tier: f.tier.trim(),
          host: f.host.trim(), port: Number(f.port) || 0, auth_mechanism: f.auth_mechanism.trim(),
          secret_ref: f.secret_ref.trim(), execution_principal: f.execution_principal.trim(),
          allowed_schemas: f.allowed_schemas.split(',').map(s => s.trim()).filter(Boolean),
          database_name: f.database_name.trim(), active: true,
        })
      }}
    >
      <h4>Add a route</h4>
      <p className="ds-add-note">
        One route serves every catalog on that engine and tier. It will belong to{' '}
        <strong>{environment}</strong> — the environment this deployment is, not a field you set.
      </p>
      <div className="ds-add-grid">
        <label>Name<input value={f.connection_id} onChange={set('connection_id')} /></label>
        <label>
          Engine
          <select value={f.engine} onChange={set('engine')}>
            <option value="">choose…</option>
            {engines.map(e => <option key={e} value={e}>{e}</option>)}
          </select>
        </label>
        <label>Tier<input value={f.tier} placeholder="edp" onChange={set('tier')} /></label>
        <label>Host<input value={f.host} onChange={set('host')} /></label>
        <label>Port<input value={f.port} onChange={set('port')} /></label>
        <label>Auth<input value={f.auth_mechanism} onChange={set('auth_mechanism')} /></label>
        <label>
          Reads as
          <input value={f.execution_principal} placeholder="svc_ro"
                 onChange={set('execution_principal')} />
        </label>
        <label>
          Instance
          <input value={f.database_name} placeholder="cluster or service"
                 onChange={set('database_name')} />
        </label>
        <label className="ds-wide">
          Schemas it may read
          <input value={f.allowed_schemas} placeholder="DPL_EIB_COMPLIANCE, BO_DPL_CIB"
                 onChange={set('allowed_schemas')} />
        </label>
        <label className="ds-wide">
          Secret reference
          <input value={f.secret_ref} placeholder="vault://featuregen/edp-hive"
                 onChange={set('secret_ref')} />
          <span className={secretOk ? 'ds-hint' : 'ds-hint bad'}>
            {secretOk
              ? 'A pointer into your secret manager. The credential is never stored here.'
              : `Must start with ${SECRET_SCHEMES.join(', ')} — never the secret itself.`}
          </span>
        </label>
      </div>
      <button type="submit" disabled={busy || !ready}>Add route</button>
    </form>
  )
}

export function DataSourcesPanel() {
  const [conns, setConns] = useState<DataSourceConnections | null>(null)
  const [catalogs, setCatalogs] = useState<CatalogEngine[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      const [c, k] = await Promise.all([getDataSourceConnections(), getCatalogEngines()])
      setConns(c)
      setCatalogs(k.catalogs)
    } catch (e) {
      setError(detail(e))
    }
  }

  useEffect(() => { void load() }, [])

  async function mutate(fn: () => Promise<unknown>) {
    setBusy(true)
    setError('')
    try {
      await fn()
      await load()
    } catch (e) {
      setError(detail(e))
    } finally {
      setBusy(false)
    }
  }

  // Unrouted first: an operator opened this screen because something could not run.
  const ordered = [...catalogs].sort((a, b) => Number(Boolean(a.engine)) - Number(Boolean(b.engine)))

  return (
    <section className="ds-panel" aria-labelledby="ds-h">
      <h2 id="ds-h">Data sources</h2>
      <p className="ds-lede">
        Where each catalog&apos;s data actually lives. The integrations above describe the catalog;
        these say which warehouse to read and who may read it. This deployment is{' '}
        <strong>{conns?.environment ?? '…'}</strong>.
      </p>

      {error && <p className="ds-error" role="alert">{error}</p>}

      <h3>Catalogs</h3>
      <ul className="ds-catalogs">
        {ordered.map(row => (
          <CatalogRow
            key={row.catalog_source}
            row={row}
            engines={conns?.engines ?? []}
            busy={busy}
            onDeclare={(source, engine, tier) =>
              void mutate(() => putCatalogEngine(source, engine, tier))}
          />
        ))}
        {ordered.length === 0 && <li className="ds-empty">No catalog has been uploaded yet.</li>}
      </ul>

      <h3>Routes</h3>
      <ul className="ds-conns">
        {(conns?.connections ?? []).map(row => (
          <ConnectionRow key={row.connection_id} row={row} />
        ))}
        {conns?.connections.length === 0 && (
          <li className="ds-empty">
            No route configured. A declared catalog still cannot be read until one exists.
          </li>
        )}
      </ul>

      <AddConnectionForm
        engines={conns?.engines ?? []}
        environment={conns?.environment ?? ''}
        busy={busy}
        onAdd={body => void mutate(() => putDataSourceConnection(body))}
      />
    </section>
  )
}
