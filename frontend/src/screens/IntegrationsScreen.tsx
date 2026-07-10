// Integrations: the two-tier connector home. Tier 1 is one OpenMetadata INSTANCE (one URL + one
// sealed bot token, referenced by env var); tier 2 is a per-service SYNC that binds one of the
// instance's DatabaseServices (optionally narrowed by database/schema) to one FeatureGen catalog
// source. This mirrors OpenMetadata's own model — hierarchy DatabaseService -> Database -> Schema
// -> Table, and one bot token sees every service — so the connection lives once per instance and
// each source is a sync under it.
//
// Secrets never cross this screen. The add/edit forms capture only the env-var REFERENCE
// (token_env); the create endpoint 422s any plaintext token field, and no response ever carries a
// token value. Cards render the reference plus a sealed/not-set chip — never a value.
//
// Discovery (the live list of services a token can see) is a convenience, not a dependency: the
// sync-create path never needs it, so when OpenMetadata is unreachable the services section says
// so and offers a retry, and the user can still add a sync by typing a service name.
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  ApiError,
  createIntegration,
  createSync,
  deleteIntegration,
  deleteSync,
  discoverServices,
  listIntegrations,
  listSyncs,
  patchIntegration,
  patchSync,
} from '../api'
import type { DiscoveredService, Integration, Sync, TableNaming } from '../api'
import { CalloutGlyph } from './IngestResultCallout'

const ERR_GLYPH = (
  <CalloutGlyph>
    <circle cx="8" cy="8" r="6.25" />
    <path d="m5.75 5.75 4.5 4.5m0-4.5-4.5 4.5" />
  </CalloutGlyph>
)

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err)
}

// Mirrors the server default: FEATUREGEN_OM_TOKEN__<NAME uppercased, non-alnum -> _>.
function defaultTokenEnv(name: string): string {
  return `FEATUREGEN_OM_TOKEN__${name.replace(/[^A-Za-z0-9]/g, '_').toUpperCase()}`
}

// A 2-letter monogram for the instance tile: the first two alnum characters, else "OM".
function monogram(name: string): string {
  const letters = name.replace(/[^A-Za-z0-9]/g, '')
  return (letters.slice(0, 2) || 'OM').toUpperCase()
}

// Tag-map overrides are a compact text field: "tag -> sensitivity" pairs (arrow, colon, or equals
// as the separator), comma- or newline-separated. A tag with no separator maps to '' (ignore).
function parseTagMap(text: string): Record<string, string> {
  const map: Record<string, string> = {}
  for (const part of text.split(/[\n,]+/)) {
    const entry = part.trim()
    if (!entry) continue
    const sep = entry.search(/->|→|[:=]/)
    if (sep === -1) {
      map[entry] = ''
      continue
    }
    const tag = entry.slice(0, sep).trim()
    const value = entry.slice(sep).replace(/^(->|→|[:=])/, '').trim()
    if (tag) map[tag] = value
  }
  return map
}

function formatTagMap(map: Record<string, string> | null | undefined): string {
  if (!map) return ''
  return Object.entries(map)
    .map(([tag, value]) => `${tag} → ${value}`)
    .join(', ')
}

// ---------------------------------------------------------------- screen

export function IntegrationsScreen() {
  const [integrations, setIntegrations] = useState<Integration[] | null>(null)
  const [listError, setListError] = useState('')
  const [adding, setAdding] = useState(false)

  useEffect(() => {
    let cancelled = false
    listIntegrations().then(
      list => {
        if (!cancelled) setIntegrations(list)
      },
      (err: unknown) => {
        if (cancelled) return
        setIntegrations([])
        setListError(errorDetail(err))
      },
    )
    return () => {
      cancelled = true
    }
  }, [])

  const onCreated = (created: Integration) => {
    setIntegrations(list => [...(list ?? []), created])
    setAdding(false)
  }
  const onUpdated = (updated: Integration) => {
    setIntegrations(list =>
      (list ?? []).map(i => (i.integration_id === updated.integration_id ? updated : i)),
    )
  }
  const onRemoved = (id: string) => {
    setIntegrations(list => (list ?? []).filter(i => i.integration_id !== id))
  }

  const empty = integrations !== null && integrations.length === 0

  return (
    <section>
      <div className="row-head">
        <h2>OpenMetadata instances</h2>
        <span className="tray-spacer" />
        {!adding && (
          <button type="button" className="btn btn--primary" onClick={() => setAdding(true)}>
            Add integration
          </button>
        )}
      </div>

      {listError && (
        <p className="error" role="alert">
          {listError}
        </p>
      )}

      {adding && (
        <AddIntegrationForm onSaved={onCreated} onCancel={() => setAdding(false)} />
      )}

      {integrations === null && <p className="hint">Loading integrations…</p>}

      {empty && !adding && (
        <div className="empty">
          <p>No integrations yet.</p>
          <p className="next">
            Add an OpenMetadata instance to discover its services and sync them into catalog
            sources. One URL and one bot token per instance; the token sees every service in it.
          </p>
        </div>
      )}

      {integrations?.map(integration => (
        <IntegrationCard
          key={integration.integration_id}
          integration={integration}
          onUpdated={onUpdated}
          onRemoved={onRemoved}
        />
      ))}
    </section>
  )
}

