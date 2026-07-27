import { useEffect, useState } from 'react'
import {
  ApiError,
  type FeatureSuggestion,
  type SuggestionGroup,
  type SuggestionRequirement,
  type TableSuggestions,
  getTableSuggestions,
} from '../api'

// Suggested features: what the deterministic engine can already build on ONE table — no hypothesis,
// no intent, no LLM. STRICTLY READ-ONLY: there is deliberately no accept / edit / dismiss control,
// because v1 writes nothing. Two honesty rules the mockup does not get to override:
//   * no relevance percentage — no percentage scorer exists in this system, so the only signal shown
//     is the engine's own `binding_quality` string;
//   * an empty or all-needs-review screen names its CAUSE. A bare "0 suggested" reads as broken; the
//     three measured causes (no business concepts / no confirmed as-of / undeclared unit-currency)
//     each get their own sentence pointing at the surface that fixes it.

// The closed requirement vocabulary in plain words. A card says what is missing, not an enum; the
// code still renders beside it so it stays traceable to the gauntlet. An unknown code (a newer
// backend) degrades to its de-underscored words rather than disappearing.
const REQUIREMENT_WORDS: Record<string, string> = {
  TYPE_IS_NUMERIC: 'needs a confirmed numeric type',
  GRAIN_IS_UNIQUE: 'needs a confirmed unique grain',
  TEMPORAL_IS_POPULATED: 'needs a populated as-of column',
  TEMPORAL_LAG_BOUNDED: 'needs a bounded as-of lag',
  JOIN_CONNECTIVITY: 'needs a confirmed join path',
  UNIT_CONSISTENT: 'needs a declared unit',
  CURRENCY_CONSISTENT: 'needs a declared currency',
  ADDITIVITY_SUPPORTS_OPERATION: 'needs additivity that supports this operation',
}

function requirementWords(code: string): string {
  return REQUIREMENT_WORDS[code] ?? code.replaceAll('_', ' ').toLowerCase()
}

const STATUS_LABEL: Record<string, string> = {
  DESIGN_CHECKED: 'clean & ready',
  NEEDS_EXTERNAL_VALIDATION: 'needs review',
}

// clean & ready reads as governed-solid; needs review as partial (warn) — never as rejected. A card
// that needs a declared fact is honest output, not a failure.
const STATUS_TONE: Record<string, string> = {
  DESIGN_CHECKED: 'gj-verified',
  NEEDS_EXTERNAL_VALIDATION: 'gj-partial',
}

// The column name — the ref's last segment. A full schema.table.column ref is unreadable on a card,
// and the page is already scoped to one table, so the short name is unambiguous here.
function columnOf(ref: string): string {
  return ref.split('.').pop() ?? ref
}

