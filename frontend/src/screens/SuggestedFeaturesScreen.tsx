import { useEffect, useState } from 'react'
import {
  ApiError,
  type FeatureSuggestionHit,
  type FeatureSuggestionPageV2,
  type JoinNeighbourhood,
  SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION,
  type SuggestionGroupV2,
  type SuggestionOmittedCounts,
  type SuggestionSemanticBlock,
  getTableSuggestionsV2,
  getTableSuggestionsV4,
} from '../api'
import { useIdentityKey } from '../session'
import { SuggestionCard, TermChip, TruncationNotes } from './SuggestionCard'
import { bounded, columnOf } from './suggestionText'

// Suggested features: what the deterministic engine can already build on ONE table — no hypothesis,
// no intent, no LLM. The CARD and its drawer live in `./SuggestionCard`, which the column dossier
// imports too; this module owns only what is page-level — the summary, the cause callouts, the
// bounded-neighbourhood note and the per-entity grouping.
//
// STRICTLY READ-ONLY: the route writes nothing, and the only control on the surface is the card's
// disclosure toggle.
//
// Honesty rules the mockup does not get to override:
//   * an empty or all-unvalidated screen names its CAUSE, and names what is MISSING rather than
//     instructing the user to approve something that is not a gate. A proposal from enrichment or
//     from the source file is usable context; the absence here is an absent PROPOSAL, not an absent
//     approval;
//   * Release A serves `read_mode=on_demand` with `projection: null`. Nothing here invents a
//     "current" badge out of an absent projection.

// ── page-level notes ────────────────────────────────────────────────────────────────────────────

// WHICH bound dropped the tables that were dropped, in words. Named rather than numbered on purpose:
// the numbers live in ONE place (join_path.MAX_NEIGHBOUR_TABLES / MAX_COLUMNS_CONSIDERED, quoted in
// the payload's own terms), so a copy that printed "20" here would be free to drift from the cap the
// server actually applied. An unknown reason from a newer backend degrades to no clause at all
// rather than to a guess.
const LIMIT_WORDS: Record<string, string> = {
  table_cap: 'the automatic limit on how many joined tables one page loads',
  column_budget: 'the automatic limit on how many columns one page grounds against',
}

// What the page did NOT look at. Suggestions are grounded on this table plus the tables joined
// DIRECTLY to it, capped — walking the join graph transitively has no resource bound on a real
// catalog, where almost everything reaches the customer/account hub. Saying nothing would turn
// every empty state on this screen into a claim the page never actually checked ("nothing else is
// buildable here" when the truth is "we did not look"). So the limit is stated ALWAYS, truncated or
// not: deeper join paths exist, and they were deliberately not loaded.
function NeighbourhoodNote({ n }: { n: JoinNeighbourhood | null }) {
  // A null neighbourhood is an ABSENT fact, not a licence to omit the note. Dropping the whole
  // paragraph would silently turn every empty state on this page back into the claim this note
  // exists to prevent — "nothing else is buildable here" when the truth is "we did not look".
  if (n === null) {
    return (
      <p className="hint sug-neighbourhood" data-testid="neighbourhood">
        <span className="sfc-absent">The join neighbourhood was not reported.</span> This page does
        not know how many joined tables it considered, so treat the list below as grounded on this
        table alone and not as a statement about what deeper join paths could build.
      </p>
    )
  }
  const joins = n.max_hops === 1 ? '1 join' : `${n.max_hops} joins`
  const because = n.limit_reason ? LIMIT_WORDS[n.limit_reason] : ''
  return (
    <p className="hint sug-neighbourhood" data-testid="neighbourhood">
      {n.truncated ? (
        <>
          Showing <strong>{n.tables_considered} of {n.tables_available}</strong> directly joined
          tables{because ? ` — ${because}` : ''}.{' '}
        </>
      ) : n.tables_available > 0 ? (
        <>
          Grounded on this table and all {n.tables_available} directly joined{' '}
          {n.tables_available === 1 ? 'table' : 'tables'}.{' '}
        </>
      ) : (
        <>
          Grounded on this table alone — no confirmed join reaches another table from here.{' '}
        </>
      )}
      Deeper join paths were not automatically considered: only tables within {joins} of this one are
      loaded on a page view.
    </p>
  )
}

// ── the screen ──────────────────────────────────────────────────────────────────────────────────

