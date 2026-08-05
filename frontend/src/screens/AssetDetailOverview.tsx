import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import {
  ApiError,
  type AssetDetail,
  type AssetIdentity,
  type EffectiveMetadataField,
  type EffectiveMetadataSection,
  type FeatureSuggestionPageV2,
  type RoleUsability,
  SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION,
  getTableSuggestionsV2,
} from '../api'
import { useIdentityKey } from '../session'
import { SuggestionCard } from './SuggestionCard'
import { AuthorityBadge } from './AuthorityBadge'
import { fieldValueText, typeDisplay } from './assetDetailFields'

// The Overview tab of the asset dossier, in three tiers.
//
//   VERDICT   a strip of four counts — what shape is this asset in?
//   REASONING the card grid — who said so, and what is still unknown?
//   RECEIPTS  a collapsed disclosure — the object/logical/graph refs and response state.
//
// Before this split every section was tier 2: seven identical `.adg-section` blocks carrying equal
// weight, with the technical identity refs sitting FIRST. A reader had to consume the whole page to
// learn whether the column was usable. The tiers exist so the first question is answered in one
// glance and the refs are reachable without being in the way.

// ---- card primitive --------------------------------------------------------------------------
// Head carries a title AND a one-line rationale. In a product whose value is knowing who asserted
// what, that rationale IS the feature: it tells the reader which authority governs the card before
// they read a single value. `aside` holds a count badge or a jump action.
function DossierCard({
  title,
  subtitle,
  aside,
  full,
  testId,
  children,
}: {
  title: string
  subtitle: string
  aside?: ReactNode
  full?: boolean
  testId?: string
  children: ReactNode
}) {
  return (
    <article className={`adg-card${full ? ' adg-card--full' : ''}`} data-testid={testId}>
      <div className="adg-card-head">
        <div>
          <h3>{title}</h3>
          <p>{subtitle}</p>
        </div>
        {aside}
      </div>
      <div className="adg-card-body">{children}</div>
    </article>
  )
}

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
function useColumnSuggestions(source: string, identity: AssetIdentity) {
  // A table asset has `table` set too, but suggestions bind COLUMN operands, so the match set is
  // empty by construction. Gate on the anchor being a column: otherwise the table page pays for a
  // request it cannot use and reports "0 · use this column" on a page that has no column.
  const table = identity.kind === 'column' ? (identity.table ?? '') : ''
  const objectRef = identity.object_ref
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

// ---- tier 1: the verdict strip ---------------------------------------------------------------

// A count with the sentence that stops it being read as a verdict on its own. `0 relationships` is
// a failure; `0 · the parent table still provides context` is a fact. The standing rule that this
// UI never frames an absent or unreviewed value as failure lives in these qualifiers.
function Stat({
  value,
  label,
  qualifier,
  tone,
}: {
  value: string
  label: string
  qualifier: string
  tone?: 'ok' | 'warn'
}) {
  return (
    <div className="stat adg-stat">
      <b className={tone ? `tone-${tone}` : undefined}>{value}</b>
      <span className="adg-stat-label">{label}</span>
      <small>{qualifier}</small>
    </div>
  )
}

// "a", "a and b", "a, b and c" — a bare join(' and ') reads as "a and b and c".
function listWords(items: string[]): string {
  if (items.length <= 1) return items[0] ?? ''
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`
}

function SummaryStrip({
  detail,
  matching,
  page,
  isColumn,
}: {
  detail: AssetDetail
  matching: unknown[]
  page: FeatureSuggestionPageV2 | null
  isColumn: boolean
}) {
  const usability = detail.readiness?.usability
  const evidence = detail.evidence
  const rel = detail.relationships
  const decided = evidence ? Object.keys(evidence.latest_decision_by_field).length : null
  // Direct relationships are governed edges to OTHER assets. Containment is structural context, not
  // a relationship, so it is named in the qualifier instead of inflating the count.
  const direct = rel ? rel.approved_joins.length + rel.cross_catalog.length : null
  const needChecks = (usability?.roles ?? [])
    .filter(r => r.state === 'needs_data_check')
    .map(r => r.label.toLowerCase())
  const checksQualifier = needChecks.length === 0
    ? 'No role is waiting on a data check'
    : `${listWords(needChecks)} ${needChecks.length === 1 ? 'needs' : 'need'} a data check`

  return (
    <section className="stats adg-stats" aria-label="Asset decision summary">
      {usability && (
        <Stat
          value={`${usability.usable_roles} of ${usability.total_roles}`}
          label="Potential uses"
          tone={usability.usable_roles === usability.total_roles ? 'ok' : undefined}
          qualifier={checksQualifier}
        />
      )}
      {isColumn && (
        // Rendered while the read is still in flight so the strip does not grow from three tiles to
        // four under the reader's eye. Vocabulary matches the section below: suggested, not
        // recommended.
        <Stat
          value={page ? String(matching.length) : '—'}
          label="Suggested features"
          qualifier={page
            ? `Use this column · ${page.collection.summary.suggested} available for the table`
            : 'Reading the table\u2019s suggestions\u2026'}
        />
      )}
      {decided !== null && (
        <Stat
          value={String(decided)}
          label="Field decisions"
          qualifier={decided === 0
            ? 'Nothing governed yet · evidence lives in Metadata & evidence'
            : 'Evidence and corrections live in Metadata & evidence'}
        />
      )}
      {direct !== null && (
        <Stat
          value={String(direct)}
          label="Direct relationships"
          qualifier={direct === 0
            ? 'The parent table still provides context'
            : 'Verified joins and cross-catalog links'}
        />
      )}
    </section>
  )
}

// ---- tier 2: meaning -------------------------------------------------------------------------
// The source definition and the AI summary, SIDE BY SIDE — the summary never replaces the
// definition, and it is labelled as AI-drafted so nobody mistakes a synthesis for the source.
function MeaningCard({
  metadata,
  isUnavailable,
}: {
  metadata: EffectiveMetadataSection | undefined
  isUnavailable: (name: string) => boolean
}) {
  if (isUnavailable('effective_metadata') || !metadata) return null
  const definition = metadata.fields.definition
  const summary = metadata.fields.ai_summary
  if (!definition && !summary) return null
  return (
    <DossierCard
      full
      testId="meaning"
      title="Business meaning"
      subtitle="The source definition and the AI interpretation stay visibly distinct."
    >
      <div className="adg-meaning">
        <div className="adg-meaning-col" data-testid="meaning-definition">
          <p className="micro-label adg-sub">Definition</p>
          {definition?.value != null ? (
            <>
              <p className="adg-meaning-text">{definition.value}</p>
              <AuthorityBadge field={definition} />
            </>
          ) : (
            <p className="hint">No definition from the source yet.</p>
          )}
        </div>
        <div className="adg-meaning-col" data-testid="meaning-summary">
          <p className="micro-label adg-sub">
            AI summary <span className="badge gj-proposed">AI-drafted</span>
          </p>
          {summary?.value != null ? (
            <p className="adg-meaning-text">{summary.value}</p>
          ) : (
            <p className="hint">No AI summary yet.</p>
          )}
        </div>
      </div>
    </DossierCard>
  )
}

// ---- tier 2: source glossary -----------------------------------------------------------------
// What the source FILE itself asserted, in product words, each value with its source provenance
// chip. An asset whose upload declared none of these shows NO card (nothing is fabricated); the
// declared SQL type joins the list because the file declared it too.
const GLOSSARY_FIELDS: readonly [string, string][] = [
  ['business_term', 'Business term'],
  ['term_type', 'Term type'],
  ['process_path', 'Business processes'],
  ['related_terms', 'Related terms'],
  ['bian_path', 'BIAN classification'],
  ['fibo_path', 'FIBO classification'],
  ['physical_fqn', 'Physical path'],
]

// The L1 → L2 → L3 process path, one line. The upload stores " > "-joined levels; render with
// arrows so it reads as the path it is.
function processPathText(value: string): string {
  return value.split('>').map(part => part.trim()).filter(Boolean).join(' → ')
}

// A classification path renders as its own segments rather than one long string, so the hierarchy
// is legible at a glance instead of being parsed out of punctuation by the reader.
function PathValue({ value }: { value: string }) {
  const parts = value.split('>').map(p => p.trim()).filter(Boolean)
  if (parts.length < 2) return <>{value}</>
  return (
    <span className="adg-path">
      {parts.map((part, i) => (
        <span key={`${part}:${i}`}>
          {i > 0 && <i aria-hidden="true">→</i>}
          <em>{part}</em>
        </span>
      ))}
    </span>
  )
}

function SourceGlossaryCard({ detail }: { detail: AssetDetail }) {
  const fields = detail.source_glossary?.fields ?? {}
  const declaredType = detail.identity.declared_type
  const rows = GLOSSARY_FIELDS.filter(([key]) => fields[key])
  if (rows.length === 0 && !declaredType) return null
  return (
    <DossierCard
      full
      testId="source-glossary"
      title="From the source glossary"
      subtitle="Business vocabulary supplied by the catalog source, not labels inferred by this UI."
    >
      <ul className="rows adg-fieldsum">
        {rows.map(([key, label]) => (
          <li className="row adg-field" key={key}>
            <span className="adg-field-label">{label}</span>
            <span className={`adg-field-value ${key === 'physical_fqn' ? 'mono' : ''}`}>
              {key === 'bian_path' || key === 'fibo_path'
                ? <PathValue value={fields[key].value} />
                : key === 'process_path'
                  ? processPathText(fields[key].value)
                  : fields[key].value}
            </span>
            <span className="badge gj-verified">{fields[key].provenance}</span>
          </li>
        ))}
        {declaredType && (
          <li className="row adg-field" key="declared_type">
            <span className="adg-field-label">Declared type</span>
            <span className="adg-field-value mono">{declaredType}</span>
            <span className="badge gj-verified">source declared</span>
          </li>
        )}
      </ul>
    </DossierCard>
  )
}

// ---- tier 2: operational semantics -----------------------------------------------------------
// The dossier's semantics axes, in product words. `type` lives in the hero and the technical
// disclosure; `definition`/`ai_summary` live in Meaning.
const AXIS_FIELDS: readonly [string, string][] = [
  ['concept', 'Business concept'],
  ['domain', 'Data domain'],
  ['additivity', 'Aggregation behavior'],
  ['unit', 'Unit'],
  ['currency', 'Currency'],
  ['entity', 'Entity'],
  ['sensitivity_display', 'Sensitivity'],
  ['party_role', 'Party role'],
]

function axisIsKnown(field: EffectiveMetadataField | undefined): boolean {
  return !!field && (field.value != null || field.proposed_value != null)
}

function AxisRow({
  name,
  label,
  field,
}: {
  name: string
  label: string
  field: EffectiveMetadataField | undefined
}) {
  return (
    <li className="row adg-field adg-axis" data-testid={`axis-${name}`}>
      <span className="adg-field-label">{label}</span>
      {axisIsKnown(field) && field ? (
        <>
          <span className="adg-field-value mono">{fieldValueText(field)}</span>
          <AuthorityBadge field={field} />
        </>
      ) : (
        // Explicit, quiet, and distinguishable from a hidden axis: nothing is known — no governed
        // value, no proposal from anyone. Not an error, not an omission.
        <>
          <span className="adg-field-value hint">nothing known yet</span>
          <span className="badge gj-none">not set</span>
        </>
      )}
    </li>
  )
}

function SemanticsCard({
  metadata,
  isUnavailable,
  isColumn,
}: {
  metadata: EffectiveMetadataSection | undefined
  isUnavailable: (name: string) => boolean
  isColumn: boolean
}) {
  const unavailable = isUnavailable('effective_metadata')
  // The axes are per-COLUMN. A table anchor carries `{fields:{}, note:…}`, so listing all eight as
  // unknown would assert a vacancy that cannot exist rather than reporting one that does.
  const axesApply = isColumn && !unavailable
  // EVERY axis renders whether or not the response carried it. Filtering to present keys made a
  // server that omits an axis indistinguishable from an axis nobody has an opinion on — the one
  // thing this list may not say is nothing at all.
  const known = axesApply
    ? AXIS_FIELDS.filter(([name]) => axisIsKnown(metadata?.fields[name])).length
    : 0
  const unknown = AXIS_FIELDS.length - known
  return (
    <DossierCard
      full
      title="Operational semantics"
      subtitle="Every supported axis is shown; “not known” is different from hidden."
      aside={axesApply && (
        <span className="badge gj-proposed adg-count">
          {known} populated · {unknown} unknown
        </span>
      )}
    >
      {unavailable ? (
        <p className="adg-unavailable" role="status">Not available to your roles.</p>
      ) : !axesApply ? (
        <p className="hint">{metadata?.note ?? 'No per-field metadata on this asset.'}</p>
      ) : (
        <ul className="rows adg-fieldsum adg-axes" data-testid="attested-metadata">
          {AXIS_FIELDS.map(([name, label]) => (
            <AxisRow key={name} name={name} label={label} field={metadata?.fields[name]} />
          ))}
        </ul>
      )}
    </DossierCard>
  )
}

// ---- tier 2: capabilities --------------------------------------------------------------------
// Tone by STATE, never by "is something outstanding". `ai_proposed` is a normal, usable state — the
// most common one in a freshly-ingested catalog — so it must not read as a warning.
const USABILITY_TONE: Record<string, string> = {
  confirmed: 'gj-verified',
  ai_proposed: 'gj-proposed',
  needs_data_check: 'gj-partial',
  not_set: 'gj-none',
  not_considered: 'gj-none',
  not_suitable: 'gj-none',
  unavailable: 'gj-none',
}

// Lifted onto Overview because it answers the question the page exists for — can I use this
// column? — without a tab change. The evidence ids and the run-a-check actions stay on Readiness;
// this is the verdict and the reason, nothing more.
//
// The jump moves FOCUS as well as the tab: this button unmounts with the Overview panel, so leaving
// focus where it was drops a keyboard or screen-reader user onto <body> with no place in the page.
function CapabilitiesCard({
  roles,
  onOpenReadiness,
}: {
  roles: RoleUsability[]
  onOpenReadiness: () => void
}) {
  return (
    <DossierCard
      title="What can the system use it for?"
      subtitle="Role verdicts, each with the evidence still required."
      aside={
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => {
            onOpenReadiness()
            // After the tab swap the Overview panel is gone; put the caret on the tab that now owns
            // the content rather than letting it fall to the document body.
            requestAnimationFrame(() => {
              const tab = document.querySelector<HTMLButtonElement>(
                '[aria-label="Asset sections"] button[aria-pressed="true"]')
              tab?.focus()
            })
          }}
        >
          Full readiness
        </button>
      }
    >
      <ul className="rows adg-caps">
        {roles.map(role => (
          <li className="row adg-cap" key={role.role} data-testid={`cap-${role.role}`}>
            <span className="adg-cap-role mono">{role.label}</span>
            <span className="adg-cap-copy">
              <small>{role.detail}</small>
            </span>
            <span className={`badge ${USABILITY_TONE[role.state] ?? 'gj-none'}`}>
              {role.headline}
            </span>
          </li>
        ))}
      </ul>
    </DossierCard>
  )
}

// ---- tier 2: trust and coverage --------------------------------------------------------------
interface CoverageRow {
  key: string
  label: string
  headline: string
  detail: string
  badge: string
}

// Coverage the asset-detail contract does not carry YET. Structure and honest absence only: no
// invented owner, no sample null rate, no placeholder SLA. A plausible-looking fabricated value in
// a governance catalog is worse than a blank, because somebody will act on it. When the API lands,
// replace this constant with the response field — the row shape is already what the panel renders.
const UNWIRED_COVERAGE: readonly CoverageRow[] = [
  {
    key: 'profiling',
    label: 'Data profiling',
    headline: 'Not profiled',
    detail: 'No observed null rate, distinctness, value range or samples are held for this column.',
    badge: 'not profiled',
  },
  {
    key: 'owner',
    label: 'Owner / steward',
    headline: 'Not assigned',
    detail: 'No accountable owner is held on this asset response.',
    badge: 'not set',
  },
  {
    key: 'sla',
    label: 'Operational SLA',
    headline: 'Not defined',
    detail: 'No refresh or quality SLA is held for this column.',
    badge: 'not set',
  },
]

// A timestamp a person can read. Falls back to the raw value rather than rendering "Invalid Date"
// if the server ever sends something Date cannot parse.
function readableAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

function coverageRows(detail: AssetDetail, isUnavailable: (name: string) => boolean): CoverageRow[] {
  const rows: CoverageRow[] = []
  const latest = detail.history?.runs[0]
  if (latest) {
    rows.push({
      key: 'ingestion',
      label: 'Catalog ingestion',
      headline: readableAt(latest.completed_at ?? latest.at),
      detail: `${latest.stages.length} recorded stages · ${latest.status.toLowerCase()}.`,
      badge: 'last observed',
    })
  }
  rows.push(...UNWIRED_COVERAGE)
  // Audit is separately gated by audit:read; when it is withheld the page says so rather than
  // implying nothing was ever recorded.
  if (isUnavailable('audit')) {
    rows.push({
      key: 'audit',
      label: 'LLM audit summaries',
      headline: 'Restricted',
      detail: 'This session’s roles do not include audit:read.',
      badge: 'role gated',
    })
  } else if (detail.audit) {
    rows.push({
      key: 'audit',
      label: 'LLM audit summaries',
      headline: `${detail.audit.summaries.length} recorded`,
      detail: 'Task, stage, provider and outcome are readable under your roles.',
      badge: 'available',
    })
  } else {
    // Neither returned nor refused: this response was not asked for the section. Saying "0" here
    // would report an absence of records when the truth is an absence of a question.
    rows.push({
      key: 'audit',
      label: 'LLM audit summaries',
      headline: 'Not requested',
      detail: 'This response did not ask for the audit section, so nothing is known either way.',
      badge: 'not read',
    })
  }
  return rows
}

function TrustCoverageCard({
  detail,
  isUnavailable,
}: {
  detail: AssetDetail
  isUnavailable: (name: string) => boolean
}) {
  return (
    <DossierCard
      testId="trust-coverage"
      title="Trust and coverage"
      subtitle="Information that matters is named even where the platform does not hold it."
    >
      <ul className="rows adg-coverage">
        {coverageRows(detail, isUnavailable).map(row => (
          <li className="row adg-cov" key={row.key} data-testid={`coverage-${row.key}`}>
            <span className="adg-cov-label">{row.label}</span>
            <span className="adg-cov-copy">
              <strong>{row.headline}</strong>
              <small>{row.detail}</small>
            </span>
            <span className="badge gj-none">{row.badge}</span>
          </li>
        ))}
      </ul>
    </DossierCard>
  )
}

// ---- tier 2: recommendations -----------------------------------------------------------------
// Deliberately NOT wrapped in a card: the suggestion cards carry their own border, background and
// shadow, and a card inside a card is always wrong. A section heading over the grid gives the
// cards the page ground to sit on.
function RecommendationsSection({
  source,
  identity,
  outcome,
  page,
  matching,
  table,
}: {
  source: string
  identity: AssetIdentity
  outcome: SuggestionsOutcome
  page: FeatureSuggestionPageV2 | null
  matching: FeatureSuggestionPageV2['hits']
  table: string
}) {
  return (
    <section className="adg-section adg-recs" data-testid="column-suggestions">
      <div className="adg-section-head adg-suggestion-head">
        <div>
          {/* "Suggested", not "recommended": the route, the screen and the API all call this object
              a suggestion, and a second word for the same thing costs more than it buys. */}
          <h3>Suggested features using this column</h3>
          {page && matching.length > 0 && (
            <p className="hint">
              {matching.length} of {page.collection.summary.suggested} table suggestions use{' '}
              <code>{identity.column}</code>.
            </p>
          )}
        </div>
        {/* The handoff needs a table to land on. `table` is '' when identity.table is null, and
            '#/suggested?source=X&table=' is a dead destination — so withhold the action rather
            than offer one that goes nowhere. */}
        {table && (
          <a
            className="btn btn--ghost"
            href={`#/suggested?${new URLSearchParams({ source, table }).toString()}`}
          >
            View all table recommendations
          </a>
        )}
      </div>
      {outcome.kind === 'loading' ? (
        <p className="hint" role="status">Reading what the catalog can build with this column…</p>
      ) : outcome.kind === 'forbidden' ? (
        // Honest access message, never silently swallowed into an empty list.
        <p className="adg-unavailable" role="status">
          You don't have access to feature suggestions. This view needs the{' '}
          <code>catalog:read</code> permission and this session's roles don't carry it.
        </p>
      ) : outcome.kind === 'unsupported' ? (
        <p className="adg-unavailable" role="status">
          This deployment does not serve the discovery contract this screen asks for, so suggestions
          cannot be shown here. The server is older than this screen.
        </p>
      ) : outcome.kind === 'error' || !page ? (
        <p role="alert" className="error">
          Could not load suggestions: {outcome.kind === 'error' ? outcome.detail : 'no payload returned'}
        </p>
      ) : matching.length === 0 ? (
        <p className="hint">
          {page.collection.summary.suggested === 0
            ? 'No suggestions on this table yet.'
            : `None of the ${page.collection.summary.suggested} suggestions on this table uses `
              + 'this column.'}
        </p>
      ) : (
        <ul className="rows adg-suggestion-grid">
          {matching.map(hit => (
            <SuggestionCard
              key={hit.suggestion.suggestion_id} hit={hit} headingLevel={4}
              omitted={page.collection.omitted_counts}
            />
          ))}
        </ul>
      )}
    </section>
  )
}

