import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ApiError,
  RECIPE_REVIEW_DECISIONS,
  type RecipeDetail,
  type RecipeReviewDecision,
  type RecipeReviewSummaryRow,
  type RecipeReviews,
  getRecipeDetail,
  getRecipeReviewSummary,
  getRecipeReviews,
  postRecipeReview,
} from '../api'

// RECIPE REVIEWS — the BR-23 sign-off surface.
//
// The queue lists every governed V2 recipe with its review validity AT ITS CURRENT REVISION —
// computed server-side by the validity fold, never derived here. Selecting a recipe loads the
// definition the reviewer is signing (the serialized contract itself) plus the immutable event
// history, and the decision form submits the revision hash on screen as the optimistic-concurrency
// token: if the definition changed since it rendered, the server answers 409 and the screen says
// so — a stale tab can never approve blind.
//
// Vocabulary discipline: validity has three honest states and none of them is failure.
// "Approved" (current at this revision), "Blocked" (a recorded decision stands in the way —
// changes_required/rejected/retired, or one person signed a multi-role requirement), and
// "Awaiting roles" (nobody with a required role has decided yet). The queue never renders an
// unreviewed recipe as an error: unreviewed is the honest starting state of all of them.
//
// What this screen deliberately does NOT do: gate the form on a client-side role check. The
// server owns governance:confirm; the form states the requirement and relays the server's own
// refusal verbatim rather than pre-judging who may decide.

type ValidityState = 'approved' | 'blocked' | 'awaiting'

function validityState(row: RecipeReviewSummaryRow): ValidityState {
  if (row.validity.current) return 'approved'
  if (row.validity.blocking_decisions.length > 0 || row.validity.single_identity_violation) {
    return 'blocked'
  }
  return 'awaiting'
}

function validityLabel(row: RecipeReviewSummaryRow): string {
  const state = validityState(row)
  if (state === 'approved') return 'Approved'
  if (state === 'blocked') {
    return row.validity.blocking_decisions.length > 0
      ? `Blocked · ${row.validity.blocking_decisions.join(', ').replaceAll('_', ' ')}`
      : 'Blocked · one identity signed every role'
  }
  const n = row.validity.missing_roles.length
  return n === row.validity.required_roles.length
    ? `Awaiting all ${n} roles`
    : `Awaiting ${n} of ${row.validity.required_roles.length} roles`
}

const DECISION_LABEL: Record<RecipeReviewDecision, string> = {
  approved: 'Approve',
  changes_required: 'Changes required',
  rejected: 'Reject',
  retired: 'Retire',
}

// Absent optional prose renders as the words "not set" — never a blank that could read as an
// empty policy, and never an invented value.
function orNotSet(value: string): string {
  return value.trim() ? value : 'not set'
}

function shortHash(hash: string): string {
  return `${hash.slice(0, 12)}…`
}

function Kv({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="rr-kv">
      <span className="rr-kv-label">{label}</span>
      <span className={mono ? 'rr-kv-value mono' : 'rr-kv-value'}>{value}</span>
    </div>
  )
}

