// Authoring a PREDICTION TARGET — a form, not a chatbot.
//
// A training label is normally CONSTRUCTED ("went non-performing within 90 days"), not stored, so
// the tool proposes a RULE and a person completes it. It fills what the catalog justifies and
// leaves blank what it cannot know: nothing profiles column values, so it cannot know whether a
// flag holds 'Performing' or 'P'. A confidently pre-filled wrong answer is worse than a blank,
// because people confirm defaults — so every blank here states WHY it is blank, in plain words.
//
// The concurrence surface is the SENTENCE, not the field list. Twelve fields get rubber-stamped; a
// statement of meaning gets read. The SQL sits beneath it for anyone who wants to see exactly how
// the label will be built.
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  describeTarget,
  listCatalogs,
  listTargetEntities,
  listTargets,
  previewTargetSql,
  proposeTarget,
  registerTarget,
} from '../api'
import type {
  ExistingTarget, RegisteredTarget, SelectableEntity, TargetDraft, VisibleCatalog,
} from '../api'

// Why a field was left blank, in the words a person can act on. The API's reason codes are a
// CLOSED set precisely so this mapping is total — an unrecognised code would render as a blank
// with no explanation, which is the thing the whole design exists to prevent.
const REASONS: Record<string, string> = {
  no_value_profile:
    'Nothing in the catalog records what this column contains, so the tool cannot guess its '
    + 'values. You have to say what they are.',
  business_choice:
    'Two defensible definitions exist here. Which one you mean is a business decision, not the '
    + 'tool’s to make.',
  population_choice:
    'Everyone, or only those who have not had the outcome yet? Those are different questions.',
  not_stated:
    'Your objective does not say, and there is no safe default — a wrong guess here changes the '
    + 'dataset silently.',
  not_in_catalog:
    'The column the tool named is not in this catalog, so it was dropped rather than trusted.',
}

type Kind = 'text' | 'number' | 'list' | 'bool' | 'select'

interface FieldSpec {
  key: string
  label: string
  kind: Kind
  options?: string[]
  hint?: string
}

const SHARED: FieldSpec[] = [
  { key: 'name', label: 'Label name', kind: 'text', hint: 'must start with tgt_' },
  { key: 'window_days', label: 'Window (days)', kind: 'number',
    hint: 'how far FORWARD of the as-of date the outcome is measured' },
  { key: 'as_of_frequency', label: 'Sampling frequency', kind: 'select',
    options: ['', 'daily', 'weekly', 'monthly', 'quarterly', 'single'],
    hint: 'which as-of dates the label is evaluated on — a different frequency is a different '
      + 'dataset' },
  { key: 'label_type', label: 'Label type', kind: 'select',
    options: ['', 'binary', 'count', 'amount'] },
  { key: 'operator', label: 'Operator', kind: 'select',
    options: ['', '>=', '>', '==', '!=', '<=', '<'], hint: 'binary labels only' },
  { key: 'threshold', label: 'Threshold', kind: 'number', hint: 'binary labels only' },
]

const CENSORING: FieldSpec = {
  key: 'require_full_window', label: 'Require the full window to be observable', kind: 'bool',
  hint: 'off means rows whose window runs past the end of history are INCLUDED, and labelled as '
    + 'though the outcome did not happen',
}