// Exactly one of these is true at a time, so the screen cannot render a permission message and a
// payload at once — and, more importantly, each is stored WITH the read scope it belongs to.
type Outcome =
  | { kind: 'loading' }
  // `semantic` is present when the deployment served contract v4; absent means the screen fell
  // back to the older page — the cards render either way, the engine section only with v4.
  | { kind: 'ok'; page: FeatureSuggestionPageV2; semantic?: SuggestionSemanticBlock }
  | { kind: 'forbidden' }
  | { kind: 'unsupported' }
  | { kind: 'error', detail: string }

export function SuggestedFeaturesScreen({
  source,
  table,
  fromColumn,
}: {
  source: string
  table: string
  // Optional: the column the reader clicked through from. Highlights, never filters — hiding
  // suggestions the reader did not ask to hide would be a worse answer than an unmarked list.
  fromColumn?: string
}) {
  // Read scope decides which columns ground and which suggestions exist at all, so a result read
  // under one identity is not an answer for another. Keyed on principal + claims, NOT on the URL.
  const identity = useIdentityKey()
  // The separator is an escaped NUL: it is the one character that cannot occur in a principal,
  // a catalog source or a table ref, so no pair of different scopes can collide into the same
  // key. Written as an ESCAPE, never as a literal byte — a raw NUL in the source makes git treat
  // the file as binary (diffs vanish) and hides it from every plain grep.
  const requestKey = `${identity}\u0000${source}\u0000${table}`
  // The outcome is stored WITH the key it was read under, and the render below trusts it only when
  // the two still match. An effect cannot give that guarantee: `setData(null)` runs AFTER the render
  // triggered by the session store's update, so a version that only cleared in the effect would
  // paint the previous scope's cards once before clearing them. Here a stale key simply is not an
  // answer, so the previous scope's rows can never reach the DOM.
  const [result, setResult] = useState<{ key: string; outcome: Outcome }>(
    { key: requestKey, outcome: { kind: 'loading' } })
  const outcome: Outcome = result.key === requestKey ? result.outcome : { kind: 'loading' }

  useEffect(() => {
    let live = true
    const settle = (o: Outcome) => {
      if (live) setResult({ key: requestKey, outcome: o })
    }
    settle({ kind: 'loading' })
    // SE-13: ask for contract v4 (the v3 execution truth + the engine's semantic block). An
    // older backend that refuses v4 gets ONE graceful step down to the v2 page this screen has
    // always rendered — a rolling deploy must not blank the page — and only a deployment that
    // refuses BOTH is reported as unsupported.
    getTableSuggestionsV4(source, table)
      .then(body => settle({ kind: 'ok', page: body, semantic: body.semantic }))
      .catch((e: unknown) => {
        // A 403 is not a failure to report as one: the route is gated on catalog:read and this
        // session's roles do not carry it. Kept a distinct outcome so the screen can say WHICH
        // permission is missing instead of leaking the server's detail string into a red alert.
        if (e instanceof ApiError && e.status === 403) settle({ kind: 'forbidden' })
        else if (e instanceof ApiError
          && e.errorCode === SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION) {
          getTableSuggestionsV2(source, table)
            .then(body => settle({ kind: 'ok', page: body }))
            .catch((e2: unknown) => {
              if (e2 instanceof ApiError && e2.status === 403) settle({ kind: 'forbidden' })
              else if (e2 instanceof ApiError
                && e2.errorCode === SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION) {
                settle({ kind: 'unsupported' })
              } else settle({ kind: 'error', detail: e2 instanceof ApiError ? e2.detail : String(e2) })
            })
        } else settle({ kind: 'error', detail: e instanceof ApiError ? e.detail : String(e) })
      })
    return () => {
      live = false
    }
  }, [source, table, requestKey])

  if (outcome.kind === 'loading') {
    return (
      <section className="sug">
        <p role="status" className="hint">
          Reading what this catalog can build on <code>{table}</code>…
        </p>
      </section>
    )
  }

  // Read-only and permission-gated: the honest answer is which permission is missing, not a red
  // "could not load". Deliberately no fix-it control — the role model is not this screen's to change.
  if (outcome.kind === 'forbidden') {
    return (
      <section className="sug">
        <div className="callout callout--warn">
          <div className="callout-body">
            <p role="status">
              <strong>You don’t have access to feature suggestions.</strong> This view needs the{' '}
              <code>catalog:read</code> permission and this session’s roles don’t carry it.
            </p>
            <p className="hint">
              Roles that hold it: catalog_viewer, data_owner, feature_engineer and platform_admin.
            </p>
          </div>
        </div>
      </section>
    )
  }

  if (outcome.kind === 'unsupported') {
    return (
      <section className="sug">
        <div className="callout callout--warn">
          <div className="callout-body">
            <p role="status">
              <strong>This deployment does not serve the discovery contract.</strong> The screen
              asked for suggestion contract version 4, stepped down to version 2, and the server
              refused both.
            </p>
            <p className="hint">
              The server is older than this screen. Nothing is wrong with the catalog or with your
              permissions.
            </p>
          </div>
        </div>
      </section>
    )
  }

  if (outcome.kind === 'error') {
    return (
      <section className="sug">
        <p role="alert" className="error">
          Could not load suggestions: {outcome.detail || 'no payload returned'}
        </p>
      </section>
    )
  }

  const data = outcome.page
  const { collection } = data
  const { summary, groups, rejections } = collection

  // Cause #0: there is no such table. Stands alone — every count below would be a claim about a
  // table this catalog does not hold.
  if (collection.table_known === false) {
    return (
      <section className="sug">
        <div className="callout callout--warn">
          <div className="callout-body">
            <p role="status">
              <strong>No such table in this catalog.</strong>{' '}
              <span className="mono">{collection.anchor_catalog_source}</span> holds no table called{' '}
              <span className="mono">{collection.anchor_table_ref}</span>, so there is nothing to
              suggest on — and nothing here is a statement about its columns.
            </p>
            <p className="hint">
              Check the catalog and the table name (the bare name, e.g. <code>comp_fin_tran</code>,
              or its full <code>schema.table</code> ref). Search the catalog to find the table, then
              open Suggested features from one of its hits.
            </p>
          </div>
        </div>
      </section>
    )
  }

  const byId = new Map(data.hits.map(hit => [hit.suggestion.suggestion_id, hit]))
  const noPointInTime = rejections.filter(r => r.code === 'NO_POINT_IN_TIME')
  const hasCards = data.hits.length > 0

  return (
    <section className="sug">
      <p className="hint sug-scope">
        <span className="mono">{collection.anchor_table_ref}</span> ·{' '}
        <span className="mono">{collection.anchor_catalog_source}</span>
      </p>

      {/* The page is table-scoped, so arriving from four different columns produced four
          identical pages with nothing naming the one you clicked -- indistinguishable from a
          broken link. This states the context that the table-scoped handoff drops. It
          HIGHLIGHTS and never filters: hiding suggestions the reader did not ask to hide
          would be a worse answer than an unmarked list. */}
      {fromColumn && (
        <p className="sfs-from" role="status">
          You opened this from <code>{fromColumn}</code>.{' '}
          {(() => {
            const ref = `${table}.${fromColumn}`.toLowerCase()
            const n = data.hits.filter(h => h.suggestion.operands
              .some(o => o.graph_object_ref.toLowerCase().endsWith(ref)
                || o.graph_object_ref.toLowerCase().endsWith(`.${fromColumn.toLowerCase()}`))).length
            return n === 0
              ? 'None of the suggestions below bind it — they are the rest of what this table can build.'
              : `${n} of ${data.hits.length} below bind it; the rest are what else this table can build.`
          })()}
        </p>
      )}

      <div className="stats" role="group" aria-label="Suggestion summary">
        <Stat n={summary.suggested} label="suggested" />
        {/* deliberately untoned: 0 design checked is an honest state, never an error */}
        <Stat n={summary.design_checked} label="design checked" />
        <Stat n={summary.needs_external_validation} label="need external validation" />
        <Stat n={summary.groups} label="entities" />
      </div>

      <p className="hint sug-note">
        Design checked means the inputs pass the catalog’s design rules. It is not proof that a
        feature predicts anything, and not proof that it can run in production.
      </p>

      {/* Read-only posture and page currentness in one statement: both are facts about THIS view,
          and Release A has no stored projection, so no badge here may claim to be current. */}
      <p className="hint sug-readonly" data-testid="currentness">
        These are suggestions, not registered features. A governed recipe grounded each one against
        this catalog; none of them is in the feature registry, and this view is read-only.{' '}
        {data.read_mode === 'on_demand' && data.projection === null ? (
          <>
            Nothing here is stored: the page was computed for this request, so no card claims to be
            current or stale.
          </>
        ) : (
          <>Served from a stored projection, state {data.projection?.state ?? 'unknown'}.</>
        )}
      </p>

      <NeighbourhoodNote n={collection.neighbourhood} />

      {/* Cause #1: nothing to suggest and nothing blocked — no column carries the meaning a recipe
          needs. The copy names what is MISSING, never an approval the user is told to perform: a
          proposal from enrichment or from the upload is already usable context. */}
      {summary.suggested === 0 && rejections.length === 0 && (
        <div className="callout callout--accent">
          <div className="callout-body">
            <p>
              <strong>No suggestions yet.</strong> No column on this table carries the business
              meaning a recipe needs: an entity to compute per, a quantity to measure, and an as-of
              date to compute at.
            </p>
            <p className="hint">
              A meaning proposed by enrichment or declared in the upload is enough to ground on. It
              does not have to be confirmed first, so nothing here is waiting on an approval. What is
              missing is a proposal. Run enrichment on this source, or declare the columns&apos;
              meaning in the next upload.
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
              Confirm the table’s as-of column on Governance review — it waits there under As-of
              date — and these come back on their own.
            </p>
          </div>
        </div>
      )}

      {/* Cause #3: cards exist but none is design checked — the honest NORMAL state, not a failure. */}
      {summary.design_checked === 0 && hasCards && (
        <p className="hint sug-note">
          Nothing is design checked yet: every suggestion below needs one more declared fact before
          its inputs pass the catalog’s design rules. Each card names the fact it is waiting on.
        </p>
      )}

      {groups.map(group => (
        <EntityGroup
          key={group.grain_refs[0]?.join(':') ?? 'unlabelled'}
          group={group} byId={byId} omitted={collection.omitted_counts}
        />
      ))}

      {/* SE-13: the ENGINE's verdicts — the same lens the hypothesis Workbench serves from,
          run without a hypothesis and anchored to this table. Evidence beside the cards, never
          a re-ranking of them; absent entirely when the deployment served the older contract. */}
      {outcome.semantic && (
        <SemanticEngineSection semantic={outcome.semantic} />
      )}

      {Object.values(collection.omitted_counts).some(n => (n ?? 0) > 0) && (
        <section className="panel sug-omitted" data-testid="page-truncation">
          <h2>What this page left out</h2>
          <TruncationNotes omitted={collection.omitted_counts} />
        </section>
      )}

      {rejections.length > noPointInTime.length && (
        <section className="panel sug-rejections">
          <h2>Not offered</h2>
          <ul className="rows">
            {rejections
              .filter(r => r.code !== 'NO_POINT_IN_TIME')
              .map(r => (
                <li className="row" key={`${r.code}:${r.candidate_name}`}>
                  <span className="mono">{r.candidate_name}</span>{' '}
                  <span className="hint">{r.explanation}</span>
                </li>
              ))}
          </ul>
        </section>
      )}
    </section>
  )
}