// ---- tier 3: the receipts --------------------------------------------------------------------
// Every ref, the response state and the snapshot token. This used to be the FIRST section on the
// page — the least business-relevant content in the most valuable position. It is reachable in one
// click and out of the way otherwise.
function TechnicalIdentity({ detail }: { detail: AssetDetail }) {
  const { identity } = detail
  const t = typeDisplay(identity)
  const rows: [string, string][] = [
    ['Source', identity.source],
    ['Kind', identity.kind],
    ...(identity.schema_name ? [['Schema', identity.schema_name] as [string, string]] : []),
    ...(identity.table ? [['Table', identity.table] as [string, string]] : []),
    ...(identity.column ? [['Column', identity.column] as [string, string]] : []),
    ['Object reference', identity.object_ref],
    ['Logical reference', identity.logical_ref],
    ['Graph reference', identity.graph_ref],
    ['Declared type', identity.declared_type ?? '— none declared'],
    ['Operational type', t.basis === 'operational'
      ? (identity.operational_type ?? '')
      : '— not attested yet'],
    ['Sections returned', detail.included_sections.join(', ') || '— none'],
    ['Sections withheld', detail.unavailable_sections.join(', ') || '— none'],
    ['Consistency token', detail.consistency_token],
  ]
  return (
    <details className="adg-tech" data-testid="technical-identity">
      <summary>Technical identity, response state and catalog references</summary>
      <dl className="adg-tech-grid">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd className="mono">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="hint">
        The declared type is what the source's schema calls this column. The operational type is
        whether it is actually numeric-usable — only a technical source (a real database connector)
        attests it. A declared type is never on its own evidence a column is operationally numeric.
      </p>
    </details>
  )
}