export function SuggestedFeaturesScreen({ source, table }: { source: string; table: string }) {
  const [data, setData] = useState<TableSuggestions | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    setLoading(true)
    setError('')
    getTableSuggestions(source, table)
      .then(body => {
        if (!live) return
        setData(body)
      })
      .catch((e: unknown) => {
        if (!live) return
        setError(e instanceof ApiError ? e.detail : String(e))
        setData(null)
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
    }
  }, [source, table])

  if (loading) {
    return (
      <section className="sug">
        <p role="status" className="hint">
          Reading what this catalog can build on <code>{table}</code>…
        </p>
      </section>
    )
  }

  if (error || !data) {
    return (
      <section className="sug">
        <p role="alert" className="error">
          Could not load suggestions: {error || 'no payload returned'}
        </p>
      </section>
    )
  }

  const { summary, groups, rejections } = data
  const noPointInTime = rejections.filter(r => r.code === 'NO_POINT_IN_TIME')
  const hasCards = groups.some(g => g.suggestions.length > 0)

  return (
    <section className="sug">
      <p className="hint sug-scope">
        <span className="mono">{data.table}</span> · <span className="mono">{data.catalog_source}</span>
      </p>

      <div className="stats" role="group" aria-label="Suggestion summary">
        <Stat n={summary.suggested} label="suggested" />
        {/* deliberately untoned: 0 clean & ready is an honest state, never an error */}
        <Stat n={summary.clean_ready} label="clean & ready" />
        <Stat n={summary.needs_review} label="need review" />
        <Stat n={summary.entities} label="entities" />
      </div>

      <p className="hint sug-readonly">
        This view is read-only. These are proposals the catalog can already ground — accepting or
        dismissing one is not offered here, and nothing on this screen changes the catalog.
      </p>

      {/* Cause #1: nothing to suggest and nothing blocked — the columns carry no business concepts. */}
      {summary.suggested === 0 && rejections.length === 0 && (
        <div className="callout callout--accent">
          <div className="callout-body">
            <p>
              <strong>No suggestions yet</strong> — this table’s columns don’t carry business
              concepts, so no template can ground on it.
            </p>
            <p className="hint">
              Concepts arrive from enrichment and are confirmed on the Semantics screen. Give the
              columns their meaning (entity, unit, currency, additivity) and suggestions appear here.
            </p>
          </div>
        </div>
      )}

      {/* Cause #2: the highest-value state — a governance to-do, not an empty screen. */}
      {noPointInTime.length > 0 && (
        <div className="callout callout--warn">
          <div className="callout-body">
            <p>
              <strong>
                {noPointInTime.length} {noPointInTime.length === 1 ? 'feature is' : 'features are'}
                {' '}blocked
              </strong>
              : this table has no confirmed as-of column, so every windowed feature would risk
              future leakage.
            </p>
            <p className="hint">
              Confirm the table’s as-of column on Governance review → Grain &amp; availability, and
              these come back on their own.
            </p>
          </div>
        </div>
      )}

      {/* Cause #3: cards exist but none is clean — the honest NORMAL state, not a failure. */}
      {summary.clean_ready === 0 && hasCards && (
        <p className="hint sug-note">
          Nothing is clean &amp; ready yet: every suggestion below needs one more declared fact
          before its numbers can be trusted. Each card names the fact it is waiting on.
        </p>
      )}

      {groups.map(group => (
        <EntityGroup key={group.entity_ref || group.entity_label || 'unlabelled'} group={group} />
      ))}

      {rejections.length > noPointInTime.length && (
        <section className="panel sug-rejections">
          <h2>Not offered</h2>
          <ul className="rows">
            {rejections
              .filter(r => r.code !== 'NO_POINT_IN_TIME')
              .map(r => (
                <li className="row" key={`${r.code}:${r.name}`}>
                  <span className="mono">{r.name}</span>{' '}
                  <span className="hint">{r.reason}</span>
                </li>
              ))}
          </ul>
        </section>
      )}
    </section>
  )
}

// Same semantics as the dashboard's Stat, minus the tones: no count on this screen is an alert.
function Stat({ n, label }: { n: number; label: string }) {
  return (
    <div className="stat">
      <b>{n}</b> {label}
    </div>
  )
}

// One entity's features. The heading is the ENTITY (entity_label, e.g. 'account'); the suffix is the
// COLUMN it is computed per (entity_ref). A group whose entity could not be named renders NO
// heading — inventing one would claim an entity the catalog never attested.
function EntityGroup({ group }: { group: SuggestionGroup }) {
  return (
    <section className="sug-group">
      {group.entity_ref && (
        <div className="sug-group-head">
          <h2 className="sug-group-title">{group.entity_label} features</h2>
          <span className="hint mono" title={group.entity_ref}>
            per entity {columnOf(group.entity_ref)}
          </span>
        </div>
      )}
      <ul className="rows">
        {group.suggestions.map(s => <SuggestionCard key={s.name} suggestion={s} />)}
      </ul>
    </section>
  )
}

function SuggestionCard({ suggestion }: { suggestion: FeatureSuggestion }) {
  const status = suggestion.validation_status
  return (
    <li className="row q-item sug-card">
      <div className="q-head">
        <span className="gj-kind sug-name">{suggestion.name}</span>
        <span className={`badge ${STATUS_TONE[status] ?? 'gj-none'}`}>
          {STATUS_LABEL[status] ?? status}
        </span>
        {suggestion.binding_quality && (
          <span className="gj-score">binding {suggestion.binding_quality}</span>
        )}
      </div>
      <p className="sug-desc">{suggestion.description}</p>
      <p className="mono sug-recipe">{suggestion.recipe}</p>
      {suggestion.uses.length > 0 && (
        <p className="hint sug-uses">uses {suggestion.uses.map(columnOf).join(', ')}</p>
      )}
      {suggestion.requirements.length > 0 && (
        <ul className="adg-reqs sug-reqs">
          {suggestion.requirements.map((req: SuggestionRequirement) => (
            <li key={`${req.code}:${req.operand.join(':')}`}>
              {requirementWords(req.code)}{' '}
              <span className="mono">{columnOf(req.operand[req.operand.length - 1] ?? '')}</span>{' '}
              <span className="hint">{req.detail || req.code}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}