// SE-13: the engine's verdicts for this table. Ranked = bound, in the designed composite order
// (basis stated by the backend, echoed in the title). Actionable = undecided work with its named
// resolution — shown as a to-do, never as a low rank. One engine serves this and the Workbench,
// so the two surfaces cannot disagree about a binding's validity; this section is that engine's
// answer, verbatim.
function SemanticEngineSection({ semantic }: { semantic: SuggestionSemanticBlock }) {
  if (semantic.ranked.length === 0 && semantic.actionable.length === 0) return null
  return (
    <section className="panel sug-semantic" data-testid="semantic-engine">
      <h2>What the planning engine says</h2>
      <p className="hint" title={`order basis: ${semantic.order_basis}`}>
        The same engine that plans hypothesis workbench candidates, run against this table —
        context <span className="mono">{semantic.semantic_context_hash.slice(0, 12)}</span>.
      </p>
      {semantic.ranked.length > 0 && (
        <ul className="rows" aria-label="bindable recipes">
          {semantic.ranked.map(entry => (
            <li key={entry.recipe_id}>
              {/* D4 (UI-05): ONE card model — the projected FeatureIdea carrier the
                  Workbench renders, from the same server-side serializer. The raw
                  recipe-id row survives only for pre-D4 deployments (no card). */}
              {entry.card ? (
                <div style={{ display: 'grid', gap: 4 }}>
                  <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                    <span style={{ fontWeight: 600 }}>{entry.card.name}</span>
                    <span className="badge ok">bindable</span>
                    {entry.card.operation_class && (
                      <span className="badge">{entry.card.operation_class}</span>
                    )}
                    {entry.card.validation_status === 'NEEDS_EXTERNAL_VALIDATION' && (
                      <span className="badge stale">
                        needs data checks{entry.card.requirements?.length
                          ? ` (${entry.card.requirements.length})` : ''}
                      </span>
                    )}
                    {entry.review_current
                      ? <span className="badge ok">reviewed</span>
                      : <span className="badge">unreviewed</span>}
                  </div>
                  <p className="hint" style={{ margin: 0 }}>{entry.card.description}</p>
                  {(entry.card.input_role_bindings?.length ?? 0) > 0 && (
                    <ul aria-label="typed inputs" style={{ display: 'grid', gap: 2,
                        margin: 0, paddingLeft: 16, fontSize: 13 }}>
                      {entry.card.input_role_bindings!.map(binding => (
                        <li key={binding.role}>
                          <span style={{ fontWeight: 600 }}>{binding.role}</span>
                          {binding.ref && <> — <span className="mono">{binding.ref[1]}</span></>}
                          {binding.authority && <> · {binding.authority}</>}
                        </li>
                      ))}
                    </ul>
                  )}
                  {entry.corroborations.length > 0 && (
                    <span className="hint">also arrived at by {entry.corroborations
                      .map(c => c.origin).join(', ')}</span>
                  )}
                </div>
              ) : (
                <>
                  <span className="mono" style={{ fontWeight: 600 }}>{entry.recipe_id}</span>{' '}
                  <span className="badge ok">bindable</span>{' '}
                  <span className="badge">{entry.readiness.toLowerCase().replace(/_/g, ' ')}</span>
                  {entry.review_current
                    ? <span className="badge ok"> reviewed</span>
                    : <span className="badge"> unreviewed</span>}
                  {entry.corroborations.length > 0 && (
                    <span className="hint"> · also arrived at by {entry.corroborations
                      .map(c => c.origin).join(', ')}</span>
                  )}
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {semantic.actionable.length > 0 && (
        <>
          <h3>Could be useful if…</h3>
          <ul className="rows" aria-label="recipes needing a decision">
            {semantic.actionable.map(entry => {
              const action = entry.verdicts.find(v => v.resolution)?.resolution
                ?? 'no eligible binding for every required operand'
              return (
                <li key={entry.recipe_id}>
                  <span className="mono" style={{ fontWeight: 600 }}>{entry.recipe_id}</span>{' '}
                  <span className="badge stale">{entry.binding_state}</span>{' '}
                  <span className="hint">{action}</span>
                </li>
              )
            })}
          </ul>
        </>
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

// One entity's features. The heading is the ENTITY (entity.display_name, e.g. 'account'); the
// suffix is the COLUMN it is computed per. A group bound to a column the catalog could not NAME an
// entity for shows only that column — a heading built from the column would read "cif_id features"
// beside "customer features", claiming an entity the catalog never attested.
function EntityGroup({
  group,
  byId,
  omitted,
}: {
  group: SuggestionGroupV2
  byId: Map<string, FeatureSuggestionHit>
  omitted: SuggestionOmittedCounts
}) {
  const grain = group.grain_refs.map(r => columnOf(r[1])).join(', ')
  return (
    <section className="sug-group">
      {grain && (
        <div className="sug-group-head">
          {group.entity && <h2 className="sug-group-title">{group.entity.display_name} features</h2>}
          <span className="hint mono" title={bounded(group.grain_refs.map(r => r[1]).join(', '))}>
            per entity {grain}
          </span>
          {group.contextual_entity_terms.map(t => <TermChip key={t.value} text={t} />)}
        </div>
      )}
      <ul className="rows">
        {group.suggestion_ids.map(id => {
          const hit = byId.get(id)
          return hit
            ? <SuggestionCard key={id} hit={hit} omitted={omitted} />
            : null
        })}
      </ul>
    </section>
  )
}