// ---- the tab ---------------------------------------------------------------------------------

export function OverviewTab({
  detail,
  source,
  isUnavailable,
  onOpenReadiness,
}: {
  detail: AssetDetail
  source: string
  isUnavailable: (name: string) => boolean
  onOpenReadiness: () => void
}) {
  const { identity } = detail
  const metadata = detail.effective_metadata
  const isColumn = identity.kind === 'column' && !!identity.table
  const { outcome, page, matching, table } = useColumnSuggestions(source, identity)
  const roles = detail.readiness?.usability?.roles ?? []

  return (
    <>
      <SummaryStrip detail={detail} matching={matching} page={page} isColumn={isColumn} />

      <div className="adg-grid">
        <MeaningCard metadata={metadata} isUnavailable={isUnavailable} />
        <SourceGlossaryCard detail={detail} />
        <SemanticsCard metadata={metadata} isUnavailable={isUnavailable} isColumn={isColumn} />
        {roles.length > 0 && !isUnavailable('readiness') && (
          <CapabilitiesCard roles={roles} onOpenReadiness={onOpenReadiness} />
        )}
        <TrustCoverageCard detail={detail} isUnavailable={isUnavailable} />
      </div>

      {isColumn && (
        <RecommendationsSection
          source={source}
          identity={identity}
          outcome={outcome}
          page={page}
          matching={matching}
          table={table}
        />
      )}

      <TechnicalIdentity detail={detail} />
    </>
  )
}