const SHAPE_FIELDS: Record<string, FieldSpec[]> = {
  state_change: [
    ...SHARED,
    { key: 'column_ref', label: 'Flag column', kind: 'text' },
    { key: 'from_values', label: 'Starting values', kind: 'list',
      hint: 'comma separated — rows in these states are the candidates' },
    { key: 'to_values', label: 'Outcome values', kind: 'list',
      hint: 'comma separated — moving into one of these is the outcome' },
    { key: 'population_filter', label: 'Population', kind: 'select',
      options: ['from_values', 'all'],
      hint: 'from_values excludes rows that ALREADY have the outcome' },
    { key: 'exclude_null_at_as_of', label: 'Drop rows whose state cannot be read', kind: 'bool' },
    CENSORING,
  ],
  event_window: [
    ...SHARED,
    { key: 'event_catalog', label: 'Event catalog', kind: 'text' },
    { key: 'event_table', label: 'Event table', kind: 'text' },
    { key: 'event_date_ref', label: 'Event date column', kind: 'text' },
    { key: 'join_left', label: 'Join key (anchor side)', kind: 'text' },
    { key: 'join_right', label: 'Join key (event side)', kind: 'text' },
    { key: 'aggregate', label: 'Aggregate', kind: 'select', options: ['count', 'sum'] },
    { key: 'measure_ref', label: 'Measure column', kind: 'text', hint: 'sum only' },
    { key: 'population_having', label: 'Population', kind: 'select', options: ['any', 'none'],
      hint: 'none asks who will START — it excludes anyone already doing this' },
    { key: 'population_lookback_days', label: 'Lookback (days)', kind: 'number',
      hint: 'how far back “already doing this” looks' },
    CENSORING,
  ],
}

// Stamped from the person's own choice and never editable here: they picked the entity, and the
// server looked up its spine. An editable grain only invites a disagreement the catalog check then
// rejects, with the person unable to see why.
const STAMPED = ['entity', 'anchor_catalog', 'grain_ref', 'as_of_ref']

// The conditions that decide WHICH EVENTS COUNT. Rendered separately because a filter is a nested
// object, not a scalar — and rendered at all because a label with none counts every row in the
// table. "Who will start transacting in FOREIGN CURRENCY" with no currency condition is a label
// that looks right and answers a wider question, which is the failure this whole screen is for.
interface EventFilter { column_ref: string; op: string; value: string }

const FILTER_OPS = ['==', '!=', '>', '>=', '<', '<=']

//: Long enough that typing a value is one request rather than one per character, short enough that
//: the sentence still reads as live feedback on what was just typed.
const DEBOUNCE_MS = 250

function asText(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (Array.isArray(value)) return value.join(', ')
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  return String(value)
}

function buildRule(shape: string, values: Record<string, string>): Record<string, unknown> {
  const rule: Record<string, unknown> = { shape }
  for (const key of STAMPED) rule[key] = values[key] ?? ''
  for (const spec of SHAPE_FIELDS[shape] ?? []) {
    const raw = values[spec.key] ?? ''
    if (spec.kind === 'list') {
      rule[spec.key] = raw.split(',').map(v => v.trim()).filter(Boolean)
    } else if (spec.kind === 'number') {
      // A blank number is ABSENT, not zero: `threshold: 0` would be a real threshold, and
      // `window_days: 0` is refused by the contract with a message about a positive window
      // rather than about a field nobody filled in.
      rule[spec.key] = raw === '' ? null : Number(raw)
    } else if (spec.kind === 'bool') {
      rule[spec.key] = raw !== 'false'
    } else {
      rule[spec.key] = raw === '' ? null : raw
    }
  }
  return rule
}

