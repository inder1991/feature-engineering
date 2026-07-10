// One guided feature-generation flow: a gates strip naming who holds each step of the loop, a
// goal + scope hero with two peer paths (Generate candidate sets through the engine, or Write
// definitions myself through the batch composer), set summary cards to compare strategy lenses,
// one shared candidate list, and a selection tray with an explicit approval confirm before
// anything registers.
//
// Invariants carried over from the hardening campaign:
// - Lineage comes ONLY from backend-resolved pairs (FeatureIdea.derives_pairs for generated
//   candidates, the drafted-against source snapshot for drafts), never from typed context.
// - registerFeature fires only after the explicit Confirm approval step, exactly once per
//   candidate (batch in-flight ref + per-candidate registered state).
// - Every fetch handler carries an out-of-order guard (monotonic sequence refs).
// - Scope edits invalidate candidates: source edits clear everything (draft snapshots no longer
//   match the context), entity/target edits clear generated candidates, sets, and rejections.
//
// Multi-set model decisions (documented for the record):
// - Generation always calls /features/recommend-sets. There is NO silent fallback to
//   /features/recommend on a 503: that status means no LLM provider is configured on the
//   deployment, so the plain endpoint would return the same 503; the honest notice renders
//   instead (never fake capability).
// - A response with one non-empty set renders the flat list exactly as before, no cards row.
// - Sets that came back empty are dropped from the compare row (nothing to take or compare);
//   their gauntlet rejections still show in the rejections panel.
// - Candidate identity within a round is the feature name: the same name in several sets is the
//   same feature on purpose (strong signals earn their place in several theses), so it renders
//   with an "In N sets" chip, selects globally, and registers once. Keys stay per-fetch
//   (g{seq}:{name}) so a later round reusing a name never resurrects registered state.
//   Accepted tradeoff: the backend does not GUARANTEE cross-lens name identity (two lenses could
//   in principle propose different definitions under one name); the first set's definition wins
//   the merge, and the row renders that one definition.
// - Set theses are client-side copy keyed by the router's fixed lens vocabulary (the wire
//   carries no set description); an unknown lens simply renders without a thesis line.
//
// Feedback channels (Phase 3 decisions, documented for the record):
// - Whole-round feedback re-calls /contract/considered-set with the ROUND's original hypothesis
//   and objective (both snapshotted at generate time; a later edit never silently rewrites what
//   feedback runs against) plus the current scope, which cannot have drifted (scope edits
//   invalidate the round), and the human's instruction as `feedback`. It mints a FRESH governing
//   intent over the guided set, so post-feedback candidates stay governable.
// - Pin semantics are client-side: candidates that are selected or registered stay (selected
//   pins get a Kept chip; registered rows are already their own mark), everything else is
//   replaced by the new response. A new candidate reusing a pinned name is dropped: the pin
//   wins, one row per name. Kept candidates leave the sets model (lenses cleared) so new set
//   cards count only their own features; kept rows stay visible in every set view. Drafts are
//   not part of the engine round and pass through untouched.
// - Both feedback channels stop at 3 rounds, then it is back in the human's hands. Scope edits
//   reset the counters with everything else; a fresh generate starts a fresh allowance.
// - Per-candidate feedback (refine) exists on generated rows only: a draft is the human's own
//   definition, revised by editing its line, not by asking the engine. Registered rows take no
//   feedback, and a revision that resolves after its candidate registered is dropped.
// - A refine revision is a SUGGESTION: it touches nothing until Approve revision, and even then
//   only the local candidate; registration still requires the tray's explicit confirm.
// - Simple mutual-exclusion rule with registration: while the tray is confirming or a register
//   batch is in flight, both feedback channels are disabled, Approve revision / Revert to
//   original are inert, and the generate path plus scope fields lock, so nothing can change a
//   row or pull it out of view while its registration is being written.
// - Scope edits bump the generation sequence so an in-flight round (generate or feedback) that
//   resolves after the edit is discarded, never applied against the new scope.
import { type FormEvent, type ReactNode, useEffect, useRef, useState } from 'react'
import {
  ApiError, type ConsideredSetResp, type FeatureFreshness, type FeatureIdea, type FeatureSpecIn,
  type JoinStep, type Recipe, type RecipeDisposition, type RecognitionCandidate,
  type RecognitionResp, type RefineRejection, type Rejection, type SetRecommendation,
  contractConfirm, contractConsideredSet, contractDraft, contractRecognitions, featureFreshness,
  featureRecipe, refineCandidate, registerFeature,
} from '../api'
import { getSession } from '../session'

// ---- Phase 1B feature flags -------------------------------------------------------------------
// Two independent UI flags, read via Vite's env idiom and default OFF: with both off the screen
// behaves EXACTLY as today (one-shot generate, no recognition call, no disposition lens). Read at
// call time (not cached at module scope) so tests can flip them with vi.stubEnv.
// - intent_confirmation_ui: on Generate, first recognise the objective and let the human
//   confirm/override/broaden the scope BEFORE the considered set is generated.
// - intent_disposition_lens: when a scoped response carries dispositions, group recipes by their
//   final disposition (only meaningful with the confirmation UI on).
function confirmationUiEnabled(): boolean {
  return import.meta.env.VITE_INTENT_CONFIRMATION_UI === '1'
}
function dispositionLensEnabled(): boolean {
  return import.meta.env.VITE_INTENT_DISPOSITION_LENS === '1'
}

// The disposition lens, in render order: each final_disposition mapped to its human heading.
const DISPOSITION_GROUPS: { key: RecipeDisposition['final_disposition']; heading: string }[] = [
  { key: 'eligible', heading: 'Recommended' },
  { key: 'unbuildable', heading: 'Relevant but missing data' },
  { key: 'safety_rejected', heading: 'Rejected by safety' },
  { key: 'out_of_scope', heading: 'Outside confirmed scope' },
]

// The reason a recipe landed in its group: the eligible tier, or the reason_codes of the stage
// that decided it (applicability for out-of-scope, grounding for unbuildable, safety for rejected).
function dispositionReason(d: RecipeDisposition): string {
  if (d.final_disposition === 'eligible') {
    return d.relevance_tier ? `${d.relevance_tier} relevance` : 'eligible'
  }
  const stage = d.final_disposition === 'out_of_scope'
    ? d.applicability
    : d.final_disposition === 'unbuildable'
      ? d.grounding
      : d.safety
  const codes = stage?.reason_codes ?? []
  return codes.length > 0 ? codes.join(', ') : d.final_disposition.replace(/_/g, ' ')
}

const HELP_STYLE = { fontSize: 12 } as const
// Solid ok chip (index.css has no fresh badge class; mirrors .badge.stale's solid treatment).
const OK_SOLID_CHIP_STYLE = {
  background: 'var(--ok-solid)', borderColor: 'transparent', color: 'var(--chip-ink)',
} as const
// Sticky-feel selection tray: the last row of the candidate list, pinned while the list scrolls.
const TRAY_STYLE = {
  position: 'sticky', bottom: 0, zIndex: 1, background: 'var(--surface)',
  borderBottomLeftRadius: 'var(--radius-panel)', borderBottomRightRadius: 'var(--radius-panel)',
  flexWrap: 'wrap', gap: 12,
} as const

const EXAMPLE_GOAL = 'predict churn'

// One definition per line. The newline-separated example teaches the batch shape at a glance.
const DESCRIBE_PLACEHOLDER =
  'One feature per line, e.g.\ntotal spend per customer over the last 90 days\ndays since last transaction'

const WARN_GLYPH = 'M8 2.5 1.5 13.25h13L8 2.5ZM8 6.75v3M8 12v.01'

// Plain-English thesis per router lens. Client-side copy: the wire carries lens + features only,
// and the lens vocabulary is fixed by the backend's deterministic router.
const LENS_THESES: Record<string, string> = {
  unary: 'Single-column transforms; flags, buckets, and scaled values of one column.',
  ratio: 'Ratios between numeric columns; how quantities relate, not how large they are.',
  aggregation: 'Aggregations over related rows via a join key; totals, counts, and averages.',
  temporal: 'Point-in-time and recency signals; how behavior moves over time.',
  distributional: 'Position within the peer group; how this entity compares to its cohort.',
}

// Both feedback channels stop here: 3 rounds of guidance, then it is back in the human's hands.
const FEEDBACK_ROUNDS = 3

// Human labels for gauntlet rejection codes. STALE reads "stale source"; every other code
// lowercases with spaces so even an unknown code from a newer backend reads as words.
const REJECT_LABELS: Record<string, string> = { STALE: 'stale source' }

function rejectLabel(code: string): string {
  return REJECT_LABELS[code] ?? code.toLowerCase().replace(/_/g, ' ')
}

// Display form of a router lens token: "temporal" -> "Temporal".
function lensLabel(lens: string): string {
  return lens.charAt(0).toUpperCase() + lens.slice(1)
}

function CalloutGlyph({ d }: { d: string }) {
  return (
    <span className="callout-glyph" aria-hidden="true">
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d={d} />
      </svg>
    </span>
  )
}

// Peer-path card icons (plus-in-circle for Generate, pencil for Write definitions). Decorative:
// the card title text carries the meaning.
function PathGlyph({ children }: { children: ReactNode }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  )
}

// Registered rows swap their checkbox for this ok mark; the "Registered <id>" text carries the
// state, so the glyph is decorative (color never works alone).
function CheckGlyph() {
  return (
    <span
      aria-hidden="true"
      style={{
        display: 'inline-flex', width: 32, height: 32, flex: 'none',
        alignItems: 'center', justifyContent: 'center', color: 'var(--ok)',
      }}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="m3 8.5 3.5 3.5L13 4.5" />
      </svg>
    </span>
  )
}

// ---------------------------------------------------------------- gates strip

type GateState = 'done' | 'active' | 'todo'

// Text form of each gate state for assistive tech: the visual encoding (check glyph, wash,
// dimming) never works alone.
const GATE_STATE_WORDS: Record<GateState, string> = {
  done: 'done', active: 'current step', todo: 'upcoming',
}

function Gate({ state, who, title, sub }: {
  state: GateState
  who: 'You' | 'Engine'
  title: string
  sub: string
}) {
  return (
    <div
      className="gate"
      role="listitem"
      data-state={state}
      aria-current={state === 'active' ? 'step' : undefined}
    >
      <span className={who === 'You' ? 'gate-who you' : 'gate-who engine'}>{who}</span>
      <div className="gate-title">
        {title}
        {state === 'done' && <span className="gate-check" aria-hidden="true">✓</span>}
      </div>
      <div className="gate-sub">{sub}</div>
      <span className="visually-hidden">{GATE_STATE_WORDS[state]}</span>
    </div>
  )
}