// ---------------------------------------------------------------- add / edit integration

const TOKEN_HELP =
  'The bot token stays sealed on the server: set the referenced environment variable where the ' +
  'API runs. No response ever carries the token. The OpenMetadata host must be on the deployment ' +
  'allowlist. Leave the variable blank to use the name-derived default.'

function AddIntegrationForm({
  onSaved,
  onCancel,
}: {
  onSaved: (i: Integration) => void
  onCancel: () => void
}) {
  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [tokenEnv, setTokenEnv] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (saving) return
    setSaving(true)
    setError('')
    try {
      const created = await createIntegration({
        name: name.trim(),
        base_url: baseUrl.trim(),
        ...(tokenEnv.trim() ? { token_env: tokenEnv.trim() } : {}),
      })
      onSaved(created)
    } catch (err) {
      setError(errorDetail(err))
    } finally {
      setSaving(false)
    }
  }

  const envPreview = tokenEnv.trim() || (name.trim() ? defaultTokenEnv(name.trim()) : null)

  return (
    <form className="integration-form" onSubmit={submit} aria-label="Add an OpenMetadata integration">
      <h3 style={{ margin: 0 }}>Add an OpenMetadata integration</h3>
      <div className="form-grid-3">
        <label>
          Name
          <input value={name} onChange={e => setName(e.target.value)} placeholder="Corporate OpenMetadata" required />
        </label>
        <label>
          OpenMetadata URL
          <input
            className="mono"
            value={baseUrl}
            onChange={e => setBaseUrl(e.target.value)}
            placeholder="https://openmetadata.bank.internal"
            required
          />
        </label>
        <label>
          Bot token environment variable
          <input
            className="mono"
            value={tokenEnv}
            onChange={e => setTokenEnv(e.target.value)}
            placeholder={name.trim() ? defaultTokenEnv(name.trim()) : 'FEATUREGEN_OM_TOKEN__<NAME>'}
          />
        </label>
      </div>
      <p className="hint" style={{ margin: 0 }}>
        {TOKEN_HELP}
        {envPreview && (
          <>
            {' '}
            This integration will read <span className="mono">{envPreview}</span>.
          </>
        )}
      </p>
      <div className="form-foot">
        <button type="submit" className="btn btn--primary" disabled={saving}>
          {saving ? 'Saving…' : 'Save integration'}
        </button>
        <button type="button" className="btn" onClick={onCancel}>
          Cancel
        </button>
      </div>
      {error && (
        <div className="callout callout--danger" role="alert">
          {ERR_GLYPH}
          <div className="callout-body">
            <p>
              <strong>The integration was not saved.</strong> {error}
            </p>
          </div>
        </div>
      )}
    </form>
  )
}

