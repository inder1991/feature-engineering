import type { ReactElement } from 'react'
import {
  entityMapEnabled,
  codeGenerationEnabled,
  featureExecutionEnabled,
  gateConsoleEnabled,
  materializationRunsEnabled,
  useHashRoute,
} from './nav'
import type { Route } from './nav'
import { SessionBar } from './SessionBar'
import { AssetDetailScreen } from './screens/AssetDetailScreen'
import { EntityMapScreen } from './screens/EntityMapScreen'
import { GateEvaluationScreen } from './screens/GateEvaluationScreen'
import { CodeGenerationWorkspaceScreen } from './screens/CodeGenerationWorkspaceScreen'
import { FeatureExecutionScreen } from './screens/FeatureExecutionScreen'
import { MaterializationRunScreen } from './screens/MaterializationRunScreen'
import { GovernanceDashboardScreen } from './screens/GovernanceDashboardScreen'
import { GovernanceReviewScreen } from './screens/GovernanceReviewScreen'
import { IntegrationsScreen } from './screens/IntegrationsScreen'
import { OverviewScreen } from './screens/OverviewScreen'
import { RecipeReviewScreen } from './screens/RecipeReviewScreen'
import { RegistryScreen } from './screens/RegistryScreen'
import { ReviewQueueScreen } from './screens/ReviewQueueScreen'
import { RunDetailScreen } from './screens/RunDetailScreen'
import { RunsScreen } from './screens/RunsScreen'
import { AnalysisWorkspaceScreen } from './screens/AnalysisWorkspaceScreen'
import { SearchScreen } from './screens/SearchScreen'
import { SemanticsPendingScreen } from './screens/SemanticsPendingScreen'
import { SuggestedFeaturesScreen } from './screens/SuggestedFeaturesScreen'
import { UploadScreen } from './screens/UploadScreen'
import { WorkbenchScreen } from './screens/WorkbenchScreen'

function Logomark() {
  // Bracketed lattice: the catalog holds structure.
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M6.5 3.25H4.25v13.5H6.5" />
      <path d="M13.5 3.25h2.25v13.5H13.5" />
      <path d="M10 7v6M7 10h6" />
    </svg>
  )
}