export function TargetLabelScreen() {
  const [catalogs, setCatalogs] = useState<VisibleCatalog[]>([])
  const [source, setSource] = useState('')
  const [entities, setEntities] = useState<SelectableEntity[] | null>(null)
  const [entity, setEntity] = useState('')
  const [hypothesis, setHypothesis] = useState('')

  const [draft, setDraft] = useState<TargetDraft | null>(null)
  const [existing, setExisting] = useState<ExistingTarget[]>([])
  const [proposalFailed, setProposalFailed] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const [filters, setFilters] = useState<EventFilter[]>([])
  const [comment, setComment] = useState('')
  const [description, setDescription] = useState('')

  const [sentence, setSentence] = useState<string | null>(null)
  const [incomplete, setIncomplete] = useState<string | null>(null)
  const [sql, setSql] = useState<string | null>(null)
  const [showSql, setShowSql] = useState(false)
  const [registered, setRegistered] = useState<RegisteredTarget[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState<string | null>(null)

  useEffect(() => {
    listCatalogs()
      .then(r => {
        setCatalogs(r.catalogs)
        setSource(current => current || (r.catalogs[0]?.source ?? ''))
      })
      .catch(() => setCatalogs([]))
  }, [])

  useEffect(() => {
    if (!source) return
    setEntities(null)
    listTargetEntities(source)
      .then(list => {
        setEntities(list)
        setEntity(list[0]?.entity ?? '')
      })
      .catch(() => setEntities([]))
  }, [source])

  useEffect(() => {
    if (!entity) return
    listTargets(entity).then(setRegistered).catch(() => setRegistered([]))
  }, [entity])

  const rule = useMemo(() => {
    if (!draft) return null
    const built = buildRule(draft.shape, values)
    if (draft.shape === 'event_window') {
      built.event_filters = filters.filter(f => f.column_ref.trim() && f.op)
    }
    return built
  }, [draft, values, filters])

  // The sentence and the SQL are recomputed from the CURRENT form, not from what was proposed —
  // a person approving a statement about a rule they have since edited is approving nothing.
  //
  // DEBOUNCED, because `rule` changes on every keystroke: typing "Non-performing" into one field
  // would otherwise be fourteen round trips, and the answers could land out of order. The `live`
  // flag discards a response whose request has already been superseded, so the form never shows a
  // sentence describing a rule the person has moved past.
  useEffect(() => {
    if (!rule) return
    let live = true
    const timer = setTimeout(() => {
      describeTarget(rule)
        .then(r => { if (live) { setSentence(r.reads_as); setIncomplete(r.incomplete) } })
        .catch(() => { if (live) setSentence(null) })
    }, DEBOUNCE_MS)
    return () => { live = false; clearTimeout(timer) }
  }, [rule])

  useEffect(() => {
    if (!rule || !showSql) return
    let live = true
    const timer = setTimeout(() => {
      previewTargetSql(rule)
        .then(r => { if (live) setSql(r.sql) })
        .catch(() => { if (live) setSql(null) })
    }, DEBOUNCE_MS)
    return () => { live = false; clearTimeout(timer) }
  }, [rule, showSql])

  const propose = useCallback(async () => {
    setBusy(true); setError(''); setProposalFailed(false); setDone(null)
    try {
      const result = await proposeTarget({ hypothesis, entity, catalog_source: source })
      setExisting(result.existing)
      setDraft(result.draft)
      setProposalFailed(result.draft === null)
      if (result.draft) {
        const seeded: Record<string, string> = {}
        for (const [key, value] of Object.entries(result.draft.fields)) {
          seeded[key] = asText(value)
        }
        for (const spec of SHAPE_FIELDS[result.draft.shape] ?? []) {
          if (!(spec.key in seeded)) seeded[spec.key] = spec.kind === 'bool' ? 'true' : ''
        }
        setValues(seeded)
        const proposedFilters = result.draft.fields.event_filters
        setFilters(Array.isArray(proposedFilters)
          ? (proposedFilters as EventFilter[]).map(f => ({
            column_ref: String(f.column_ref ?? ''), op: String(f.op ?? '=='),
            value: String(f.value ?? ''),
          }))
          : [])
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'the proposal failed')
    } finally {
      setBusy(false)
    }
  }, [hypothesis, entity, source])

  const blanks = draft?.needs_input ?? []
  const unfilled = blanks.filter(key => !(values[key] ?? '').trim())

  const submit = useCallback(async () => {
    if (!draft || !rule) return
    setBusy(true); setError('')
    try {
      const result = await registerTarget({
        rule,
        description,
        // What the TOOL proposed, kept beside what the person submitted. Without it nobody can
        // later tell which fields a person actually decided and which they accepted unchanged.
        proposed_draft: draft as unknown as Record<string, unknown>,
        author_comment: comment,
      })
      setDone(result.name)
      listTargets(entity).then(setRegistered).catch(() => undefined)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'registration failed')
    } finally {
      setBusy(false)
    }
  }, [draft, rule, description, comment, entity])

  return (
    <div className="tgt-screen">
      <section className="panel">
        <h2>What are you predicting?</h2>
        <div className="field">
          <label htmlFor="tgt-catalog">Catalog</label>
          <select
            id="tgt-catalog" value={source} onChange={e => setSource(e.target.value)}
          >
            {catalogs.map(c => <option key={c.source} value={c.source}>{c.source}</option>)}
          </select>
        </div>

        {entities !== null && entities.length === 0 ? (
          <p className="empty">
            {source} has no keyed spine table, so it cannot anchor a prediction target. A label
            needs one row per thing being predicted and a date to measure forward from.
          </p>
        ) : (
          <>
            <div className="field">
              <label htmlFor="tgt-entity">Entity</label>
              <select
                id="tgt-entity" value={entity} onChange={e => setEntity(e.target.value)}
              >
                {(entities ?? []).map(e => (
                  <option key={e.entity} value={e.entity}>
                    {e.entity} — one row per {e.entity} in {e.spine_table}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label htmlFor="tgt-hypothesis">What are you trying to predict?</label>
              <textarea
                id="tgt-hypothesis" rows={3} value={hypothesis}
                onChange={e => setHypothesis(e.target.value)}
                placeholder="which customers will go non-performing in the next quarter"
              />
            </div>

            <button
              type="button" className="btn btn--primary" disabled={busy || !hypothesis.trim() || !entity}
              onClick={propose}
            >
              Propose a target
            </button>
          </>
        )}
      </section>

      {existing.length > 0 && (
        <section className="panel">
          <h2>Already registered</h2>
          <p className="micro-label">
            Decisions the organisation has already made. Reusing one keeps two teams comparable;
            a new label with the same meaning and a different window quietly does not.
          </p>
          <ul className="rows">
            {existing.map(e => (
              <li key={e.name} className="row">
                <strong>{e.name}</strong> — {e.description} ({e.window_days} days)
              </li>
            ))}
          </ul>
        </section>
      )}

      {proposalFailed && (
        <section className="panel">
          <p className="empty">
            The tool could not propose a target for this objective. Nothing has been filled in —
            an empty form is a better answer than a guessed one.
          </p>
        </section>
      )}

      {draft && (
        <>
          <section className="panel">
            <h2>The proposed target</h2>
            <p className="micro-label">
              Shape: {draft.shape}. Filled where the catalog justifies it, blank where it does not.
            </p>

            <dl className="tgt-stamped">
              {STAMPED.map(key => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd>{values[key] || 'not set'}</dd>
                </div>
              ))}
            </dl>

            {(SHAPE_FIELDS[draft.shape] ?? []).map(spec => {
              const needs = blanks.includes(spec.key)
              const id = `tgt-${spec.key}`
              return (
                <div className={needs ? 'field field--needs-input' : 'field'} key={spec.key}>
                  <label htmlFor={id}>
                    {spec.label} <code className="micro-label">{spec.key}</code>
                  </label>
                  {spec.kind === 'select' ? (
                    <select
                      id={id} value={values[spec.key] ?? ''} aria-invalid={needs || undefined}
                      onChange={e => setValues(v => ({ ...v, [spec.key]: e.target.value }))}
                    >
                      {(spec.options ?? []).map(o => (
                        <option key={o} value={o}>{o === '' ? '— not set —' : o}</option>
                      ))}
                    </select>
                  ) : spec.kind === 'bool' ? (
                    <input
                      id={id} type="checkbox" checked={(values[spec.key] ?? 'true') !== 'false'}
                      onChange={e => setValues(
                        v => ({ ...v, [spec.key]: e.target.checked ? 'true' : 'false' }))}
                    />
                  ) : (
                    <input
                      id={id} type="text" value={values[spec.key] ?? ''}
                      aria-invalid={needs || undefined}
                      onChange={e => setValues(v => ({ ...v, [spec.key]: e.target.value }))}
                    />
                  )}
                  {needs && (
                    <p className="tgt-reason">{REASONS[draft.notes[spec.key]] ?? draft.notes[spec.key]}</p>
                  )}
                  {!needs && spec.hint && <p className="micro-label">{spec.hint}</p>}
                </div>
              )
            })}
          </section>

          {draft.shape === 'event_window' && (
            <section className="panel">
              <h2>Which events count</h2>
              {filters.length === 0 ? (
                <p className="empty">
                  No conditions, so this label counts every row in {String(values.event_table
                    || 'the event table')}. If you meant a particular currency, channel or product,
                  say so here — a label that counts everything looks right and answers a wider
                  question.
                </p>
              ) : (
                <p className="micro-label">
                  All conditions must hold. They apply to the outcome window AND to the prior
                  activity that decides who is a candidate.
                </p>
              )}
              {filters.map((f, i) => (
                <div className="field tgt-filter" key={i}>
                  <input
                    type="text" value={f.column_ref}
                    aria-label={`condition ${i + 1} column`} placeholder="public.table.column"
                    onChange={e => setFilters(list => list.map(
                      (x, j) => (j === i ? { ...x, column_ref: e.target.value } : x)))}
                  />
                  <select
                    value={f.op} aria-label={`condition ${i + 1} operator`}
                    onChange={e => setFilters(list => list.map(
                      (x, j) => (j === i ? { ...x, op: e.target.value } : x)))}
                  >
                    {FILTER_OPS.map(o => <option key={o} value={o}>{o}</option>)}
                  </select>
                  <input
                    type="text" value={f.value} aria-label={`condition ${i + 1} value`}
                    onChange={e => setFilters(list => list.map(
                      (x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                  />
                  <button
                    type="button" className="btn btn--ghost"
                    aria-label={`remove condition ${i + 1}`}
                    onClick={() => setFilters(list => list.filter((_, j) => j !== i))}
                  >
                    Remove
                  </button>
                </div>
              ))}
              <button
                type="button" className="btn"
                onClick={() => setFilters(list => [...list, { column_ref: '', op: '==', value: '' }])}
              >
                Add a condition
              </button>
            </section>
          )}

          <section className="panel">
            <h2>What this means</h2>
            {sentence ? <p className="tgt-sentence">{sentence}</p> : (
              <p className="empty">
                {incomplete
                  ? `Not a complete rule yet: ${incomplete}`
                  : 'Fill the remaining fields to see what this label would mean.'}
              </p>
            )}

            <button type="button" className="btn" onClick={() => setShowSql(s => !s)}>
              {showSql ? 'Hide the SQL' : 'Show the SQL that builds it'}
            </button>
            {showSql && (
              sql
                ? <pre className="tgt-sql">{sql}</pre>
                : <p className="empty">No logic to show until the rule is complete.</p>
            )}
          </section>

          <section className="panel">
            <h2>Register</h2>
            <div className="field">
              <label htmlFor="tgt-description">Description</label>
              <input
                id="tgt-description" type="text" value={description}
                onChange={e => setDescription(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="tgt-comment">
                Anything worth recording — why you changed what the tool proposed
              </label>
              <textarea
                id="tgt-comment" rows={2} value={comment}
                onChange={e => setComment(e.target.value)}
                placeholder="180 rather than 90 because the desk reviews quarterly"
              />
            </div>

            {unfilled.length > 0 && (
              <p className="empty">
                Still blank: {unfilled.join(', ')}. These are the fields the tool could not know,
                so they cannot be defaulted.
              </p>
            )}
            <button
              type="button" className="btn btn--primary"
              disabled={busy || unfilled.length > 0 || !sentence}
              onClick={submit}
            >
              Register this target
            </button>
            {done && <p className="hint" role="status">Registered {done}.</p>}
            {error && <p className="error" role="alert">{error}</p>}
          </section>
        </>
      )}

      {registered.length > 0 && (
        <section className="panel">
          <h2>Targets registered for {entity}</h2>
          <ul className="rows">
            {registered.map(t => (
              <li key={t.definition_id} className="row">
                <strong>{t.name}</strong> — {t.description} ({t.window_days} days, {t.label_type})
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