export function RecipeReviewScreen({ initialRecipe = '' }: { initialRecipe?: string }) {
  const [summary, setSummary] = useState<RecipeReviewSummaryRow[] | null>(null)
  const [loadError, setLoadError] = useState('')
  const [query, setQuery] = useState('')
  const [family, setFamily] = useState('')
  const [status, setStatus] = useState<'' | ValidityState>('')
  const [selectedId, setSelectedId] = useState(initialRecipe)
  const [detail, setDetail] = useState<RecipeDetail | null>(null)
  const [reviews, setReviews] = useState<RecipeReviews | null>(null)
  const [detailError, setDetailError] = useState('')

  const [formRole, setFormRole] = useState('')
  const [formDecision, setFormDecision] = useState<RecipeReviewDecision>('approved')
  const [rationale, setRationale] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [submitStale, setSubmitStale] = useState(false)
  const [recorded, setRecorded] = useState('')

  const loadSummary = useCallback(async () => {
    try {
      const page = await getRecipeReviewSummary()
      setSummary(page.recipes)
      setLoadError('')
    } catch (e) {
      setLoadError(e instanceof ApiError ? e.detail : 'The recipe queue could not be loaded.')
    }
  }, [])

  useEffect(() => { void loadSummary() }, [loadSummary])

  const loadSelected = useCallback(async (recipeId: string) => {
    setDetail(null)
    setReviews(null)
    setDetailError('')
    setSubmitError('')
    setSubmitStale(false)
    setRecorded('')
    setFormRole('')
    setFormDecision('approved')
    setRationale('')
    try {
      const [d, r] = await Promise.all([getRecipeDetail(recipeId), getRecipeReviews(recipeId)])
      setDetail(d)
      setReviews(r)
    } catch (e) {
      setDetailError(e instanceof ApiError ? e.detail : 'This recipe could not be loaded.')
    }
  }, [])

  useEffect(() => {
    if (selectedId) void loadSelected(selectedId)
  }, [selectedId, loadSelected])

  const families = useMemo(
    () => [...new Set((summary ?? []).map(r => r.family))].sort(),
    [summary],
  )

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return (summary ?? []).filter(row =>
      (!family || row.family === family)
      && (!status || validityState(row) === status)
      && (!q
        || row.recipe_id.toLowerCase().includes(q)
        || row.display_label.toLowerCase().includes(q)))
  }, [summary, query, family, status])

  const grouped = useMemo(() => {
    const groups = new Map<string, RecipeReviewSummaryRow[]>()
    for (const row of filtered) {
      const bucket = groups.get(row.family)
      if (bucket) bucket.push(row)
      else groups.set(row.family, [row])
    }
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [filtered])

  const submit = async () => {
    if (!detail || !formRole || !rationale.trim()) return
    setSubmitting(true)
    setSubmitError('')
    setSubmitStale(false)
    setRecorded('')
    try {
      await postRecipeReview(detail.recipe.recipe_id, {
        decision: formDecision,
        reviewerRole: formRole,
        reviewedRevisionHash: detail.recipe_revision_hash,
        rationale: rationale.trim(),
      })
      setRecorded(`${DECISION_LABEL[formDecision]} recorded as ${formRole.replaceAll('_', ' ')}.`)
      setRationale('')
      // The decision changed the fold's inputs: re-read reviews AND the queue rather than
      // predicting the new validity client-side.
      const [r] = await Promise.all([getRecipeReviews(detail.recipe.recipe_id), loadSummary()])
      setReviews(r)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setSubmitStale(true)
        setSubmitError(e.detail)
      } else {
        setSubmitError(e instanceof ApiError ? e.detail : 'The decision could not be recorded.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  if (loadError) {
    return (
      <div className="callout callout--danger">
        <div className="callout-body">
          <strong>The recipe queue could not be loaded.</strong>
          <p>{loadError}</p>
        </div>
      </div>
    )
  }
  if (summary === null) return <p className="hint">Loading the recipe registry…</p>

  const approvedCount = summary.filter(r => r.validity.current).length

  return (
    <div className="rr-layout">
      <aside className="rr-queue" aria-label="Recipe queue">
        <p className="hint">
          {summary.length} governed recipes · {approvedCount} fully approved at their current
          revision
        </p>
        <div className="rr-filters">
          <input
            className="rr-search"
            aria-label="Search recipes"
            placeholder="Search by name or id"
            value={query}
            onChange={e => setQuery(e.target.value)}
          />
          <select aria-label="Family" value={family} onChange={e => setFamily(e.target.value)}>
            <option value="">All families</option>
            {families.map(f => <option key={f} value={f}>{f.replaceAll('_', ' ')}</option>)}
          </select>
          <select
            aria-label="Review status"
            value={status}
            onChange={e => setStatus(e.target.value as '' | ValidityState)}
          >
            <option value="">Any review status</option>
            <option value="awaiting">Awaiting roles</option>
            <option value="blocked">Blocked</option>
            <option value="approved">Approved</option>
          </select>
        </div>
        {filtered.length === 0 && <p className="hint">No recipes match these filters.</p>}
        {grouped.map(([fam, rows]) => (
          <section key={fam} className="rr-family">
            <h3 className="micro-label rr-family-head">
              {fam.replaceAll('_', ' ')} · {rows.length}
            </h3>
            <ul className="rr-rows">
              {rows.map(row => (
                <li key={row.recipe_id}>
                  <button
                    type="button"
                    className={row.recipe_id === selectedId ? 'rr-row rr-row--active' : 'rr-row'}
                    onClick={() => setSelectedId(row.recipe_id)}
                  >
                    <span className="rr-row-label">{row.display_label}</span>
                    <span className="rr-row-meta">
                      <span className="mono rr-row-id">{row.recipe_id}</span>
                      <span
                        className={`rr-chip rr-chip--${validityState(row)}`}
                        data-state={validityState(row)}
                      >
                        {validityLabel(row)}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </aside>

      <section className="rr-detail" aria-label="Recipe under review">
        {!selectedId && (
          <div className="callout">
            <div className="callout-body">
              <strong>Select a recipe to review.</strong>
              <p>
                The definition, its required reviewer roles, and its full decision history load
                here. Approval is revision-specific: any change to a definition stales every
                recorded approval by construction.
              </p>
            </div>
          </div>
        )}
        {selectedId && detailError && (
          <div className="callout callout--danger">
            <div className="callout-body"><p>{detailError}</p></div>
          </div>
        )}
        {selectedId && !detailError && (!detail || !reviews) && (
          <p className="hint">Loading {selectedId}…</p>
        )}
        {detail && reviews && (
          <RecipeDetailPanel
            detail={detail}
            reviews={reviews}
            formRole={formRole}
            setFormRole={setFormRole}
            formDecision={formDecision}
            setFormDecision={setFormDecision}
            rationale={rationale}
            setRationale={setRationale}
            submitting={submitting}
            submitError={submitError}
            submitStale={submitStale}
            recorded={recorded}
            onSubmit={submit}
            onReload={() => void loadSelected(selectedId)}
          />
        )}
      </section>
    </div>
  )
}

function RecipeDetailPanel(props: {
  detail: RecipeDetail
  reviews: RecipeReviews
  formRole: string
  setFormRole: (r: string) => void
  formDecision: RecipeReviewDecision
  setFormDecision: (d: RecipeReviewDecision) => void
  rationale: string
  setRationale: (r: string) => void
  submitting: boolean
  submitError: string
  submitStale: boolean
  recorded: string
  onSubmit: () => void
  onReload: () => void
}) {
  const { detail, reviews } = props
  const recipe = detail.recipe
  const validity = reviews.validity

  // The newest event per role AT THIS REVISION, for the checklist — same projection the
  // validity fold uses (events arrive oldest-first, so later entries win).
  const byRole = new Map<string, (typeof reviews.events)[number]>()
  for (const event of reviews.events) {
    if (event.recipe_revision_hash === detail.recipe_revision_hash) {
      byRole.set(event.reviewer_role, event)
    }
  }

  const canSubmit = props.formRole !== '' && props.rationale.trim() !== '' && !props.submitting

  return (
    <div className="rr-panel">
      <header className="rr-head">
        <h2>{recipe.output.display_label}</h2>
        <p className="mono rr-head-id">{recipe.recipe_id}</p>
        <div className="rr-head-chips">
          <span className="badge">{recipe.family.replaceAll('_', ' ')}</span>
          <span className="badge">{recipe.readiness}</span>
          <span className="badge">{recipe.computation_kind.replaceAll('_', ' ')}</span>
          {recipe.leakage.classification !== 'standard' && (
            <span className="badge sensitivity">
              leakage: {recipe.leakage.classification.replaceAll('_', ' ')}
            </span>
          )}
        </div>
        <p className="hint">
          Revision <span className="mono" title={detail.recipe_revision_hash}>
            {shortHash(detail.recipe_revision_hash)}
          </span>
          {' '}· revision {recipe.revision} · approval is specific to this exact definition
        </p>
      </header>

      <section className="rr-section">
        <h3 className="micro-label">Definition</h3>
        <p>{recipe.business_definition}</p>
        {recipe.decision_context && <p className="hint">{recipe.decision_context}</p>}
        {recipe.conceptual_reason && (
          <p className="hint">Conceptual only: {recipe.conceptual_reason}</p>
        )}
      </section>

      <section className="rr-section" aria-label="Required approvals">
        <h3 className="micro-label">Required approvals at this revision</h3>
        <ul className="rr-roles">
          {validity.required_roles.map(role => {
            const event = byRole.get(role)
            const state = event === undefined
              ? 'missing' : event.decision === 'approved' ? 'approved' : 'blocked'
            return (
              <li key={role} className={`rr-role rr-role--${state}`}>
                <span className="rr-role-name">{role.replaceAll('_', ' ')}</span>
                <span className="rr-role-state">
                  {event === undefined
                    ? 'no decision yet'
                    : `${event.decision.replaceAll('_', ' ')} — ${event.reviewer}`}
                </span>
              </li>
            )
          })}
        </ul>
        {validity.single_identity_violation && (
          <p className="field-error">
            Every required role was signed by one identity — a multi-role recipe needs at least
            two distinct reviewers.
          </p>
        )}
        {validity.current && (
          <p className="rr-current">Review is current: every required role approved this revision.</p>
        )}
      </section>

      <section className="rr-section">
        <h3 className="micro-label">Output</h3>
        <div className="rr-kvs">
          <Kv label="Type" value={`${recipe.output.output_type} · ${recipe.output.unit_kind}`} />
          <Kv label="Additivity" value={recipe.output.additivity} />
          <Kv label="Null inputs" value={orNotSet(recipe.output.null_input_policy)} />
          <Kv label="Empty population" value={orNotSet(recipe.output.empty_population_policy)} />
          {recipe.output.unit_kind === 'monetary' && (
            <Kv label="Currency policy" value={orNotSet(recipe.output.currency_policy)} />
          )}
          {recipe.output.unit_kind === 'ratio' && (
            <Kv label="Zero denominator" value={orNotSet(recipe.output.zero_denominator_policy)} />
          )}
          <Kv label="Grain" value={`${recipe.source_grain} → ${recipe.output_grain}`} mono />
          <Kv label="Objective" value={recipe.primary_objective} mono />
        </div>
      </section>

      <section className="rr-section">
        <h3 className="micro-label">Operands · {recipe.operands.length}</h3>
        <ul className="rr-operands">
          {recipe.operands.map(op => (
            <li key={op.role} className="rr-operand">
              <span className="mono rr-operand-role">{op.role}</span>
              <span className="rr-operand-facts">
                {op.concept}
                {op.economic_role ? ` · economic role: ${op.economic_role}` : ''}
                {op.status_policy_ref ? ` · ${op.status_policy_ref}` : ''}
                {op.required ? '' : ' · optional'}
              </span>
            </li>
          ))}
        </ul>
      </section>

      <section className="rr-section">
        <h3 className="micro-label">Temporal contract</h3>
        <div className="rr-kvs">
          <Kv label="Anchor" value={recipe.temporal.anchor_kind.replaceAll('_', ' ')} />
          <Kv
            label="Window"
            value={recipe.temporal.window_parameter
              ? `${recipe.temporal.window_parameter} (${recipe.temporal.window_unit})`
              : 'not set'}
          />
          <Kv label="Cutoff" value={recipe.temporal.cutoff_inclusivity} />
          <Kv label="Late arrivals" value={orNotSet(recipe.temporal.late_arrival_policy)} />
          {recipe.temporal.snapshot_policy && (
            <Kv label="Snapshot" value={recipe.temporal.snapshot_policy} />
          )}
        </div>
      </section>

      <section className="rr-section">
        <h3 className="micro-label">Eligibility &amp; policies</h3>
        <div className="rr-kvs">
          <Kv label="Included" value={orNotSet(recipe.eligibility.included)} />
          <Kv label="Excluded" value={orNotSet(recipe.eligibility.excluded)} />
        </div>
        {recipe.eligibility.policy_refs.length > 0 && (
          <ul className="rr-policies">
            {recipe.eligibility.policy_refs.map(ref => (
              <li key={ref} className="mono rr-policy">{ref}</li>
            ))}
          </ul>
        )}
        {recipe.leakage.classification !== 'standard' && (
          <p className="hint">
            Permitted stages: {recipe.leakage.permitted_stages.join(', ') || 'none'} ·
            prohibited: {recipe.leakage.prohibited_stages.join(', ') || 'none'}
          </p>
        )}
      </section>

      <section className="rr-section">
        <h3 className="micro-label">Computation</h3>
        {recipe.formula ? (
          <div className="rr-kvs">
            <Kv label="Formula schema" value={recipe.formula.formula_schema_version} mono />
            <Kv label="Expectation" value={recipe.formula.expectation_ref} mono />
            <Kv label="Result class" value={recipe.formula.result_class} />
          </div>
        ) : (
          <p className="hint">
            No executable formula — {recipe.computation_kind === 'governed_model_output'
              ? `a governed model output (${recipe.model_feature_ref || 'model spec not set'})`
              : 'this recipe is not executable at its current readiness'}.
          </p>
        )}
      </section>

      <section className="rr-section rr-decide" aria-label="Record a decision">
        <h3 className="micro-label">Record a decision</h3>
        <p className="hint">
          Recorded against revision <span className="mono">{shortHash(detail.recipe_revision_hash)}</span>{' '}
          under your session identity. Needs the governance role (platform-admin in the dev
          session).
        </p>
        <div className="rr-form">
          <select
            aria-label="Reviewer role"
            value={props.formRole}
            onChange={e => props.setFormRole(e.target.value)}
          >
            <option value="">Reviewing as…</option>
            {validity.required_roles.map(role => (
              <option key={role} value={role}>{role.replaceAll('_', ' ')}</option>
            ))}
          </select>
          <select
            aria-label="Decision"
            value={props.formDecision}
            onChange={e => props.setFormDecision(e.target.value as RecipeReviewDecision)}
          >
            {RECIPE_REVIEW_DECISIONS.map(d => (
              <option key={d} value={d}>{DECISION_LABEL[d]}</option>
            ))}
          </select>
          <textarea
            aria-label="Rationale"
            placeholder="Why — the rationale is part of the audit record"
            value={props.rationale}
            onChange={e => props.setRationale(e.target.value)}
            rows={3}
          />
          <div className="rr-form-actions">
            <button
              type="button"
              className="btn btn--primary"
              disabled={!canSubmit}
              onClick={props.onSubmit}
            >
              {props.submitting ? 'Recording…' : 'Record decision'}
            </button>
            {props.recorded && <span className="rr-recorded">{props.recorded}</span>}
          </div>
          {props.submitError && (
            <div className="callout callout--danger">
              <div className="callout-body">
                <p>{props.submitError}</p>
                {props.submitStale && (
                  <button type="button" className="btn" onClick={props.onReload}>
                    Reload the current definition
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      </section>

      <section className="rr-section" aria-label="Decision history">
        <h3 className="micro-label">Decision history · {reviews.events.length}</h3>
        {reviews.events.length === 0 && (
          <p className="hint">No decisions recorded yet — unreviewed is the starting state.</p>
        )}
        <ul className="rr-events">
          {[...reviews.events].reverse().map(event => (
            <li key={event.event_id} className="rr-event">
              <span className="rr-event-head">
                <strong>{event.decision.replaceAll('_', ' ')}</strong>
                {' — '}{event.reviewer_role.replaceAll('_', ' ')} · {event.reviewer}
                {event.recipe_revision_hash !== detail.recipe_revision_hash && (
                  <span className="rr-chip rr-chip--stale" title={event.recipe_revision_hash}>
                    earlier revision {shortHash(event.recipe_revision_hash)}
                  </span>
                )}
              </span>
              {event.rationale && <span className="rr-event-why">{event.rationale}</span>}
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