// ---------------------------------------------------------------- rejections panel

function RejectionsPanel({ rejections, open, onToggle }: {
  rejections: Rejection[]
  open: boolean
  onToggle: () => void
}) {
  const counts = new Map<string, number>()
  for (const r of rejections) {
    const label = rejectLabel(r.code)
    counts.set(label, (counts.get(label) ?? 0) + 1)
  }
  // Largest tally first; ties keep first-seen order (stable sort).
  const tallyLine = [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([label, n]) => `${label} ${n}`)
    .join(' · ')
  const n = rejections.length
  return (
    <div className="rej-panel">
      <div className="rej-line">
        <span className="badge rej-count tabular-nums">{n} rejected</span>
        <span>
          The safety gauntlet rejected {n} {n === 1 ? 'candidate' : 'candidates'} across all
          lenses: {tallyLine}.
        </span>
        <button
          type="button"
          className="rej-toggle"
          aria-expanded={open}
          aria-controls="wb-rej-list"
          onClick={onToggle}
        >
          {open ? 'Hide' : 'Show'}
        </button>
      </div>
      {open && (
        <ul className="rej-list" id="wb-rej-list">
          {rejections.map((r, i) => (
            <li key={`${i}:${r.name}`}>
              <code>{r.name}</code>
              <span className="badge rejected">{rejectLabel(r.code)}</span>
              <span className="rej-why">{r.reason}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// Uploaded cardinality is unvalidated free text. Only a normalized N:1 or 1:1 hop is known-safe;
// anything else ('1:N', '1:n', 'one_to_many', ...) can multiply rows, and a missing value means
// fan-out cannot be ruled out.
const SAFE_CARDINALITIES = new Set(['N:1', '1:1'])

function normalizeCardinality(raw: string | null): string | null {
  const value = raw?.trim().toUpperCase()
  return value ? value : null
}

function stepFansOut(raw: string | null): boolean {
  const norm = normalizeCardinality(raw)
  return norm !== null && !SAFE_CARDINALITIES.has(norm)
}

// Suggested feature name for a draft: a slug of the description, editable before selection.
function slugFrom(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 63)
}

// Candidate identity within a round is the feature name (the same name in several sets is the
// same feature); keys carry the fetch sequence (g{seq}:{name}) because LLM-chosen names are not
// unique across rounds, so keying registered state by name alone would show phantom "Registered"
// on a fresh, never-registered candidate that reuses an old name.
interface GeneratedCandidate {
  kind: 'generated'
  key: string
  idea: FeatureIdea
  // Every lens whose set holds this feature, in set order. Length > 1 renders the
  // "In N sets" chip.
  lenses: string[]
  // Pinned through a whole-round feedback regeneration (it was selected or registered when the
  // round replaced everything else). Kept rows show in every set view and leave the sets model.
  kept?: boolean
}

// One recorded whole-round feedback submission: who asked, what they asked, what it did.
interface SetFeedbackRecord {
  round: number
  user: string
  instruction: string
  // Selected (unregistered) pins that were kept; registered rows persist as their own record.
  kept: number
  replaced: number
}

// Per-candidate refine channel state, keyed by candidate key.
interface RefineState {
  open: boolean
  instruction: string
  busy: boolean
  // Rounds consumed (a gauntlet rejection consumes its round too; a transport error does not).
  rounds: number
  // The engine's revision awaiting the human's Approve revision / Revert to original call.
  pending: FeatureIdea | null
  pendingRound: number
  pendingInstruction: string
  rejection: RefineRejection | null
  error: string | null
  // Round number of the last APPROVED revision; renders the "Revised · R<n>" chip.
  appliedRound: number | null
}

const EMPTY_REFINE: RefineState = {
  open: false, instruction: '', busy: false, rounds: 0, pending: null, pendingRound: 0,
  pendingInstruction: '', rejection: null, error: null, appliedRound: null,
}

// Display form of backend-resolved lineage pairs; shared by the row facts and the refine diff.
function fmtPairs(pairs: [string, string][]): string {
  return pairs.map(([source, ref]) => `${source}:${ref}`).join(', ') || 'none'
}

// One field of the recorded revision diff: old struck through, new inserted, or the honest
// "unchanged" marker. <del>/<ins> carry the change semantics for assistive tech; strikethrough
// plus ordering keep the encoding non-color.
function DiffLine({ label, before, after }: { label: string; before: string; after: string }) {
  return (
    <p className="diff-line">
      <span className="diff-label">{label}</span>{' '}
      {before === after ? (
        <span className="diff-unchanged">unchanged</span>
      ) : (
        <>
          <del>{before}</del> <span aria-hidden="true">→</span> <ins>{after}</ins>
        </>
      )}
    </p>
  )
}

// A described feature drafted through /features/recipe. Recipes are single-catalog by API
// contract, so the source it was drafted against is snapshotted and registration lineage uses
// [snapshotSource, ref] pairs; the live source field may have changed since.
interface DraftCandidate {
  kind: 'draft'
  key: string
  name: string
  description: string
  recipe: Recipe
  snapshotSource: string
}

type Candidate = GeneratedCandidate | DraftCandidate

interface Registration {
  id: string
  freshness: FeatureFreshness | null
}

function specFor(candidate: Candidate): FeatureSpecIn {
  if (candidate.kind === 'generated') {
    const { idea } = candidate
    return {
      name: idea.name, description: idea.description, grain_table: idea.grain_table,
      aggregation: idea.aggregation, as_of_column: null,
      // Lineage comes from the pairs the backend resolved at recommend time, never from the
      // typed source context: the typed source can differ from where a derive actually lives.
      derives_from: idea.derives_pairs.map(([catalog_source, object_ref]) => ({
        catalog_source, object_ref })),
    }
  }
  const { recipe } = candidate
  return {
    name: candidate.name.trim(), description: candidate.description,
    grain_table: recipe.grain_table, aggregation: recipe.aggregation,
    as_of_column: recipe.as_of_column,
    // Recipes are single-catalog: every derive lives in the snapshotted source.
    derives_from: recipe.derives_from.map(object_ref => ({
      catalog_source: candidate.snapshotSource, object_ref })),
  }
}

function JoinPathDetails({ steps }: { steps: JoinStep[] }) {
  if (steps.length === 0) return null
  const fansOut = steps.some(s => stepFansOut(s.cardinality))
  const hasUnknownHop = steps.some(s => normalizeCardinality(s.cardinality) === null)
  return (
    <details>
      <summary style={{ cursor: 'pointer', padding: '6px 0', fontWeight: 500, color: 'var(--ink-soft)' }}>
        Join path ({steps.length} {steps.length === 1 ? 'hop' : 'hops'})
      </summary>
      <ol className="mono" style={{ margin: '8px 0 0', paddingLeft: 22, display: 'grid', gap: 4 }}>
        {steps.map(s => (
          <li key={`${s.from_ref}->${s.to_ref}`}>
            {s.from_ref} → {s.to_ref}{' '}
            <span
              style={
                stepFansOut(s.cardinality)
                  ? { color: 'var(--warn)', fontWeight: 600 }
                  : { color: 'var(--ink-soft)' }
              }
            >
              ({normalizeCardinality(s.cardinality) === null
                ? 'cardinality unknown'
                : s.cardinality})
            </span>
          </li>
        ))}
      </ol>
      {hasUnknownHop && (
        <p className="hint" style={{ marginTop: 8 }}>
          Cardinality is missing on at least one hop, so fan-out cannot be ruled out. Confirm the
          join direction before registering this feature.
        </p>
      )}
      {fansOut && (
        <div className="callout callout--warn">
          <CalloutGlyph d={WARN_GLYPH} />
          <div className="callout-body">
            <p>
              <strong>Fan-out.</strong> A one-to-many hop multiplies rows. Aggregate before
              joining or the feature will double-count.
            </p>
          </div>
        </div>
      )}
    </details>
  )
}

export function WorkbenchScreen() {
  const [goal, setGoal] = useState('')
  const [hypothesis, setHypothesis] = useState('')
  // The server-side intent that will later govern these candidates into a signed contract. Set
  // on a successful generate; dropped by clearSets on any invalidation (scope edit or error).
  const [intentId, setIntentId] = useState<string | null>(null)
  const [source, setSource] = useState('')
  const [entity, setEntity] = useState('')
  const [target, setTarget] = useState('')
  const [generated, setGenerated] = useState<GeneratedCandidate[] | null>(null)
  // Ordered lenses of the last round's non-empty sets. Two or more render the compare cards;
  // one (or zero) renders the flat single list exactly as before the sets model.
  const [setLenses, setSetLenses] = useState<string[]>([])
  const [recommendation, setRecommendation] = useState<SetRecommendation | null>(null)
  const [rejections, setRejections] = useState<Rejection[]>([])
  const [rejectionsOpen, setRejectionsOpen] = useState(false)
  // Which set's features the one detail list shows (multi-set rounds only).
  const [activeLens, setActiveLens] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<DraftCandidate[]>([])
  // GLOBAL selection across set views: candidate key -> the lens it was picked from (null for
  // drafts and flat-list picks). Keys carry the fetch sequence, so cleared rounds stay inert.
  const [selected, setSelected] = useState<Record<string, string | null>>({})
  const [registered, setRegistered] = useState<Record<string, Registration>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [screenedTarget, setScreenedTarget] = useState<string | null>(null)
  const [scopeChanged, setScopeChanged] = useState(false)
  const [describeOpen, setDescribeOpen] = useState(false)
  const [describeText, setDescribeText] = useState('')
  // Per-line draft failures, "Line N: <detail>", shown in the composer while the successful
  // lines still draft. Distinct from the top notice, which carries deployment-level facts.
  const [draftErrors, setDraftErrors] = useState<string[]>([])
  const [generating, setGenerating] = useState(false)
  const [drafting, setDrafting] = useState(false)
  const [confirmingBatch, setConfirmingBatch] = useState(false)
  const [batchBusy, setBatchBusy] = useState(false)
  // Govern path, symmetric with register: which candidates minted a signed contract (contract
  // id + version), whether the tray is confirming a govern, and whether a govern batch is running.
  const [governed, setGoverned] = useState<Record<string, { contractId: string; version: number }>>({})
  const [confirmingGovern, setConfirmingGovern] = useState(false)
  const [governBusy, setGovernBusy] = useState(false)
  const [notice, setNotice] = useState('')
  // ---- Phase 1B: Gate #1 scope confirmation (behind intent_confirmation_ui) ----
  // The recognizer's attempt for the current round: non-null puts the screen in the confirm step
  // (proposed scope shown, no candidates yet), null means today's flow. The working scope the human
  // edits before confirming: the chosen primary use-case, the kept secondaries, and whether to
  // include descendant sub-use-cases (exact ↔ include_descendants).
  const [recognition, setRecognition] = useState<RecognitionResp | null>(null)
  const [scopePrimary, setScopePrimary] = useState<string | null>(null)
  const [scopeSecondary, setScopeSecondary] = useState<string[]>([])
  const [scopeExpansion, setScopeExpansion] = useState<'exact' | 'include_descendants'>('exact')
  // The scoped considered-set's per-recipe dispositions (the lens) and the scope id the last scoped
  // run was governed by — the prior scope a broaden supersedes. Both null on the unscoped path.
  const [dispositions, setDispositions] = useState<RecipeDisposition[] | null>(null)
  const [lastScopeId, setLastScopeId] = useState<string | null>(null)
  // Whole-round feedback channel: the hypothesis and objective the round was generated for
  // (feedback reruns THOSE, not a since-edited input), the instruction being typed, rounds
  // consumed, the recorded strips, and the in-flight flag.
  const [roundHypothesis, setRoundHypothesis] = useState('')
  const [roundObjective, setRoundObjective] = useState('')
  const [setFbInstruction, setSetFbInstruction] = useState('')
  const [setFbRounds, setSetFbRounds] = useState(0)
  const [setFbRecords, setSetFbRecords] = useState<SetFeedbackRecord[]>([])
  const [setFbBusy, setSetFbBusy] = useState(false)
  // Per-candidate refine channel, keyed by candidate key (cleared with its candidates).
  const [refines, setRefines] = useState<Record<string, RefineState>>({})
  // Out-of-order guards: only the latest request per handler may apply its response.
  const generateSeq = useRef(0)
  const draftSeq = useRef(0)
  // Reentry guard for the whole-round feedback call (one flight at a time, exactly once).
  const setFbInFlight = useRef(false)
  // Reentry guard per candidate for refine calls.
  const refineInFlight = useRef(new Set<string>())
  // Reentry guard for the draft batch: a second submit while a batch is in flight is a no-op,
  // so each line's recipe fires exactly once even before the disabled attribute lands.
  const draftInFlight = useRef(false)
  // Reentry guard for the register batch: state updates are async, so a double click on
  // Confirm approval could otherwise start two batches before the disabled attribute lands.
  const batchInFlight = useRef(false)
  // Reentry guard for the govern batch, mirroring batchInFlight: a double click on Confirm
  // govern must never mint two contracts for one candidate.
  const governInFlight = useRef(false)
  // Element id to focus after the next render (house pattern from ReviewQueueScreen): Approve
  // revision / Revert to original unmount the button that held focus, so without an explicit
  // move keyboard focus falls back to <body>.
  const focusTarget = useRef<string | null>(null)
  // Latest-state mirrors (assigned every render) so async arrivals decide against CURRENT
  // state, not their submit-time closure: feedback pins read the selection as it stands when
  // the response lands, and a refine result checks whether its row registered meanwhile.
  const generatedRef = useRef(generated)
  generatedRef.current = generated
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  const registeredRef = useRef(registered)
  registeredRef.current = registered

  useEffect(() => {
    if (!focusTarget.current) return
    const el = document.getElementById(focusTarget.current)
    focusTarget.current = null
    el?.focus()
  })

  // Phase 1B flags, read each render so a test can flip them with vi.stubEnv. Default OFF → today.
  const confirmationUi = confirmationUiEnabled()
  const dispositionLens = dispositionLensEnabled()
  // The recognizer's proposals as the working scope holds them: the chosen primary and the kept
  // secondaries, resolved back to their candidate records for display (name/confidence/evidence).
  const primaryCandidate =
    recognition?.candidates.find(c => c.use_case_id === scopePrimary) ?? null
  const secondaryCandidates: RecognitionCandidate[] = scopeSecondary
    .map(id => recognition?.candidates.find(c => c.use_case_id === id))
    .filter((c): c is RecognitionCandidate => c != null)

  const multiSet = setLenses.length > 1
  // Every live candidate, across ALL sets plus drafts: selection and registration always work
  // over this, so picks survive set switching and a batch registers whatever is selected even
  // when another set's view is showing.
  const allCandidates: Candidate[] = [...(generated ?? []), ...drafts]
  // What the one detail list shows: the active set's features (multi-set rounds) or every
  // generated candidate (flat rounds), plus drafts in both cases. Kept pins belong to no new
  // set, so they show in every view.
  const visibleGenerated = multiSet && activeLens !== null
    ? (generated ?? []).filter(c => c.kept === true || c.lenses.includes(activeLens))
    : generated ?? []
  const listCandidates: Candidate[] = [...visibleGenerated, ...drafts]
  // One definition per non-empty line: the button label and its gating read this directly.
  const draftLines = describeText.split('\n').map(line => line.trim()).filter(Boolean)
  // Only generated candidates pass the design gauntlet, so the design-checked explanation
  // appears only when the list holds at least one generated candidate.
  const hasGenerated = (generated?.length ?? 0) > 0
  // Selection is the intersection of the map and the live candidate list: keys from cleared
  // rounds are inert, and registered candidates can never re-enter a batch.
  const selectedCandidates = allCandidates.filter(
    c => c.key in selected && !registered[c.key] && !governed[c.key])
  const selectedCount = selectedCandidates.length
  // Only fresh, unrefined generated candidates are governable: they came through the CURRENT intent's
  // considered set AND still match its persisted snapshot. A draft is the human's own definition; a
  // KEPT candidate was pinned from a PRIOR generation; and a REFINED candidate's idea was mutated in
  // place (approveRevision) so it no longer matches the snapshot the server reconstructs the choice
  // from — governing it would 422 or silently mint a contract from the pre-refine data. None govern.
  const governableCount = selectedCandidates.filter(
    c => c.kind === 'generated' && !c.kept && (refines[c.key]?.appliedRound ?? null) === null).length
  // Distinct set origins of the current picks, for the tray's mix note.
  const originLenses = [...new Set(
    selectedCandidates
      .map(c => selected[c.key])
      .filter((lens): lens is string => typeof lens === 'string'),
  )]

  // The one mutual-exclusion rule between feedback and registration: while the tray is
  // confirming approval or a register batch is in flight, both feedback channels disable,
  // Approve revision / Revert to original are inert, the generate path locks, and scope edits
  // are inert. A revision must never race what the human is about to write into the registry,
  // and a registration must never complete invisibly after its row leaves view.
  const feedbackLocked = confirmingBatch || batchBusy || confirmingGovern || governBusy
  const setFbExhausted = setFbRounds >= FEEDBACK_ROUNDS

  // Gates advance with real state, never decoratively. The brief needs BOTH the hypothesis and the
  // goal (generate() requires both), so the gate must not flip to 'done' — and cascade gate2 to
  // 'active' — while a required field is still empty.
  const goalDone = goal.trim() !== '' && hypothesis.trim() !== ''
  const haveCandidates = allCandidates.length > 0
  const anyRegistered = allCandidates.some(c => registered[c.key] !== undefined)
  const gate1: GateState = goalDone ? 'done' : 'active'
  const gate2: GateState = haveCandidates ? 'done' : goalDone ? 'active' : 'todo'
  const gate3: GateState = !haveCandidates
    ? 'todo'
    : selectedCount > 0 || anyRegistered ? 'done' : 'active'
  const gate4: GateState = selectedCount > 0 ? 'active' : anyRegistered ? 'done' : 'todo'

  function fail(err: unknown) {
    setNotice(
      err instanceof ApiError && err.status === 503
        ? 'AI assist is not configured on this deployment: no LLM provider is enabled.'
        : err instanceof ApiError
          ? err.detail
          : String(err),
    )
  }

  function clearSets() {
    setSetLenses([])
    setRecommendation(null)
    setActiveLens(null)
    setRejections([])
    setRejectionsOpen(false)
    // Drop any stale governance intent: the candidates it governed no longer exist.
    setIntentId(null)
  }

  // Resets both feedback channels: round counters, recorded strips, typed instructions, and
  // every per-candidate refine state. Runs with every path that replaces or clears the round.
  function clearFeedback() {
    setSetFbRounds(0)
    setSetFbRecords([])
    setSetFbInstruction('')
    setSetFbBusy(false)
    setRefines({})
  }

  // A scope edit voids any in-flight round (generate or whole-round feedback): bump the
  // sequence so a late response is discarded instead of applying against the new scope, and
  // release the busy flags the discarded flights can no longer clear.
  function voidInFlightRounds() {
    generateSeq.current += 1
    setGenerating(false)
    // A scope edit also drops the Gate #1 confirm step and any scoped disposition lens: the
    // recognised scope was for the previous scope context. (Both no-ops when the flags are off.)
    setRecognition(null)
    setDispositions(null)
  }

  function changeSource(value: string) {
    setSource(value)
    // Generated candidates were produced for the previous source context, and draft snapshots
    // no longer match it either: a source edit clears everything.
    const hadCandidates = allCandidates.length > 0
    voidInFlightRounds()
    setGenerated(null)
    setDrafts([])
    setSelected({})
    setRegistered({})
    setGoverned({})
    setErrors({})
    setDraftErrors([])
    setScreenedTarget(null)
    setConfirmingBatch(false)
    setConfirmingGovern(false)
    clearSets()
    clearFeedback()
    if (hadCandidates) setScopeChanged(true)
  }

  // Entity and target edits invalidate generated candidates and their round's sets and
  // rejections (they were gathered and screened for the previous scope). Drafts survive: their
  // snapshot source is unchanged.
  function invalidateGenerated() {
    const hadGenerated = (generated?.length ?? 0) > 0
    voidInFlightRounds()
    setGenerated(null)
    setSelected({})
    setScreenedTarget(null)
    setConfirmingBatch(false)
    setConfirmingGovern(false)
    clearSets()
    clearFeedback()
    if (hadGenerated) setScopeChanged(true)
  }

  function changeEntity(value: string) {
    setEntity(value)
    invalidateGenerated()
  }

  function changeTarget(value: string) {
    setTarget(value)
    invalidateGenerated()
  }

  // Apply a considered-set response as the current round: dedupe candidates by name across sets,
  // open the detail list on the advisory pick, and reset the feedback cycle against THIS round's
  // hypothesis/objective. Shared by the one-shot generate (flag off) and the confirmed/broadened
  // scoped generate (flag on). Callers run the out-of-order guard BEFORE calling.
  function applyConsideredRound(
    cs: ConsideredSetResp, seq: number, roundHyp: string, roundObj: string,
  ) {
    setIntentId(cs.intent_id)
    // Dedupe by name across sets: the same feature in several lenses is one candidate that
    // knows every set it belongs to. Empty sets are dropped (nothing to compare or take).
    const byName = new Map<string, GeneratedCandidate>()
    const lenses: string[] = []
    for (const set of cs.alternatives) {
      if (set.features.length === 0) continue
      lenses.push(set.lens)
      for (const idea of set.features) {
        const existing = byName.get(idea.name)
        if (existing) {
          if (!existing.lenses.includes(set.lens)) existing.lenses.push(set.lens)
        } else {
          byName.set(idea.name, {
            kind: 'generated', key: `g${seq}:${idea.name}`, idea, lenses: [set.lens],
          })
        }
      }
    }
    setGenerated([...byName.values()])
    setSetLenses(lenses)
    setRecommendation(cs.recommendation)
    // The detail list opens on the advisory pick when there is one among the surviving sets.
    setActiveLens(
      lenses.length > 1
        ? cs.recommendation !== null && lenses.includes(cs.recommendation.recommended_lens)
          ? cs.recommendation.recommended_lens
          : lenses[0]
        : null)
    setRejections(cs.rejections)
    setRejectionsOpen(false)
    setScreenedTarget(target.trim() || null)
    setConfirmingBatch(false)
    setConfirmingGovern(false)
    // Phase 1B: carry the scoped disposition lens + the governing scope id a later broaden
    // supersedes. Both null on the unscoped/one-shot path (the response omits them).
    setDispositions(cs.dispositions ?? null)
    setLastScopeId(cs.scope_id ?? null)
    // A fresh engine round starts a fresh feedback cycle against ITS hypothesis and objective:
    // whole-round feedback reruns these even if the inputs are edited later.
    setRoundHypothesis(roundHyp)
    setRoundObjective(roundObj)
    clearFeedback()
  }

  // Phase 1B (intent_confirmation_ui): recognise the objective and enter the confirm step. NO
  // considered-set call yet — the human confirms/overrides/broadens the proposed scope first.
  // Fail-open: the endpoint never 5xxs, so a technical failure still lands as a recognition here.
  async function recognize(objective: string) {
    const seq = ++generateSeq.current
    setNotice('')
    setScopeChanged(false)
    setGenerating(true)
    // The confirm step shows only the proposed scope: clear any prior round's candidates + lens.
    setGenerated(null)
    setDispositions(null)
    clearSets()
    clearFeedback()
    try {
      const rec = await contractRecognitions(hypothesis.trim(), objective)
      if (seq !== generateSeq.current) return
      setRoundHypothesis(hypothesis.trim())
      setRoundObjective(objective)
      setRecognition(rec)
      setScopePrimary(rec.candidates.find(c => c.relationship === 'primary')?.use_case_id ?? null)
      setScopeSecondary(
        rec.candidates.filter(c => c.relationship === 'secondary').map(c => c.use_case_id))
      setScopeExpansion('exact')
    } catch (err) {
      if (seq !== generateSeq.current) return
      setRecognition(null)
      fail(err)
    } finally {
      if (seq === generateSeq.current) setGenerating(false)
    }
  }

  // The human confirmed the recognised scope: mint the run, persist the scope, and ground only the
  // in-scope subset. Reuses the round's snapshotted hypothesis/objective (set at recognise time).
  async function confirmScope() {
    const rec = recognition
    if (!rec || feedbackLocked) return
    const seq = ++generateSeq.current
    setNotice('')
    setGenerating(true)
    try {
      const ids = [scopePrimary, ...scopeSecondary].filter((id): id is string => id !== null)
      // Every confirmed use-case is llm_proposed here: the controls re-role the recognizer's
      // proposals (confirm/remove/change-primary), there is no free-text add, so the
      // proposed-vs-accepted delta the backend stores is purely re-roling.
      const useCaseOrigins = Object.fromEntries(ids.map(id => [id, 'llm_proposed']))
      const cs = await contractConsideredSet(roundHypothesis, roundObjective, {
        catalogSource: source.trim() || undefined,
        entity: entity.trim() || undefined,
        targetRef: target.trim() || undefined,
        intentId: rec.intent_id,
        recognitionId: rec.recognition_id,
        confirmedScope: {
          primary: scopePrimary,
          secondary: scopeSecondary,
          expansion: scopeExpansion,
          unscoped: false,
          useCaseOrigins,
          confirmationSource: 'user_confirmed',
        },
      })
      if (seq !== generateSeq.current) return
      applyConsideredRound(cs, seq, roundHypothesis, roundObjective)
      setRecognition(null)
    } catch (err) {
      if (seq !== generateSeq.current) return
      fail(err)
    } finally {
      if (seq === generateSeq.current) setGenerating(false)
    }
  }

  // Broaden ("show all buildable recipes"): re-run UNSCOPED under a fresh run, superseding the prior
  // scope (lineage only). Available from the proposed-scope panel and from the disposition lens.
  async function broadenScope() {
    if (feedbackLocked) return
    const rec = recognition
    const seq = ++generateSeq.current
    setNotice('')
    setGenerating(true)
    try {
      const cs = await contractConsideredSet(roundHypothesis, roundObjective, {
        catalogSource: source.trim() || undefined,
        entity: entity.trim() || undefined,
        targetRef: target.trim() || undefined,
        // Prefer the committed intentId: from the disposition-lens broaden (~post-generation) `rec` is
        // already cleared, so falling back to `rec?.intent_id` alone would mint a FRESH intent and orphan
        // the run/scope lineage. `intentId` holds the confirmed round's intent (set by applyConsideredRound).
        intentId: intentId ?? rec?.intent_id,
        recognitionId: rec?.recognition_id,
        confirmedScope: {
          primary: null,
          secondary: [],
          expansion: 'exact',
          unscoped: true,
          useCaseOrigins: {},
          confirmationSource: 'broaden',
        },
        supersedesScopeId: lastScopeId ?? undefined,
      })
      if (seq !== generateSeq.current) return
      applyConsideredRound(cs, seq, roundHypothesis, roundObjective)
      setRecognition(null)
    } catch (err) {
      if (seq !== generateSeq.current) return
      fail(err)
    } finally {
      if (seq === generateSeq.current) setGenerating(false)
    }
  }

  // Change the primary to another candidate: promote the chosen use-case, demote the old primary
  // into the secondaries (both stay llm_proposed — the recognizer proposed them, we re-role).
  function makePrimary(useCaseId: string) {
    setScopeSecondary(prev => {
      const withoutChosen = prev.filter(id => id !== useCaseId)
      return scopePrimary !== null && scopePrimary !== useCaseId
        ? [...withoutChosen, scopePrimary]
        : withoutChosen
    })
    setScopePrimary(useCaseId)
  }

  function removeSecondary(useCaseId: string) {
    setScopeSecondary(prev => prev.filter(id => id !== useCaseId))
  }

  async function generate(e: FormEvent) {
    e.preventDefault()
    const objective = goal.trim()
    // Both the hypothesis and the objective are required: the considered-set intake governs
    // against a stated hypothesis. Same early-return style as the objective check.
    if (!hypothesis.trim() || !objective) return
    // A register batch is confirming or in flight: a new round would replace the rows the
    // human is approving, letting their registrations complete out of view.
    if (feedbackLocked) return
    // Phase 1B (intent_confirmation_ui): Generate first RECOGNISES the objective and hands the
    // proposed scope to the human. Nothing is generated until they confirm/broaden. Flag off →
    // fall straight through to today's one-shot considered-set call below.
    if (confirmationUi) {
      void recognize(objective)
      return
    }
    const seq = ++generateSeq.current
    setNotice('')
    setScopeChanged(false)
    setGenerating(true)
    try {
      // The governed considered-set endpoint: same gauntlet-validated FeatureSet[] as
      // recommend-sets, returned as `alternatives`, plus a server-side intent_id that later
      // governs these candidates into a signed contract. No fallback to a plain endpoint on 503
      // (one provider, identical failure — show the honest notice).
      const cs = await contractConsideredSet(hypothesis.trim(), objective, {
        catalogSource: source.trim() || undefined,
        entity: entity.trim() || undefined,
        targetRef: target.trim() || undefined,
      })
      if (seq !== generateSeq.current) return
      applyConsideredRound(cs, seq, hypothesis.trim(), objective)
    } catch (err) {
      if (seq !== generateSeq.current) return
      setGenerated(null)
      setScreenedTarget(null)
      clearSets()
      clearFeedback()
      fail(err)
    } finally {
      if (seq === generateSeq.current) setGenerating(false)
    }
  }

  // Whole-round feedback: rerun the round's hypothesis + objective under the human's guidance
  // through considered-set (minting a fresh governing intent). Selected and registered candidates
  // are pinned client-side; everything else is replaced by the response.
  async function sendSetFeedback(e: FormEvent) {
    e.preventDefault()
    if (setFbInFlight.current) return
    const instruction = setFbInstruction.trim()
    if (!instruction || setFbRounds >= FEEDBACK_ROUNDS) return
    if (feedbackLocked || generating) return
    const seq = ++generateSeq.current
    setFbInFlight.current = true
    setNotice('')
    setSetFbBusy(true)
    try {
      // Feedback routes through the governed considered-set endpoint so the round mints a FRESH
      // intent over the guided set: post-feedback candidates become governable (the stale-intent
      // guard is lifted). The ROUND's snapshotted hypothesis + objective run, never a since-edited
      // input; scope cannot have drifted (a scope edit voids the round).
      const cs = await contractConsideredSet(roundHypothesis, roundObjective, {
        catalogSource: source.trim() || undefined,
        entity: entity.trim() || undefined,
        targetRef: target.trim() || undefined,
        feedback: instruction,
      })
      if (seq !== generateSeq.current) return
      const round = {
        sets: cs.alternatives, recommendation: cs.recommendation, rejections: cs.rejections,
      }
      setIntentId(cs.intent_id)
      // Pins read the selection AS THE RESPONSE LANDS (the mirrors), not as it stood at submit.
      const prev = generatedRef.current ?? []
      const pinned = prev.filter(c =>
        c.key in selectedRef.current || registeredRef.current[c.key] !== undefined)
      const keptSelected = pinned
        .filter(c => registeredRef.current[c.key] === undefined).length
      const replaced = prev.length - pinned.length
      const pinnedNames = new Set(pinned.map(c => c.idea.name))
      const pinnedKeys = new Set(pinned.map(c => c.key))
      const byName = new Map<string, GeneratedCandidate>()
      const lenses: string[] = []
      for (const set of round.sets) {
        // The pin wins a name collision: one row per name, and the human's kept candidate is
        // never silently swapped for a regenerated variant. Filter FIRST: a set whose every
        // candidate collided (or that arrived empty) must not render an empty card, and must
        // never become the active or recommended-active view.
        const freshIdeas = set.features.filter(idea => !pinnedNames.has(idea.name))
        if (freshIdeas.length === 0) continue
        lenses.push(set.lens)
        for (const idea of freshIdeas) {
          const existing = byName.get(idea.name)
          if (existing) {
            if (!existing.lenses.includes(set.lens)) existing.lenses.push(set.lens)
          } else {
            byName.set(idea.name, {
              kind: 'generated', key: `g${seq}:${idea.name}`, idea, lenses: [set.lens],
            })
          }
        }
      }
      setGenerated([
        ...pinned.map((c): GeneratedCandidate => ({ ...c, kept: true, lenses: [] })),
        ...byName.values(),
      ])
      // The intent was refreshed above (setIntentId(cs.intent_id)) so the FRESH candidates are
      // governable; the kept pins came from a prior generation and are excluded by governableCount.
      // Kept rows left the sets model, so their selection origins go neutral: a kept pick
      // reads as kept in the tray, never as a pick from a set that no longer exists.
      setSelected(prev => Object.fromEntries(
        Object.entries(prev).map(([key, origin]) =>
          pinnedKeys.has(key) ? [key, null] : [key, origin])))
      setSetLenses(lenses)
      setRecommendation(round.recommendation)
      setActiveLens(
        lenses.length > 1
          ? round.recommendation !== null && lenses.includes(round.recommendation.recommended_lens)
            ? round.recommendation.recommended_lens
            : lenses[0]
          : null)
      setRejections(round.rejections)
      setRejectionsOpen(false)
      setConfirmingBatch(false)
      setConfirmingGovern(false)
      setSetFbRounds(r => r + 1)
      setSetFbRecords(records => [...records, {
        round: records.length + 1, user: getSession().user, instruction,
        kept: keptSelected, replaced,
      }])
      setSetFbInstruction('')
      // Kept candidates keep their consumed refine rounds; replaced ones drop theirs.
      setRefines(prevMap => Object.fromEntries(
        Object.entries(prevMap).filter(([key]) => pinnedKeys.has(key))))
    } catch (err) {
      if (seq !== generateSeq.current) return
      // The round never ran: candidates stay, no round is consumed.
      fail(err)
    } finally {
      // Unconditional: only one feedback flight can exist (reentry ref), so no newer feedback
      // owns the flag, and a scope edit or newer generate already reset it anyway.
      setFbInFlight.current = false
      setSetFbBusy(false)
    }
  }

  function patchRefine(key: string, patch: (prev: RefineState) => RefineState) {
    setRefines(prev => ({ ...prev, [key]: patch(prev[key] ?? EMPTY_REFINE) }))
  }

  function toggleRefine(key: string) {
    patchRefine(key, prev => ({ ...prev, open: !prev.open }))
  }

  // Per-candidate feedback: one engine run, re-checked by the gauntlet, recorded under the
  // session user. The result is a suggestion; the candidate changes only on Approve revision.
  async function sendRefine(candidate: GeneratedCandidate) {
    const { key, idea } = candidate
    if (refineInFlight.current.has(key)) return
    const state = refines[key] ?? EMPTY_REFINE
    const instruction = state.instruction.trim()
    if (!instruction || state.rounds >= FEEDBACK_ROUNDS) return
    if (feedbackLocked) return
    refineInFlight.current.add(key)
    setNotice('')
    patchRefine(key, prev => ({ ...prev, busy: true, rejection: null, error: null }))
    try {
      const result = await refineCandidate(
        {
          name: idea.name, description: idea.description, derives_from: idea.derives_from,
          aggregation: idea.aggregation, grain_table: idea.grain_table,
        },
        instruction,
        source.trim() || null, entity.trim() || null, target.trim() || null,
        // The current goal rides along so the engine revises against the objective, not the
        // instruction alone.
        goal.trim() || null,
      )
      // The row may have registered or been replaced while the engine ran: registered
      // candidates take no feedback, and a gone row has nothing to revise. Drop the result.
      const live = (generatedRef.current ?? []).some(c => c.key === key)
      if (!live || registeredRef.current[key] !== undefined) return
      if ('revised' in result) {
        const revised = result.revised
        patchRefine(key, prev => ({
          ...prev, rounds: prev.rounds + 1, pending: revised, pendingRound: prev.rounds + 1,
          pendingInstruction: instruction, rejection: null, error: null, instruction: '',
        }))
      } else {
        const rejected = result.rejected
        // The gauntlet rejected the revision: the round is still consumed, and the candidate
        // is unchanged. The typed instruction stays for the human to adjust.
        patchRefine(key, prev => ({
          ...prev, rounds: prev.rounds + 1, pending: null, rejection: rejected, error: null,
        }))
      }
    } catch (err) {
      const live = (generatedRef.current ?? []).some(c => c.key === key)
      if (live && registeredRef.current[key] === undefined) {
        if (err instanceof ApiError && err.status === 503) {
          // A missing provider is a deployment fact: the one honest top notice, not a row error.
          fail(err)
        } else {
          patchRefine(key, prev => ({
            ...prev, error: err instanceof ApiError ? err.detail : String(err),
          }))
        }
      }
    } finally {
      refineInFlight.current.delete(key)
      setRefines(prev => key in prev
        ? { ...prev, [key]: { ...prev[key], busy: false } }
        : prev)
    }
  }

  // The human accepts the engine's revision: the candidate's data is replaced locally. The key
  // is stable, so selection state survives; registration still requires the tray's confirm.
  function approveRevision(key: string) {
    // Inert under feedbackLocked (the buttons also disable): an approved revision must never
    // diverge from the spec a confirming or in-flight register batch is writing.
    if (feedbackLocked) return
    const pending = (refines[key] ?? EMPTY_REFINE).pending
    if (pending === null) return
    setGenerated(prev => prev === null
      ? prev
      : prev.map(c => (c.key === key ? { ...c, idea: pending } : c)))
    patchRefine(key, prev => ({
      ...prev, pending: null, appliedRound: prev.pendingRound, open: false,
    }))
    // The revision block (holding focus) unmounts: move focus to the candidate row.
    focusTarget.current = `wb-row-${key}`
  }

  // The human declines: the revision is discarded, the candidate untouched. The consumed round
  // stays consumed; the engine ran.
  function revertRevision(key: string) {
    if (feedbackLocked) return
    patchRefine(key, prev => ({ ...prev, pending: null }))
    focusTarget.current = `wb-row-${key}`
  }

  async function draftCandidates(e: FormEvent) {
    e.preventDefault()
    if (draftInFlight.current) return
    // Capture the source once for the whole batch: every recipe drafts against one snapshot,
    // so a mid-batch scope edit cannot split the batch across sources.
    const snapshotSource = source.trim()
    const lines = describeText.split('\n').map(line => line.trim()).filter(Boolean)
    if (lines.length === 0 || !snapshotSource) return
    const seq = ++draftSeq.current
    draftInFlight.current = true
    setNotice('')
    setDraftErrors([])
    setDrafting(true)
    const fresh: DraftCandidate[] = []
    const failedLines: string[] = []
    const lineErrors: string[] = []
    let providerErr: ApiError | null = null
    try {
      // Sequential, in line order: deterministic and kind to the backend. A rejected line is
      // isolated; the surviving lines still draft.
      for (let i = 0; i < lines.length; i++) {
        const query = lines[i]
        try {
          const recipe = await featureRecipe(query, snapshotSource)
          fresh.push({
            kind: 'draft' as const, key: `d${seq}:${i}`, name: slugFrom(query),
            description: query, recipe, snapshotSource,
          })
        } catch (err) {
          failedLines.push(query)
          if (err instanceof ApiError && err.status === 503) {
            // A missing provider is a deployment fact, not a per-line problem: it surfaces as
            // the one honest notice the generate path uses, never as N identical line errors.
            providerErr = err
          } else {
            lineErrors.push(
              `Line ${i + 1}: ${err instanceof ApiError ? err.detail : String(err)}`)
          }
        }
      }
      if (seq !== draftSeq.current) return
      if (fresh.length > 0) setDrafts(prev => [...prev, ...fresh])
      // Keep only the failed lines so a retry is one click away; a clean batch clears fully.
      setDescribeText(failedLines.join('\n'))
      setDraftErrors(lineErrors)
      if (providerErr) fail(providerErr)
    } finally {
      draftInFlight.current = false
      if (seq === draftSeq.current) setDrafting(false)
    }
  }

  function deselect(key: string) {
    setSelected(prev => {
      if (!(key in prev)) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
  }

  function renameDraft(key: string, value: string) {
    setDrafts(prev => prev.map(d => (d.key === key ? { ...d, name: value } : d)))
    // A draft without a name cannot be registered: drop it from the selection too.
    if (!value.trim()) deselect(key)
  }

  function toggleSelect(key: string, origin: string | null) {
    // Changing the selection backs out of either confirm step: the confirm copy must always
    // describe exactly what will be registered or governed.
    setConfirmingBatch(false)
    setConfirmingGovern(false)
    setSelected(prev => {
      const next = { ...prev }
      if (key in next) {
        delete next[key]
      } else {
        next[key] = origin
      }
      return next
    })
  }

  // Take this set: select every unregistered feature of the lens, stamping it as picked from
  // that set. Picks made from other sets keep their own origins (a la carte mixing).
  function takeSet(lens: string) {
    setConfirmingBatch(false)
    setConfirmingGovern(false)
    setActiveLens(lens)
    setSelected(prev => {
      const next = { ...prev }
      for (const c of generated ?? []) {
        if (c.lenses.includes(lens) && !registered[c.key]) next[c.key] = lens
      }
      return next
    })
  }

  async function confirmRegistration() {
    if (batchInFlight.current) return
    const batch = allCandidates.filter(c =>
      c.key in selected && !registered[c.key] && (c.kind === 'generated' || c.name.trim() !== ''))
    if (batch.length === 0) return
    batchInFlight.current = true
    setBatchBusy(true)
    setNotice('')
    try {
      // Sequential, one request per candidate. A failure marks its candidate and the batch
      // continues; the failed candidate stays selected for retry.
      for (const candidate of batch) {
        try {
          const id = await registerFeature(specFor(candidate))
          let freshness: FeatureFreshness | null = null
          try {
            freshness = await featureFreshness(id)
          } catch {
            // Freshness is advisory on this note: omit the chip rather than fail the
            // registration UI.
          }
          setRegistered(prev => ({ ...prev, [candidate.key]: { id, freshness } }))
          deselect(candidate.key)
          setErrors(prev => {
            if (!(candidate.key in prev)) return prev
            const next = { ...prev }
            delete next[candidate.key]
            return next
          })
        } catch (err) {
          setErrors(prev => ({
            ...prev,
            [candidate.key]: err instanceof ApiError ? err.detail : String(err),
          }))
        }
      }
    } finally {
      batchInFlight.current = false
      setBatchBusy(false)
      setConfirmingBatch(false)
    }
  }

  // Govern the selected GENERATED candidates into signed contracts, mirroring confirmRegistration:
  // sequential, one candidate at a time, per-candidate try/catch so a failure marks that candidate
  // and the batch continues. Only fresh (non-kept) generated candidates are in the current intent's
  // considered-set snapshot; chosenSource is always 'alternative' and chosenOptionId is the name.
  async function confirmGovern() {
    if (governInFlight.current) return
    const iid = intentId
    if (!iid) return
    const batch = allCandidates.filter(
      (c): c is GeneratedCandidate =>
        c.key in selected && !governed[c.key] && c.kind === 'generated' && !c.kept
        && (refines[c.key]?.appliedRound ?? null) === null)   // refined => diverges from the snapshot
    if (batch.length === 0) return
    governInFlight.current = true
    setGovernBusy(true)
    setNotice('')
    try {
      for (const candidate of batch) {
        try {
          const d = await contractDraft(iid, 'alternative', candidate.idea.name)
          const c = await contractConfirm(d.draft, iid)
          setGoverned(prev => ({ ...prev,
            [candidate.key]: { contractId: c.contract_id, version: c.version } }))
          deselect(candidate.key)
          setErrors(prev => {
            if (!(candidate.key in prev)) return prev
            const next = { ...prev }; delete next[candidate.key]; return next
          })
        } catch (err) {
          setErrors(prev => ({ ...prev,
            [candidate.key]: err instanceof ApiError ? err.detail : String(err) }))
        }
      }
    } finally {
      governInFlight.current = false
      setGovernBusy(false)
      setConfirmingGovern(false)
    }
  }

  // Tray mix note: where the picks came from. Kept rows carry no lens claim (their origin sets
  // were replaced), so they read as kept picks, never as picks from the currently-viewed set.
  // Honest cross-set copy: each feature was safety-checked at generation within its own set;
  // there is NO set-level re-check of the human's mix (tracked follow-up), so the note must not
  // claim one.
  const keptPicked = selectedCandidates
    .filter(c => c.kind === 'generated' && c.kept === true).length
  const lensNote = originLenses.length > 1
    ? `mixed from ${originLenses.length} sets · each feature was safety-checked at generation; `
      + 'your approval registers them individually'
    : originLenses.length === 1 && originLenses[0] !== undefined
      ? `from the ${lensLabel(originLenses[0])} set`
      : null
  const mixNote = multiSet && selectedCount > 0
    ? keptPicked > 0
      ? lensNote !== null
        ? `${lensNote} · ${keptPicked} kept from an earlier round`
        : 'kept from an earlier round'
      : lensNote
    : null

  return (
    // Carry the current round's governing intent on the DOM so a later task can govern these
    // candidates into a signed contract; undefined until the first successful generate.
    <section data-intent-id={intentId ?? undefined}>
      <div className="gates" role="list" aria-label="Where you are in the loop">
        <Gate
          state={gate1}
          who="You"
          title="State the goal"
          sub="Nothing generates without your intent."
        />
        <Gate
          state={gate2}
          who="Engine"
          title="Propose in sets"
          sub="One set per strategy lens, all safety-checked."
        />
        <Gate
          state={gate3}
          who="You"
          title="Compare, mix, give feedback"
          sub="Take a set or pick a la carte across sets."
        />
        <Gate
          state={gate4}
          who="You"
          title="You approve"
          sub="Nothing registers without your click, under your name."
        />
      </div>
      <div className="panel">
        {notice && (
          <div role="alert" className="callout callout--warn">
            <CalloutGlyph d={WARN_GLYPH} />
            <div className="callout-body">
              <p>{notice}</p>
            </div>
          </div>
        )}
        <form onSubmit={generate} style={{ display: 'grid', gap: 16, margin: 0 }}>
          <div className="field" style={{ maxWidth: 640 }}>
            <label htmlFor="wb-hypothesis">Hypothesis</label>
            <input
              id="wb-hypothesis"
              value={hypothesis}
              onChange={e => setHypothesis(e.target.value)}
              placeholder="e.g. customers whose balance is draining are about to leave"
              style={{ height: 40 }}
            />
          </div>
          <div className="field" style={{ maxWidth: 640 }}>
            <label htmlFor="wb-goal">Prediction goal</label>
            <input
              id="wb-goal"
              value={goal}
              onChange={e => setGoal(e.target.value)}
              placeholder="e.g. predict customer churn in the next 90 days"
              style={{ height: 40 }}
            />
            <div
              className="hint"
              style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}
            >
              <span>
                Both paths use it: the engine generates against it, and written definitions attach
                to it.
              </span>
              <span>Try</span>
              <button type="button" className="role-chip" onClick={() => setGoal(EXAMPLE_GOAL)}>
                {EXAMPLE_GOAL}
              </button>
            </div>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 20 }}>
            <div className="field" style={{ flex: '1 1 220px' }}>
              <label htmlFor="wb-source">Catalog source</label>
              {/* Scope edits clear candidates, so all three lock under feedbackLocked: a row
                  must not leave view while its registration is being written. */}
              <input
                id="wb-source"
                value={source}
                onChange={e => changeSource(e.target.value)}
                placeholder="e.g. deposits"
                disabled={feedbackLocked}
              />
              <p className="hint" style={HELP_STYLE}>
                Optional. Scopes candidates to one upload source; blank searches every catalog.
              </p>
            </div>
            <div className="field" style={{ flex: '1 1 220px' }}>
              <label htmlFor="wb-entity">Entity</label>
              <input
                id="wb-entity"
                value={entity}
                onChange={e => changeEntity(e.target.value)}
                placeholder="e.g. customer"
                disabled={feedbackLocked}
              />
              <p className="hint" style={HELP_STYLE}>
                Optional. Gathers from every catalog holding this entity, e.g. Customer.
              </p>
            </div>
            <div className="field" style={{ flex: '1 1 220px' }}>
              <label htmlFor="wb-target">Target column</label>
              <input
                id="wb-target"
                value={target}
                onChange={e => changeTarget(e.target.value)}
                placeholder="e.g. public.labels.churned"
                disabled={feedbackLocked}
              />
              <p className="hint" style={HELP_STYLE}>
                What you are predicting. Candidates are screened against it server-side, so leaky
                features never reach you.
              </p>
            </div>
          </div>
          <div className="paths">
            <button
              type="submit"
              className="path path-generate"
              disabled={!hypothesis.trim() || !goal.trim() || generating || feedbackLocked}
            >
              <span className="k">Path 1 · The engine</span>
              <span className="t">
                <PathGlyph>
                  <circle cx="8" cy="8" r="6.2" />
                  <path d="M8 5v6M5 8h6" />
                </PathGlyph>
                {generating ? 'Generating' : 'Generate candidate sets'}
              </span>
              <span className="d">
                One validated set per strategy lens, with causal rationales and an advisory pick.
              </span>
            </button>
            <button
              type="button"
              className="path path-describe"
              aria-pressed={describeOpen}
              aria-controls="wb-describe-panel"
              onClick={() => setDescribeOpen(open => !open)}
            >
              <span className="k">Path 2 · Your definitions</span>
              <span className="t">
                <PathGlyph>
                  <path d="M3 13h10M4 10.5 10.8 3.7a1.4 1.4 0 0 1 2 2L6 12.5l-2.8.8z" />
                </PathGlyph>
                Write definitions myself
              </span>
              <span className="d">
                One definition per line; each becomes a draft candidate with its real join path,
                drafted together.
              </span>
            </button>
          </div>
        </form>
      </div>

      {/* Phase 1B Gate #1: the human confirms/overrides/broadens the recognised scope BEFORE the
          considered set is generated. Only rendered behind intent_confirmation_ui, after a
          recognition has landed and before it is confirmed. */}
      {confirmationUi && recognition !== null && (
        <div className="panel" id="wb-scope-panel">
          <h2>Confirm the scope</h2>
          <p className="hint" style={{ marginTop: 4 }}>
            We recognised what you're building. Confirm it, adjust it, or show every buildable
            recipe. Nothing generates until you confirm.
          </p>
          {recognition.status === 'ambiguous' && (
            <p className="hint" role="status">
              The objective read as ambiguous — check the primary before confirming.
            </p>
          )}
          {primaryCandidate === null ? (
            <p role="status">
              No use-case was recognised for this objective. Show all buildable recipes to generate
              over everything.
            </p>
          ) : (
            <>
              <div className="scope-primary" data-role="primary">
                <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                  <span className="badge recommended">Primary</span>
                  <span style={{ fontWeight: 600 }}>{primaryCandidate.display_name}</span>
                  <span className="badge">{primaryCandidate.confidence} confidence</span>
                </div>
                {primaryCandidate.evidence_spans.length > 0 && (
                  <ul className="scope-evidence" style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                    {primaryCandidate.evidence_spans.map(span => (
                      <li key={span} style={{ color: 'var(--ink-soft)' }}>“{span}”</li>
                    ))}
                  </ul>
                )}
              </div>
              {secondaryCandidates.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <h3 style={{ margin: '0 0 8px' }}>Also in scope</h3>
                  <ul className="rows">
                    {secondaryCandidates.map(cand => (
                      <li
                        key={cand.use_case_id}
                        className="row"
                        style={{ alignItems: 'flex-start', gap: 10 }}
                      >
                        <div style={{ display: 'grid', gap: 6, flex: 1, minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                            <span className="badge">Secondary</span>
                            <span style={{ fontWeight: 600 }}>{cand.display_name}</span>
                            <span className="badge">{cand.confidence} confidence</span>
                          </div>
                          {cand.evidence_spans.length > 0 && (
                            <ul className="scope-evidence" style={{ margin: 0, paddingLeft: 18 }}>
                              {cand.evidence_spans.map(span => (
                                <li key={span} style={{ color: 'var(--ink-soft)' }}>“{span}”</li>
                              ))}
                            </ul>
                          )}
                        </div>
                        <button
                          type="button"
                          className="btn"
                          onClick={() => makePrimary(cand.use_case_id)}
                        >
                          Make primary
                        </button>
                        <button
                          type="button"
                          className="btn"
                          aria-label={`Remove ${cand.display_name}`}
                          onClick={() => removeSecondary(cand.use_case_id)}
                        >
                          Remove
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <label
                htmlFor="wb-scope-descendants"
                style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12 }}
              >
                <input
                  id="wb-scope-descendants"
                  type="checkbox"
                  checked={scopeExpansion === 'include_descendants'}
                  onChange={e =>
                    setScopeExpansion(e.target.checked ? 'include_descendants' : 'exact')}
                  style={{ width: 18, height: 18 }}
                />
                Include all sub-use-cases?
              </label>
            </>
          )}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 16 }}>
            {primaryCandidate !== null && (
              <button
                type="button"
                className="btn btn--primary"
                disabled={generating}
                onClick={() => void confirmScope()}
              >
                Confirm scope and generate
              </button>
            )}
            <button
              type="button"
              className="btn"
              disabled={generating}
              onClick={() => void broadenScope()}
            >
              Show all buildable recipes
            </button>
          </div>
        </div>
      )}

      {scopeChanged && (
        <p role="status" className="hint">
          Scope changed. Regenerate to refresh candidates.
        </p>
      )}

      {describeOpen && (
        <div className="panel" id="wb-describe-panel">
          <h2>Describe features</h2>
          <form onSubmit={draftCandidates} style={{ display: 'grid', gap: 12, margin: 0 }}>
            <div className="field" style={{ maxWidth: 640 }}>
              <label htmlFor="wb-describe">Describe the feature you want</label>
              <textarea
                id="wb-describe"
                rows={4}
                value={describeText}
                onChange={e => setDescribeText(e.target.value)}
                placeholder={DESCRIBE_PLACEHOLDER}
              />
              <p className="hint" style={HELP_STYLE}>
                Write one definition per line. Each line becomes a draft candidate you can name,
                adjust, and register together.
              </p>
            </div>
            {draftErrors.length > 0 && (
              <div style={{ display: 'grid', gap: 4 }}>
                {draftErrors.map(message => (
                  <p key={message} className="error" role="alert">
                    {message}
                  </p>
                ))}
              </div>
            )}
            <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
              <button
                type="submit"
                className="btn"
                disabled={drafting || draftLines.length === 0 || !source.trim()}
              >
                {drafting
                  ? 'Drafting…'
                  : draftLines.length > 1
                    ? `Draft ${draftLines.length} candidates`
                    : 'Draft candidate'}
              </button>
              {!source.trim() && (
                <p className="hint">
                  Recipes read one catalog source. Set Catalog source above to draft.
                </p>
              )}
            </div>
          </form>
        </div>
      )}

      {/* Phase 1B disposition lens: only when a SCOPED response carried dispositions and the
          intent_disposition_lens flag is on. Groups the recipe library by how the confirmed scope
          dispositioned each recipe, and keeps "show all buildable recipes" one click away. */}
      {dispositionLens && dispositions !== null && (
        <div className="panel" id="wb-disposition-lens">
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
            <h2>How your scope dispositioned the recipes</h2>
            <span className="micro-label tabular-nums">
              <span style={{ color: 'var(--accent)' }}>{dispositions.length}</span> recipes
            </span>
          </div>
          {DISPOSITION_GROUPS.map(group => {
            const recipes = dispositions.filter(d => d.final_disposition === group.key)
            if (recipes.length === 0) return null
            return (
              <div key={group.key} className="disposition-group" style={{ marginTop: 12 }}>
                <h3 style={{ margin: '0 0 8px' }}>
                  {group.heading}{' '}
                  <span className="micro-label tabular-nums">{recipes.length}</span>
                </h3>
                <ul className="rows">
                  {recipes.map(d => (
                    <li key={d.recipe_id} className="row" style={{ gap: 10, alignItems: 'baseline' }}>
                      <span className="mono" style={{ fontWeight: 600 }}>{d.recipe_id}</span>
                      <span className="hint">{dispositionReason(d)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )
          })}
          <div style={{ marginTop: 12 }}>
            <button
              type="button"
              className="btn"
              disabled={generating}
              onClick={() => void broadenScope()}
            >
              Show all buildable recipes
            </button>
          </div>
        </div>
      )}

      {generated?.length === 0 && (
        <>
          <div className="empty" role="status">
            <p>No grounded candidates for that goal.</p>
            <p className="next">Rephrase the goal, or change the catalog source and generate again.</p>
          </div>
          {/* An all-rejected round still shows WHY: rejections are never hidden. (When drafts
              exist the candidates block below renders the panel instead.) */}
          {rejections.length > 0 && allCandidates.length === 0 && (
            <RejectionsPanel
              rejections={rejections}
              open={rejectionsOpen}
              onToggle={() => setRejectionsOpen(open => !open)}
            />
          )}
        </>
      )}

      {allCandidates.length > 0 && (
        <>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginTop: 32 }}>
            <h2>{multiSet ? 'Proposed feature sets' : 'Proposed features'}</h2>
            <span className="micro-label tabular-nums">
              {multiSet && generated !== null ? (
                <>
                  <span style={{ color: 'var(--accent)' }}>{setLenses.length}</span> sets ·{' '}
                  <span style={{ color: 'var(--accent)' }}>{generated.length}</span>{' '}
                  {generated.length === 1 ? 'feature' : 'features'}
                </>
              ) : (
                <>
                  <span style={{ color: 'var(--accent)' }}>{listCandidates.length}</span>{' '}
                  {listCandidates.length === 1 ? 'candidate' : 'candidates'}
                </>
              )}
            </span>
          </div>
          <p className="hint" style={{ marginTop: 4 }}>
            <strong style={{ color: 'var(--ink)' }}>
              Nothing below enters the catalog without your approval.
            </strong>
            {hasGenerated &&
              ' Design-checked: structurally safe against leakage, staleness, and double-counting. Predictive value is proven later by backtests.'}
          </p>
          {screenedTarget && (
            <p className="hint" style={{ marginTop: 4 }}>
              Screened against <span className="mono">{screenedTarget}</span>: leaky candidates
              were rejected before reaching you.
            </p>
          )}
          {rejections.length > 0 && (
            <RejectionsPanel
              rejections={rejections}
              open={rejectionsOpen}
              onToggle={() => setRejectionsOpen(open => !open)}
            />
          )}
          {multiSet && generated !== null && (
            <>
              <div className="sets">
                {setLenses.map(lens => {
                  const feats = generated.filter(c => c.lenses.includes(lens))
                  const inTray = feats.filter(
                    c => c.key in selected || registered[c.key] !== undefined).length
                  const isActive = lens === activeLens
                  const thesis = LENS_THESES[lens]
                  return (
                    <div key={lens} className="set-card" data-active={isActive || undefined}>
                      <button
                        type="button"
                        className="set-card-view"
                        aria-pressed={isActive}
                        onClick={() => setActiveLens(lens)}
                      >
                        <span className="set-lens">
                          Lens · {lensLabel(lens)}
                          {recommendation?.recommended_lens === lens && (
                            <span className="badge recommended">Recommended</span>
                          )}
                        </span>
                        <span className="set-name">{lensLabel(lens)} set</span>
                        {thesis !== undefined && <span className="set-thesis">{thesis}</span>}
                        <span className="set-meta tabular-nums">
                          {feats.length} {feats.length === 1 ? 'feature' : 'features'} · all
                          design-checked
                          {inTray > 0 ? ` · ${inTray} in your tray` : ''}
                        </span>
                      </button>
                      <button
                        type="button"
                        className="btn set-take"
                        aria-label={`Take this set (${lensLabel(lens)})`}
                        onClick={() => takeSet(lens)}
                      >
                        Take this set
                      </button>
                    </div>
                  )
                })}
              </div>
              {recommendation !== null && (
                <div className="advice">
                  <p>
                    <strong>
                      Engine's pick: {lensLabel(recommendation.recommended_lens)}.
                    </strong>{' '}
                    {recommendation.reasoning}
                  </p>
                  <p className="advice-caveat">Caveat: {recommendation.caveat}</p>
                </div>
              )}
            </>
          )}
          <ul className="rows">
            {listCandidates.map(c => {
              const reg = registered[c.key]
              const gov = governed[c.key]
              const error = errors[c.key]
              const rawName = c.kind === 'generated' ? c.idea.name : c.name
              const displayName = rawName.trim() || 'unnamed draft'
              const canSelect = c.kind === 'generated' || c.name.trim() !== ''
              const description = c.kind === 'generated' ? c.idea.description : c.description
              const aggregation = c.kind === 'generated' ? c.idea.aggregation : c.recipe.aggregation
              const grain = c.kind === 'generated' ? c.idea.grain_table : c.recipe.grain_table
              const derives = c.kind === 'generated'
                ? fmtPairs(c.idea.derives_pairs)
                : c.recipe.derives_from.map(ref => `${c.snapshotSource}:${ref}`).join(', ') || 'none'
              const refine = refines[c.key] ?? EMPTY_REFINE
              const refineExhausted = refine.rounds >= FEEDBACK_ROUNDS
              return (
                <li
                  className="row"
                  key={c.key}
                  id={`wb-row-${c.key}`}
                  tabIndex={-1}
                  style={{ alignItems: 'flex-start' }}
                >
                  {reg || gov ? (
                    <CheckGlyph />
                  ) : (
                    <input
                      type="checkbox"
                      aria-label={`Select ${displayName}`}
                      checked={c.key in selected}
                      disabled={batchBusy || !canSelect}
                      onChange={() => toggleSelect(
                        // A kept row belongs to no current set: selecting it never stamps the
                        // viewed lens; its neutral origin drives the tray mix note.
                        c.key,
                        c.kind === 'generated' && multiSet && c.kept !== true
                          ? activeLens
                          : null)}
                      style={{ width: 18, height: 18, margin: 10, flex: 'none' }}
                    />
                  )}
                  <div style={{ display: 'grid', gap: 8, flex: 1, minWidth: 0, padding: '6px 0' }}>
                    <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 10 }}>
                      <span className={c.kind === 'draft' ? 'mono' : undefined} style={{ fontWeight: 600 }}>
                        {displayName}
                      </span>
                      <span className="badge proposal">
                        {c.kind === 'generated' ? 'Proposal' : 'Draft'}
                      </span>
                      {/* Honest stamp: soft (not solid) so it never outshouts the selection or
                          registered states. Drafts skip the gauntlet, so they carry no stamp. */}
                      {c.kind === 'generated' && c.idea.verification && (
                        <span className="badge ok">{c.idea.verification.toLowerCase()}</span>
                      )}
                      {/* Overlap is on purpose: strong signals earn their place in several
                          theses. Soft chip; the row is one candidate either way. */}
                      {c.kind === 'generated' && c.lenses.length > 1 && (
                        <span className="badge">In {c.lenses.length} sets</span>
                      )}
                      {/* Pinned through a whole-round regeneration. Registered rows skip the
                          chip: Registered is already their mark. */}
                      {c.kind === 'generated' && c.kept === true && !reg && !gov && (
                        <span className="badge">Kept</span>
                      )}
                      {/* The human approved an engine revision in round n. */}
                      {c.kind === 'generated' && refine.appliedRound !== null && (
                        <span className="badge revised">Revised · R{refine.appliedRound}</span>
                      )}
                    </div>
                    <p style={{ color: 'var(--ink-soft)' }}>{description}</p>
                    {c.kind === 'generated' && c.idea.rationale && (
                      <p style={{ color: 'var(--ink-soft)' }}>Why: {c.idea.rationale}</p>
                    )}
                    <dl className="kv">
                      <div>
                        <dt>derives from</dt>
                        <dd className="mono">{derives}</dd>
                      </div>
                      {aggregation && (
                        <div>
                          <dt>aggregation</dt>
                          <dd>{aggregation}</dd>
                        </div>
                      )}
                      {grain && (
                        <div>
                          <dt>grain</dt>
                          <dd>{grain}</dd>
                        </div>
                      )}
                      {c.kind === 'draft' && (
                        <div>
                          <dt>drafted against</dt>
                          <dd className="mono">{c.snapshotSource}</dd>
                        </div>
                      )}
                    </dl>
                    {c.kind === 'draft' && !reg && (
                      <div className="field" style={{ maxWidth: 380 }}>
                        <label htmlFor={`wb-name-${c.key}`}>Name</label>
                        <input
                          id={`wb-name-${c.key}`}
                          className="mono"
                          value={c.name}
                          onChange={e => renameDraft(c.key, e.target.value)}
                          placeholder="feature_name"
                        />
                        {!c.name.trim() && (
                          <p className="hint">Name this draft to select it for registration.</p>
                        )}
                      </div>
                    )}
                    {c.kind === 'draft' && <JoinPathDetails steps={c.recipe.join_path} />}
                    {error && (
                      <p className="error" role="alert">
                        {error}
                      </p>
                    )}
                    {/* Per-candidate feedback: generated rows only (a draft is the human's own
                        definition, revised by editing its line), never on registered or governed
                        rows (a minted contract is finalized; feedback would diverge from it). */}
                    {c.kind === 'generated' && !reg && !gov && (
                      <>
                        <div>
                          <button
                            type="button"
                            className="btn"
                            aria-expanded={refine.open}
                            aria-controls={`wb-refine-${c.key}`}
                            disabled={feedbackLocked}
                            onClick={() => toggleRefine(c.key)}
                          >
                            Give feedback
                          </button>
                        </div>
                        {refine.open && (
                          <form
                            id={`wb-refine-${c.key}`}
                            className="refine-box"
                            onSubmit={e => {
                              e.preventDefault()
                              void sendRefine(c)
                            }}
                          >
                            <div className="field">
                              <label htmlFor={`wb-refine-input-${c.key}`}>
                                What should change
                              </label>
                              <input
                                id={`wb-refine-input-${c.key}`}
                                value={refine.instruction}
                                onChange={e => {
                                  const value = e.target.value
                                  patchRefine(c.key, prev => ({ ...prev, instruction: value }))
                                }}
                                disabled={refine.busy || refineExhausted || feedbackLocked}
                                placeholder="e.g. use a 30 day window"
                              />
                              <p className="hint" style={HELP_STYLE}>
                                Your feedback runs the engine once, re-checks safety, and is
                                recorded under your name. 3 rounds per candidate, then it is
                                back in your hands.
                              </p>
                            </div>
                            <button
                              type="submit"
                              className="btn btn--primary"
                              disabled={refine.busy || refineExhausted || feedbackLocked
                                || !refine.instruction.trim()}
                            >
                              {refine.busy
                                ? 'Requesting revision…'
                                : refineExhausted
                                  ? 'Rounds exhausted'
                                  : `Send feedback for one revision · round ${refine.rounds + 1} of ${FEEDBACK_ROUNDS}`}
                            </button>
                          </form>
                        )}
                        {refine.error && (
                          <p className="error" role="alert">
                            {refine.error}
                          </p>
                        )}
                        {refine.rejection && (
                          <p className="error" role="alert">
                            The safety gauntlet rejected this revision:{' '}
                            {refine.rejection.reason} ({rejectLabel(refine.rejection.code)}).
                            The round is consumed; the candidate is unchanged.
                          </p>
                        )}
                        {refine.pending && (
                          <div className="revision" role="status">
                            <div className="rev-meta">
                              <span className="badge revised">
                                Revision · round {refine.pendingRound} of {FEEDBACK_ROUNDS}
                              </span>
                              <span className="rev-who">
                                {`recorded · from user:${getSession().user} · "${refine.pendingInstruction}"`}
                              </span>
                            </div>
                            <div className="diff">
                              <DiffLine
                                label="name"
                                before={c.idea.name}
                                after={refine.pending.name}
                              />
                              <DiffLine
                                label="description"
                                before={c.idea.description}
                                after={refine.pending.description}
                              />
                              <DiffLine
                                label="aggregation"
                                before={c.idea.aggregation ?? 'none'}
                                after={refine.pending.aggregation ?? 'none'}
                              />
                              <DiffLine
                                label="derives"
                                before={fmtPairs(c.idea.derives_pairs)}
                                after={fmtPairs(refine.pending.derives_pairs)}
                              />
                            </div>
                            <p className="recheck">Re-checked after revision</p>
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 10 }}>
                              {/* Inert while the tray is confirming or a batch is in flight:
                                  the spec being written must not change under the approval. */}
                              <button
                                type="button"
                                className="btn btn--primary"
                                disabled={feedbackLocked}
                                onClick={() => approveRevision(c.key)}
                              >
                                Approve revision
                              </button>
                              <button
                                type="button"
                                className="btn"
                                disabled={feedbackLocked}
                                onClick={() => revertRevision(c.key)}
                              >
                                Revert to original
                              </button>
                            </div>
                          </div>
                        )}
                      </>
                    )}
                    {reg && (
                      <p
                        style={{
                          color: 'var(--ok)', fontWeight: 500,
                          display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8,
                        }}
                      >
                        <span>
                          Registered <span className="mono">{reg.id}</span>
                        </span>
                        {reg.freshness && (reg.freshness.fresh ? (
                          <span className="badge" style={OK_SOLID_CHIP_STYLE}>fresh</span>
                        ) : (
                          <span className="badge stale">
                            stale: {reg.freshness.stale_sources.join(', ')}
                          </span>
                        ))}
                      </p>
                    )}
                    {/* Governed mark, parallel to the registered one: a minted, versioned,
                        design-checked contract. Its own state, so no checkbox and no feedback. */}
                    {gov && (
                      <p style={{ color: 'var(--ok)', fontWeight: 500 }}>
                        Governed <span className="mono">{gov.contractId}</span> v{gov.version}
                        {' · DESIGN-CHECKED'}
                      </p>
                    )}
                  </div>
                </li>
              )
            })}
            {selectedCount > 0 && (
              <li className="row" style={TRAY_STYLE}>
                {confirmingBatch ? (
                  <>
                    <p style={{ flex: '1 1 260px', fontWeight: 500 }}>
                      Your approval writes these features into the registry with their lineage,
                      under your name.
                    </p>
                    <button
                      type="button"
                      className="btn btn--proposal-confirm"
                      disabled={batchBusy}
                      onClick={() => void confirmRegistration()}
                    >
                      Confirm approval
                    </button>
                    <button
                      type="button"
                      className="btn"
                      disabled={batchBusy}
                      onClick={() => setConfirmingBatch(false)}
                    >
                      Cancel
                    </button>
                  </>
                ) : confirmingGovern ? (
                  <>
                    <p style={{ flex: '1 1 260px', fontWeight: 500 }}>
                      Governing runs the safety gauntlet and mints a signed contract per feature —
                      a design check, not a proof it predicts well.
                    </p>
                    <button
                      type="button"
                      className="btn btn--primary"
                      disabled={governBusy}
                      onClick={() => void confirmGovern()}
                    >
                      Confirm govern
                    </button>
                    <button
                      type="button"
                      className="btn"
                      disabled={governBusy}
                      onClick={() => setConfirmingGovern(false)}
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  <>
                    <span className="tabular-nums" style={{ fontWeight: 600 }}>
                      {selectedCount} selected
                    </span>
                    {mixNote !== null && <span className="hint">{mixNote}</span>}
                    <span style={{ flex: '1 1 auto' }} aria-hidden="true" />
                    {/* Govern the generated picks into signed contracts through the two-gate flow.
                        Shown only when a governing intent exists (from the last considered-set
                        call — generate or feedback) and at least one selected candidate is a
                        fresh generated one (governable). */}
                    {intentId !== null && governableCount > 0 && (
                      <button
                        type="button"
                        className="btn"
                        onClick={() => setConfirmingGovern(true)}
                      >
                        Govern {governableCount}
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn btn--primary"
                      onClick={() => setConfirmingBatch(true)}
                    >
                      Approve and register {selectedCount}{' '}
                      {selectedCount === 1 ? 'feature' : 'features'}
                    </button>
                  </>
                )}
              </li>
            )}
          </ul>
          {/* Whole-round feedback: only an engine round can regenerate under guidance, so a
              drafts-only list offers no panel. */}
          {hasGenerated && (
            <form className="setfb" onSubmit={sendSetFeedback}>
              {setFbRecords.length > 0 && (
                <div className="setfb-records" role="status">
                  {setFbRecords.map(r => (
                    <p key={r.round} className="setfb-record">
                      {`Set feedback round ${r.round} of ${FEEDBACK_ROUNDS} · recorded · from user:${r.user} · "${r.instruction}" · kept ${r.kept} selected, replaced ${r.replaced}`}
                    </p>
                  ))}
                </div>
              )}
              <div className="field">
                <label htmlFor="wb-setfb">Feedback on the whole round</label>
                <div className="setfb-row">
                  <input
                    id="wb-setfb"
                    value={setFbInstruction}
                    onChange={e => setSetFbInstruction(e.target.value)}
                    disabled={setFbBusy || setFbExhausted || feedbackLocked}
                    placeholder="e.g. more behavioral signals, fewer balance aggregates"
                  />
                  <button
                    type="submit"
                    className="btn btn--primary"
                    disabled={setFbBusy || setFbExhausted || feedbackLocked || generating
                      || !setFbInstruction.trim()}
                  >
                    {setFbBusy
                      ? 'Regenerating…'
                      : `Regenerate with feedback · round ${Math.min(setFbRounds + 1, FEEDBACK_ROUNDS)} of ${FEEDBACK_ROUNDS}`}
                  </button>
                </div>
              </div>
              {setFbExhausted ? (
                <p className="hint" role="status">
                  Rounds exhausted. Approve, edit by hand, or restate the goal.
                </p>
              ) : (
                <p className="hint">
                  Applies to all sets: approved and selected features are kept; the rest
                  regenerate under your guidance. Recorded under your name. 3 rounds, then it
                  is back in your hands.
                </p>
              )}
            </form>
          )}
        </>
      )}
    </section>
  )
}