function EditIntegrationForm({
  integration,
  onSaved,
  onCancel,
}: {
  integration: Integration
  onSaved: (i: Integration) => void
  onCancel: () => void
}) {
  const [name, setName] = useState(integration.name)
  const [baseUrl, setBaseUrl] = useState(integration.base_url)
  const [tokenEnv, setTokenEnv] = useState(integration.token_env)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (saving) return
    setSaving(true)
    setError('')
    try {
      const updated = await patchIntegration(integration.integration_id, {
        name: name.trim(),
        base_url: baseUrl.trim(),
        token_env: tokenEnv.trim(),
      })
      onSaved(updated)
    } catch (err) {
      setError(errorDetail(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="integration-form" onSubmit={submit} aria-label={`Edit ${integration.name}`}>
      <div className="form-grid-3">
        <label>
          Name
          <input value={name} onChange={e => setName(e.target.value)} required />
        </label>
        <label>
          OpenMetadata URL
          <input className="mono" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} required />
        </label>
        <label>
          Bot token environment variable
          <input className="mono" value={tokenEnv} onChange={e => setTokenEnv(e.target.value)} />
        </label>
      </div>
      <div className="form-foot">
        <button type="submit" className="btn btn--primary" disabled={saving}>
          {saving ? 'Saving…' : 'Save changes'}
        </button>
        <button type="button" className="btn" onClick={onCancel}>
          Cancel
        </button>
      </div>
      {error && (
        <div className="callout callout--danger" role="alert">
          {ERR_GLYPH}
          <div className="callout-body">
            <p>
              <strong>The change was not saved.</strong> {error}
            </p>
          </div>
        </div>
      )}
    </form>
  )
}

// ---------------------------------------------------------------- one instance card

// What the sync form is bound to: a fixed discovered service, a typed service (OM-down fallback),
// or an existing sync being edited.
type SyncTarget = { serviceName: string; existing: Sync | null } | { typed: true }

function IntegrationCard({
  integration,
  onUpdated,
  onRemoved,
}: {
  integration: Integration
  onUpdated: (i: Integration) => void
  onRemoved: (id: string) => void
}) {
  const [services, setServices] = useState<DiscoveredService[] | null>(null)
  const [servicesError, setServicesError] = useState('')
  const [syncs, setSyncs] = useState<Sync[]>([])
  const [editing, setEditing] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [removeError, setRemoveError] = useState('')
  const [syncTarget, setSyncTarget] = useState<SyncTarget | null>(null)

  const id = integration.integration_id

  const loadServices = useCallback(() => {
    setServices(null)
    setServicesError('')
    discoverServices(id).then(
      setServices,
      (err: unknown) => {
        setServices([])
        setServicesError(errorDetail(err))
      },
    )
  }, [id])

  useEffect(() => {
    let cancelled = false
    // Syncs come from the database (reliable); services come live from OpenMetadata (may fail).
    listSyncs(id).then(
      list => {
        if (!cancelled) setSyncs(list)
      },
      () => {},
    )
    discoverServices(id).then(
      list => {
        if (!cancelled) setServices(list)
      },
      (err: unknown) => {
        if (cancelled) return
        setServices([])
        setServicesError(errorDetail(err))
      },
    )
    return () => {
      cancelled = true
    }
  }, [id])

  const syncByService = new Map(syncs.map(s => [s.service_name, s]))

  const onSyncSaved = (sync: Sync) => {
    setSyncs(list => {
      const others = list.filter(s => s.sync_id !== sync.sync_id)
      return [...others, sync]
    })
    // Reflect the new binding in the discovered list without another OM round trip.
    setServices(list =>
      (list ?? []).map(svc =>
        svc.service_name === sync.service_name
          ? { ...svc, synced: true, sync_id: sync.sync_id }
          : svc,
      ),
    )
    setSyncTarget(null)
  }

  const onSyncRemoved = (sync: Sync) => {
    setSyncs(list => list.filter(s => s.sync_id !== sync.sync_id))
    setServices(list =>
      (list ?? []).map(svc =>
        svc.service_name === sync.service_name ? { ...svc, synced: false, sync_id: null } : svc,
      ),
    )
    setSyncTarget(null)
  }

  async function remove() {
    if (removing) return
    setRemoving(true)
    setRemoveError('')
    try {
      await deleteIntegration(id)
      onRemoved(id)
    } catch (err) {
      setRemoveError(errorDetail(err))
      setRemoving(false)
    }
  }

  // Syncs whose service the live discovery did not return (service removed from OM, or discovery
  // is down): surfaced so a configured sync is never hidden.
  const discoveredNames = new Set((services ?? []).map(s => s.service_name))
  const orphanSyncs = syncs.filter(s => !discoveredNames.has(s.service_name))
  const syncedCount = syncs.length

  return (
    <div className="integration">
      <div className="integration-top">
        <div className="integration-ic" aria-hidden="true">
          {monogram(integration.name)}
        </div>
        <div className="integration-id">
          <div className="integration-nm">{integration.name}</div>
          <div className="integration-meta">
            {integration.base_url} · bot {integration.token_env}
          </div>
        </div>
        <span className="tray-spacer" />
        {integration.token_present ? (
          <span className="badge ok">token sealed</span>
        ) : (
          <span className="badge held">token not set</span>
        )}
        <div className="integration-acts">
          <button type="button" className="btn" onClick={() => setEditing(v => !v)}>
            Edit
          </button>
          <button type="button" className="btn btn--danger" disabled={removing} onClick={() => void remove()}>
            {removing ? 'Removing…' : 'Remove'}
          </button>
        </div>
      </div>

      {removeError && (
        <p className="error" role="alert" style={{ padding: '0 18px 12px' }}>
          {removeError}
        </p>
      )}

      {editing && (
        <div style={{ padding: '0 12px 8px' }}>
          <EditIntegrationForm
            integration={integration}
            onSaved={updated => {
              onUpdated(updated)
              setEditing(false)
            }}
            onCancel={() => setEditing(false)}
          />
        </div>
      )}

      <div className="services">
        {services === null && !servicesError ? (
          <p className="svc-head" role="status">
            Discovering services…
          </p>
        ) : servicesError ? (
          <div className="callout callout--warn" role="alert" style={{ margin: 4 }}>
            <CalloutGlyph>
              <path d="M8 2.75 14 13.25H2z" />
              <path d="M8 6.75v2.75M8 11.5v.01" />
            </CalloutGlyph>
            <div className="callout-body">
              <p>
                <strong>Could not reach OpenMetadata.</strong> {servicesError}
              </p>
              <p>
                You can still add a sync by typing a service name; discovery is only a convenience.
              </p>
              <button type="button" className="btn" onClick={loadServices}>
                Retry discovery
              </button>
            </div>
          </div>
        ) : (
          <div className="svc-head">
            Services this token can see · {services?.length ?? 0} ·{' '}
            <span style={{ textTransform: 'none', fontWeight: 400 }}>{syncedCount} synced</span>
          </div>
        )}

        {(services ?? []).map(svc => {
          const sync = syncByService.get(svc.service_name) ?? null
          return (
            <ServiceRow
              key={svc.service_name}
              service={svc}
              sync={sync}
              onAddSync={() => setSyncTarget({ serviceName: svc.service_name, existing: null })}
              onEditSync={() => setSyncTarget({ serviceName: svc.service_name, existing: sync })}
            />
          )
        })}

        {orphanSyncs.map(sync => (
          <div className="svc" key={sync.sync_id}>
            <div>
              <div className="svc-name">{sync.service_name}</div>
              <div className="svc-kind">not in the live service list</div>
            </div>
            <span className="tray-spacer" />
            <span className="svc-map">→ source {sync.target_source}</span>
            <span className="badge ok">synced</span>
            <button
              type="button"
              className="btn"
              onClick={() => setSyncTarget({ serviceName: sync.service_name, existing: sync })}
            >
              Edit sync
            </button>
          </div>
        ))}

        {servicesError && (
          <div className="svc-head">
            <button
              type="button"
              className="btn"
              onClick={() => setSyncTarget({ typed: true })}
            >
              Add sync by service name
            </button>
          </div>
        )}

        {syncTarget && (
          <SyncForm
            integrationId={id}
            integrationName={integration.name}
            serviceNameFixed={'typed' in syncTarget ? undefined : syncTarget.serviceName}
            existing={'typed' in syncTarget ? null : syncTarget.existing}
            onSaved={onSyncSaved}
            onRemoved={onSyncRemoved}
            onCancel={() => setSyncTarget(null)}
          />
        )}
      </div>
    </div>
  )
}

function ServiceRow({
  service,
  sync,
  onAddSync,
  onEditSync,
}: {
  service: DiscoveredService
  sync: Sync | null
  onAddSync: () => void
  onEditSync: () => void
}) {
  const synced = service.synced && sync !== null
  return (
    <div className="svc">
      <div>
        <div className="svc-name">{service.service_name}</div>
        <div className="svc-kind">{service.service_type || 'service'}</div>
      </div>
      <span className="tray-spacer" />
      {synced ? (
        <>
          <span className="svc-map">→ source {sync.target_source}</span>
          <span className="badge ok">synced</span>
          <button type="button" className="btn" onClick={onEditSync}>
            Edit sync
          </button>
        </>
      ) : (
        <>
          <span className="badge">not synced</span>
          <button type="button" className="btn btn--primary" onClick={onAddSync}>
            Add sync
          </button>
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- add / edit a sync

function SyncForm({
  integrationId,
  integrationName,
  serviceNameFixed,
  existing,
  onSaved,
  onRemoved,
  onCancel,
}: {
  integrationId: string
  integrationName: string
  serviceNameFixed?: string
  existing: Sync | null
  onSaved: (s: Sync) => void
  onRemoved: (s: Sync) => void
  onCancel: () => void
}) {
  const [serviceName, setServiceName] = useState(
    existing?.service_name ?? serviceNameFixed ?? '',
  )
  const [targetSource, setTargetSource] = useState(existing?.target_source ?? '')
  const [databaseFilter, setDatabaseFilter] = useState(existing?.database_filter ?? '')
  const [schemaFilter, setSchemaFilter] = useState(existing?.schema_filter ?? '')
  const [tagMapText, setTagMapText] = useState(formatTagMap(existing?.tag_map_override))
  const [tableNaming, setTableNaming] = useState<TableNaming>(existing?.table_naming ?? 'table')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const heading = existing
    ? `Edit sync for ${existing.service_name}`
    : serviceNameFixed
      ? `Sync ${serviceNameFixed} into the catalog`
      : 'Add a sync'

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (saving) return
    setSaving(true)
    setError('')
    const override = tagMapText.trim() ? parseTagMap(tagMapText) : null
    const spec = {
      service_name: serviceName.trim(),
      target_source: targetSource.trim(),
      database_filter: databaseFilter.trim() || null,
      schema_filter: schemaFilter.trim() || null,
      tag_map_override: override,
      table_naming: tableNaming,
    }
    try {
      const saved = existing
        ? await patchSync(integrationId, existing.sync_id, spec)
        : await createSync(integrationId, spec)
      onSaved(saved)
    } catch (err) {
      setError(errorDetail(err))
    } finally {
      setSaving(false)
    }
  }

  async function remove() {
    if (!existing || saving) return
    setSaving(true)
    setError('')
    try {
      await deleteSync(integrationId, existing.sync_id)
      onRemoved(existing)
    } catch (err) {
      setError(errorDetail(err))
      setSaving(false)
    }
  }

  return (
    <form className="sync-form" onSubmit={submit} aria-label={heading}>
      <div>
        <h3 style={{ margin: 0 }}>{heading}</h3>
        <p className="hint" style={{ margin: '4px 0 0' }}>
          {integrationName}
          {serviceNameFixed || existing ? ` · ${serviceNameFixed ?? existing?.service_name}` : ''}
        </p>
      </div>
      <div className="form-grid-3">
        {serviceNameFixed || existing ? (
          <label>
            Service
            <input className="mono" value={serviceName} readOnly aria-readonly="true" />
          </label>
        ) : (
          <label>
            Service name
            <input
              className="mono"
              value={serviceName}
              onChange={e => setServiceName(e.target.value)}
              placeholder="snowflake_dwh"
              required
            />
          </label>
        )}
        <label>
          Target catalog source
          <input
            value={targetSource}
            onChange={e => setTargetSource(e.target.value)}
            placeholder="deposits_om"
            required
          />
        </label>
        <label>
          Table naming
          <select value={tableNaming} onChange={e => setTableNaming(e.target.value as TableNaming)}>
            <option value="table">table name only</option>
            <option value="schema_table">schema_table prefix</option>
          </select>
        </label>
      </div>
      <div className="form-grid-3">
        <label>
          Database filter (optional)
          <input className="mono" value={databaseFilter} onChange={e => setDatabaseFilter(e.target.value)} placeholder="ecommerce" />
        </label>
        <label>
          Schema filter (optional)
          <input className="mono" value={schemaFilter} onChange={e => setSchemaFilter(e.target.value)} placeholder="public" />
        </label>
        <label>
          Tag map override (optional)
          <input
            className="mono"
            value={tagMapText}
            onChange={e => setTagMapText(e.target.value)}
            placeholder="Confidential.Internal → restricted"
          />
        </label>
      </div>
      <p className="hint" style={{ margin: 0 }}>
        A sync maps one service (optionally narrowed to a database or schema) to one catalog source.
        The tag map override wins per tag over the integration&#39;s map; leave it blank to inherit.
        Nothing here contacts OpenMetadata until you preview an import in Ingest.
      </p>
      <div className="form-foot">
        <button type="submit" className="btn btn--primary" disabled={saving}>
          {saving ? 'Saving…' : 'Save sync'}
        </button>
        <button type="button" className="btn" onClick={onCancel}>
          Cancel
        </button>
        {existing && (
          <button type="button" className="btn btn--danger" disabled={saving} onClick={() => void remove()}>
            Remove sync
          </button>
        )}
      </div>
      {error && (
        <div className="callout callout--danger" role="alert">
          {ERR_GLYPH}
          <div className="callout-body">
            <p>
              <strong>The sync was not saved.</strong> {error}
            </p>
          </div>
        </div>
      )}
    </form>
  )
}