function NavIcon({ children }: { children: ReactElement | ReactElement[] }) {
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

const ICONS: Record<Route, ReactElement> = {
  overview: (
    <NavIcon>
      <circle cx="8" cy="8" r="6.25" />
      <path d="M10.5 5.5 9.25 9.25 5.5 10.5l1.25-3.75z" />
    </NavIcon>
  ),
  upload: (
    <NavIcon>
      <path d="M8 10.25V3.5M5.5 6 8 3.5 10.5 6" />
      <path d="M2.75 10.75v1.5c0 .97.78 1.75 1.75 1.75h7c.97 0 1.75-.78 1.75-1.75v-1.5" />
    </NavIcon>
  ),
  search: (
    <NavIcon>
      <circle cx="7" cy="7" r="4.25" />
      <path d="m10.25 10.25 3 3" />
    </NavIcon>
  ),
  // Three entity nodes joined by edges: the ontology, drawn. Distinct from 'integrations'
  // (a hub linking OUT to services) — this is a peer graph.
  'entity-map': (
    <NavIcon>
      <circle cx="4" cy="4.5" r="1.75" />
      <circle cx="12" cy="4.5" r="1.75" />
      <circle cx="8" cy="12" r="1.75" />
      <path d="M5.75 4.5h4.5M4.9 6.1l2.2 4.3M11.1 6.1l-2.2 4.3" />
    </NavIcon>
  ),
  review: (
    <NavIcon>
      <path d="M2.75 4.5h10.5M2.75 8h10.5M2.75 11.5h5.5" />
      <circle cx="12.25" cy="11.5" r="1.5" />
    </NavIcon>
  ),
  semantics: (
    // A tag awaiting its label: connector-landed columns whose meaning an owner declares.
    <NavIcon>
      <path d="M8.4 2.75h4.85V7.6L7.4 13.45 2.55 8.6z" />
      <circle cx="10.75" cy="5.25" r="0.9" />
    </NavIcon>
  ),
  workbench: (
    // Plus-in-circle: generation adds features to the catalog. Echoes the logomark's plus.
    <NavIcon>
      <circle cx="8" cy="8" r="6.25" />
      <path d="M8 5.5v5M5.5 8h5" />
    </NavIcon>
  ),
  analysis: (
    // A question mark in a circle: this screen asks, it does not act. Deliberately not a play or
    // run glyph — nothing here executes.
    <NavIcon>
      <circle cx="8" cy="8" r="6.25" />
      <path d="M6.4 6.2a1.6 1.6 0 1 1 1.9 1.85V9.4" />
      <path d="M8.3 11.3h.01" />
    </NavIcon>
  ),
  registry: (
    // Four cells: the registered-feature inventory.
    <NavIcon>
      <rect x="2.75" y="2.75" width="4" height="4" rx="0.75" />
      <rect x="9.25" y="2.75" width="4" height="4" rx="0.75" />
      <rect x="2.75" y="9.25" width="4" height="4" rx="0.75" />
      <rect x="9.25" y="9.25" width="4" height="4" rx="0.75" />
    </NavIcon>
  ),
  // A marked list: each run is one record in an append-only log. Distinct from 'review' (bare
  // rules ending in a decision dot) — every row here is already a fact, not a question.
  runs: (
    <NavIcon>
      <circle cx="3.75" cy="4.25" r="0.9" />
      <circle cx="3.75" cy="8" r="0.9" />
      <circle cx="3.75" cy="11.75" r="0.9" />
      <path d="M6.75 4.25h6.5M6.75 8h6.5M6.75 11.75h4" />
    </NavIcon>
  ),
  integrations: (
    // Linked nodes: one instance (top) linking out to its services (below). A connection graph.
    <NavIcon>
      <circle cx="8" cy="3.75" r="1.75" />
      <circle cx="3.75" cy="12.25" r="1.75" />
      <circle cx="12.25" cy="12.25" r="1.75" />
      <path d="M6.9 5.15 4.6 10.6M9.1 5.15l2.3 5.45M5.5 12.25h5" />
    </NavIcon>
  ),
  governance: (
    // Shield with a check: joins go live only after the two-admin confirmation.
    <NavIcon>
      <path d="M8 2.5 12.75 4.25v3.4c0 2.95-1.95 5.15-4.75 5.85-2.8-.7-4.75-2.9-4.75-5.85v-3.4z" />
      <path d="m6.25 8 1.25 1.25L10 6.5" />
    </NavIcon>
  ),
  // A signed sheet: a recipe definition with a decision recorded against it. Distinct from
  // 'governance' (a shield: relationship confirmations) — this is per-definition sign-off.
  recipes: (
    <NavIcon>
      <rect x="3.25" y="2.25" width="9.5" height="11.5" rx="1.25" />
      <path d="M5.5 5.25h5M5.5 7.5h5" />
      <path d="m5.5 10.75 1.25 1.25L9.5 9.25" />
    </NavIcon>
  ),
  dashboard: (
    // Rollup bars over a baseline: the read-only counts at a glance.
    <NavIcon>
      <path d="M2.75 13.25h10.5" />
      <path d="M4.75 10.75v-3.5M8 10.75v-6M11.25 10.75v-4.5" />
    </NavIcon>
  ),
  gate: (
    // Gauge with a needle: the machine gate reads out — it does not decide.
    <NavIcon>
      <path d="M2.75 11.25a5.25 5.25 0 0 1 10.5 0" />
      <path d="m8 11.25 2.4-2.9" />
      <path d="M2.75 13.5h10.5" />
    </NavIcon>
  ),
  // A detail sheet: one catalog asset opened to its sections. Not a top-nav tab (absent from PAGES,
  // so this icon is never rendered in the rail) — the asset route is reached via a Details action —
  // but ICONS is an exhaustive Record<Route> so every route keeps an entry (mirrors 'gate').
  asset: (
    <NavIcon>
      <rect x="3.25" y="2.75" width="9.5" height="10.5" rx="1.25" />
      <path d="M5.5 5.75h5M5.5 8h5M5.5 10.25h3" />
    </NavIcon>
  ),
  // A detail sheet reached with a request_id, behind VITE_MATERIALIZATION_RUNS. The entry exists
  // only because ICONS is an exhaustive Record<Route> (mirrors 'asset' / 'gate').
  materialization: (
    <NavIcon>
      <path d="M3 11.5 8 3l5 8.5z" />
      <path d="M3 13.25h10" />
    </NavIcon>
  ),
  // S11's execution workspace, reached with an artifact id and behind VITE_FEATURE_EXECUTION. A
  // detail sheet rather than a nav tab, so it is absent from PAGES; the entry exists only because
  // ICONS is an exhaustive Record<Route> (mirrors 'materialization' / 'asset' / 'gate').
  'feature-execution': (
    <NavIcon>
      <path d="M6 3.5 2.75 8 6 12.5" />
      <path d="M10 3.5 13.25 8 10 12.5" />
    </NavIcon>
  ),
  // Also a detail sheet, not a nav tab (absent from PAGES): one table's suggested features. The
  // entry exists only because ICONS is an exhaustive Record<Route> (mirrors 'asset' / 'gate').
  suggested: (
    <NavIcon>
      <path d="M8 2.75a3.75 3.75 0 0 0-2.25 6.75V11h4.5V9.5A3.75 3.75 0 0 0 8 2.75z" />
      <path d="M6.75 13.25h2.5" />
    </NavIcon>
  ),
}

const PAGES: PageHead[] = [
  {
    route: 'overview',
    label: 'Overview',
    eyebrow: 'FEATUREGEN · START',
    title: 'Overview',
    description: 'What this platform is and where to start',
  },
  {
    route: 'workbench',
    label: 'Discover candidates',
    eyebrow: 'CATALOG · GENERATE',
    title: 'Feature generation',
    description:
      'State a hypothesis and goal, plan a candidate set over one catalog, then save ideas — '
      + 'browsable sketches, never a model input — or govern the ones that matter into signed '
      + 'contracts.',
  },
  {
    route: 'analysis',
    label: 'Ask a question',
    eyebrow: 'CATALOG · ANALYSE',
    title: 'Analysis workspace',
    description:
      'Ask a question in plain words and see what it would compute, which periods it would read, and '
      + 'what the answer would rest on — before anyone runs it.',
  },
  {
    route: 'registry',
    label: 'Registry',
    eyebrow: 'CATALOG · REGISTRY',
    title: 'Feature registry',
    description: 'Browse registered features — open one for its hypothesis, lineage, and consumers.',
  },
  {
    route: 'runs',
    label: 'Runs',
    eyebrow: 'CATALOG · RUNS',
    title: 'Feature runs',
    description:
      'Every feature-generation workflow, grouped by hypothesis — open a run to see exactly '
      + 'what happened, stage by stage, and what its evidence pins.',
  },
  {
    route: 'search',
    label: 'Search',
    eyebrow: 'CATALOG · SEARCH',
    title: 'Search',
    description:
      'Find trusted data, then understand what the system can do with it.',
  },
  {
    // Entity map v0, behind VITE_ENTITY_MAP — filtered out of the rendered nav in App() when the
    // flag is off (parseHash also refuses the route then): absent, not broken.
    route: 'entity-map',
    label: 'Entity map',
    eyebrow: 'CATALOG · ENTITY MAP',
    title: 'Entity map',
    description:
      'The catalog’s ontology, drawn: every entity the columns carry, grouped by catalog, and '
      + 'every available cross-catalog link — proposed or confirmed — as an edge. Read-only.',
  },
  {
    // The route stays 'upload' (#/upload unchanged — deep links keep working); only the words
    // change: the screen now holds two peer ingest paths (file upload + OpenMetadata connector).
    route: 'upload',
    label: 'Ingest',
    eyebrow: 'CATALOG · INGEST',
    title: 'Ingest',
    description: 'Bring data maps into the catalog: upload a file, or pull from a configured sync.',
  },
  {
    route: 'integrations',
    label: 'Integrations',
    eyebrow: 'CATALOG · INTEGRATIONS',
    title: 'Integrations',
    description:
      'Metadata services FeatureGen connects to. An integration is one OpenMetadata instance; under it, each service you sync maps to a catalog source.',
  },
  {
    route: 'review',
    label: 'Review queue',
    eyebrow: 'CATALOG · REVIEW QUEUE',
    title: 'Review queue',
    description: 'Rows the catalog refused to trust',
  },
  {
    route: 'semantics',
    label: 'Semantics',
    eyebrow: 'CATALOG · SEMANTICS',
    title: 'Semantics pending',
    description:
      'Columns that imported without their declared semantics. Fill in additivity, unit, '
      + 'currency, entity, or the as-of flag — feature generation treats the gaps honestly until you do.',
  },
  {
    route: 'governance',
    label: 'Governance',
    eyebrow: 'CATALOG · GOVERNANCE REVIEW',
    // One cross-catalog decision queue now (it used to be three source-keyed tabs behind a text
    // input): the header names the judgement being asked for, not the fact types it spans.
    title: 'Governance review',
    // Says what the page HOLDS, like every sibling here. The second sentence ("The system
    // proposes; you decide whether it means what it says") was a second framing statement stacked
    // directly above the callout that frames the page, and the callout does it with the fact that
    // actually motivates the work — that these relationships are already in use.
    description: 'Every relationship waiting on a person, across every catalog you can see.',
  },
  {
    route: 'recipes',
    label: 'Recipe reviews',
    eyebrow: 'CATALOG · RECIPE REVIEWS',
    title: 'Recipe reviews',
    description:
      'The governed recipe registry opened for sign-off: what each recipe computes, which reviewer '
      + 'roles its own declarations require, and where approvals are missing at the current '
      + 'revision. Recording a decision needs the governance role.',
  },
  {
    route: 'dashboard',
    label: 'Dashboard',
    eyebrow: 'Governance',
    title: 'Governance dashboard',
    description: 'Pipeline rollups + outcomes.',
  },
  {
    // Internal, authority-only, behind VITE_INTENT_GATE_CONSOLE — filtered out of the rendered
    // nav in App() when the flag is off (parseHash also refuses the route then).
    route: 'gate',
    label: 'Gate console',
    eyebrow: 'INTENT · GATE CONSOLE',
    title: 'Gate evaluation',
    description:
      'Authority-only: run the machine gate over a shadow cohort — verdict, failed conditions, '
      + 'coverage, and the population behind the numbers. Evaluating decides nothing.',
  },
]

// The asset detail sheet's page-head. Kept OUT of PAGES (it is not a top-nav destination — it is
// reached via a Details action on a search hit), but it still needs its own eyebrow/title/copy.
const ASSET_PAGE = {
  route: 'asset' as Route,
  label: 'Asset detail',
  eyebrow: 'CATALOG · ASSET',
  title: 'Asset detail',
  description:
    'One catalog asset opened to its sections — identity, metadata & evidence, relationships, '
    + 'readiness, and history. Every value comes from the catalog; corrections stage a new '
    + 'evidence layer, they never rewrite the source.',
  // The screen opens with its own hero (business term, physical ref, definition, authority chips),
  // so the page-head would restate it at lower quality. Render the eyebrow as a breadcrumb only.
  crumbOnly: true,
}

// The suggested-features sheet's page-head (P4 v1). Kept OUT of PAGES for the same reason as the
// asset sheet: it is a per-table destination, not a top-nav one.
const SUGGESTED_PAGE = {
  route: 'suggested' as Route,
  label: 'Suggested features',
  eyebrow: 'CATALOG · SUGGESTED FEATURES',
  title: 'Suggested features',
  description:
    'What this catalog can already build on one table — no hypothesis, no LLM. Read-only: these '
    + 'are proposals with the engine’s own statuses, and nothing here changes the catalog.',
}

// The run detail's page-head. Unlike the sheets above it SHARES its route with a list ('#/runs' and
// '#/runs/<id>' are one destination, one rail item), so it cannot be keyed into DETAIL_PAGES — it is
// selected by the same run_id param that decides which screen renders below. The detail opens with
// its own hero (name, id, owner, hypothesis), so the list's title and description would restate one
// run's record at lower quality: eyebrow only.
const RUN_DETAIL_PAGE = {
  route: 'runs' as Route,
  label: 'Run detail',
  eyebrow: 'CATALOG · RUNS',
  title: 'Feature run',
  description:
    'One feature-generation run opened to its record — identity, milestones, authoring rows and '
    + 'the stage rail, exactly as the spine derives them. Read-only.',
  crumbOnly: true,
}

// A page head. `crumbOnly` suppresses the title + description for screens that open with their own
// hero, leaving the eyebrow as a breadcrumb.
type PageHead = {
  route: Route
  label: string
  eyebrow: string
  title: string
  description: string
  crumbOnly?: boolean
}

// The detail sheets, keyed by route: reached from an action elsewhere, never from the left rail.
const DETAIL_PAGES: Partial<Record<Route, PageHead>> = {
  asset: ASSET_PAGE,
  suggested: SUGGESTED_PAGE,
}

export default function App() {
  const { route, navigate, params } = useHashRoute()
  // The upload -> review handoff travels entirely in the URL (?source=). No component state:
  // the hash is the single source of truth, so back/forward and shared deep links always show
  // the queue the address bar names.
  const openReview = (source: string) => {
    navigate('review', { source })
  }
  // Same URL-borne handoff for the semantics queue: the connector's post-import "N semantics
  // pending" link lands here with the sync's target source in the hash.
  const openSemantics = (source: string) => {
    navigate('semantics', { source })
  }
  // And for the governance dashboard -> review launchpad: a source row's Review action (or a
  // scoped pending count) lands on the confirmation surface with that source in the hash.
  const openGovernanceReview = (source: string) => {
    navigate('governance', { source })
  }
  // Flag-gated pages exist only when their flag is on — checked per render (not module scope) so
  // vi.stubEnv works in tests, same as the WorkbenchScreen intent flags.
  const pages = PAGES.filter(p =>
    (p.route !== 'gate' || gateConsoleEnabled())
    && (p.route !== 'entity-map' || entityMapEnabled()))
  // 'asset' is a detail sheet, not a nav tab (absent from PAGES, so no rail item highlights) — but
  // it still needs an honest page-head, so it selects a dedicated entry instead of falling back to
  // Overview's copy.
  // A detail route (absent from PAGES, so no rail item highlights) selects its dedicated page-head
  // instead of falling back to Overview's copy.
  // Read ONCE, and by both the head and the screen below: a run_id that selected the detail head
  // but not the detail screen would put a breadcrumb over the list.
  const runId = params.get('run_id')
  const page = (route === 'runs' && runId ? RUN_DETAIL_PAGE : DETAIL_PAGES[route])
    ?? (pages.find(p => p.route === route) ?? pages[0])
  return (
    <div className="shell">
      <aside className="rail">
        <div className="rail-brand">
          <Logomark />
          <div className="rail-brand-text">
            <span className="rail-brand-name">FeatureGen</span>
            <span className="micro-label">Feature catalog</span>
          </div>
        </div>
        <nav className="rail-nav" aria-label="Primary">
          {pages.map(p => (
            <button
              key={p.route}
              type="button"
              className={p.route === route ? 'nav-item active' : 'nav-item'}
              aria-current={p.route === route ? 'page' : undefined}
              onClick={() => navigate(p.route)}
            >
              {ICONS[p.route]}
              {p.label}
            </button>
          ))}
        </nav>
        <div className="rail-session">
          <SessionBar />
        </div>
      </aside>
      <main>
        <header className={page.crumbOnly ? 'page-head page-head--crumb' : 'page-head'}>
          <p className="page-head-eyebrow">{page.eyebrow}</p>
          {!page.crumbOnly && (
            <>
              <h1>{page.title}</h1>
              <p>{page.description}</p>
            </>
          )}
        </header>
        {route === 'overview' && <OverviewScreen navigate={navigate} />}
        {route === 'upload' && (
          <UploadScreen
            onReviewQueue={openReview}
            onSemanticsQueue={openSemantics}
            onManageIntegrations={() => navigate('integrations')}
          />
        )}
        {route === 'integrations' && <IntegrationsScreen />}
        {route === 'search' && <SearchScreen />}
        {route === 'registry' && (
          <RegistryScreen featureId={params.get('id')} navigate={navigate} />
        )}
        {/* One route, two surfaces: '#/runs' is the grouped list, '#/runs/<id>' one run's record.
            The id rides the PATH (nav.ts decodes it into run_id), so an absent param is the list —
            never a detail sheet silently pointed at nothing. */}
        {route === 'runs' && (runId
          ? <RunDetailScreen runId={runId} />
          : <RunsScreen navigate={navigate} />)}
        {route === 'review' && <ReviewQueueScreen initialSource={params.get('source') ?? ''} />}
        {route === 'semantics' && (
          <SemanticsPendingScreen initialSource={params.get('source') ?? ''} />
        )}
        {route === 'governance' && (
          <GovernanceReviewScreen initialSource={params.get('source') ?? ''} />
        )}
        {route === 'recipes' && <RecipeReviewScreen initialRecipe={params.get('recipe') ?? ''} />}
        {route === 'dashboard' && <GovernanceDashboardScreen onReview={openGovernanceReview} />}
        {route === 'asset' && (
          // Reached via a Details action on a search hit — source + object_ref ride the hash. Keyed
          // so an asset -> asset deep link (different params) remounts to a clean load.
          <AssetDetailScreen
            key={`${params.get('source') ?? ''}:${params.get('object_ref') ?? ''}`}
            source={params.get('source') ?? ''}
            objectRef={params.get('object_ref') ?? ''}
          />
        )}
        {route === 'suggested' && (
          // One table's suggested features — source + table ride the hash. Keyed so a
          // suggested -> suggested deep link (a different table) remounts to a clean load.
          <SuggestedFeaturesScreen
            key={`${params.get('source') ?? ''}:${params.get('table') ?? ''}`}
            source={params.get('source') ?? ''}
            table={params.get('table') ?? ''}
            // The column the reader arrived FROM. The page stays table-scoped -- its job is
            // "everything this table can build" -- but four different columns landing on an
            // identical page, with nothing saying which one you clicked, is indistinguishable
            // from a broken link.
            fromColumn={params.get('column') ?? undefined}
          />
        )}
        {route === 'gate' && gateConsoleEnabled() && <GateEvaluationScreen />}
        {route === 'materialization' && materializationRunsEnabled() && (
          <MaterializationRunScreen requestId={params.get('request_id') ?? ''} />
        )}
        {/* S11's execution workspace. A detail sheet reached with an artifact id, exactly as the
            materialization sheet is reached with a request id — every field it needs identifies
            WHICH artifact and WHICH environment, and none of them has a defensible default: a
            missing artifact id must render an empty workspace that says so, not one silently
            pointed at something else. */}
        {route === 'feature-execution' && featureExecutionEnabled() && (
          <FeatureExecutionScreen
            artifactId={params.get('artifact_id') ?? ''}
            environmentId={params.get('environment_id') ?? ''}
            logicalGroupName={params.get('group') ?? ''}
            inventoryObservationId={params.get('observation_id') ?? ''}
            generationAuthorizationRevisionId={params.get('authorization_id') ?? ''}
            checkSetHash={params.get('check_set_hash') ?? ''}
            goal={params.get('goal') ?? ''}
            targetMode={params.get('target_mode') ?? 'prediction'}
            targetRef={params.get('target_ref')}
          />
        )}
        {/* Step 5b — the generation workspace: one durable build journey, watched from a job
            id. A detail sheet like the materialization and execution sheets: a missing job id
            renders an empty workspace that says so, never one pointed at something else. */}
        {route === 'code-generation' && codeGenerationEnabled() && (
          <CodeGenerationWorkspaceScreen
            jobId={params.get('job_id') ?? ''}
            navigate={navigate}
          />
        )}
        {route === 'entity-map' && entityMapEnabled() && <EntityMapScreen navigate={navigate} />}
        {route === 'workbench' && <WorkbenchScreen />}
        {route === 'analysis' && <AnalysisWorkspaceScreen />}
      </main>
    </div>
  )
}
