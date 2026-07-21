import type { ReactElement } from 'react'
import { gateConsoleEnabled, useHashRoute } from './nav'
import type { Route } from './nav'
import { SessionBar } from './SessionBar'
import { AssetDetailScreen } from './screens/AssetDetailScreen'
import { GateEvaluationScreen } from './screens/GateEvaluationScreen'
import { GovernanceDashboardScreen } from './screens/GovernanceDashboardScreen'
import { GovernanceReviewScreen } from './screens/GovernanceReviewScreen'
import { IntegrationsScreen } from './screens/IntegrationsScreen'
import { OverviewScreen } from './screens/OverviewScreen'
import { RegistryScreen } from './screens/RegistryScreen'
import { ReviewQueueScreen } from './screens/ReviewQueueScreen'
import { SearchScreen } from './screens/SearchScreen'
import { SemanticsPendingScreen } from './screens/SemanticsPendingScreen'
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
  registry: (
    // Four cells: the registered-feature inventory.
    <NavIcon>
      <rect x="2.75" y="2.75" width="4" height="4" rx="0.75" />
      <rect x="9.25" y="2.75" width="4" height="4" rx="0.75" />
      <rect x="2.75" y="9.25" width="4" height="4" rx="0.75" />
      <rect x="9.25" y="9.25" width="4" height="4" rx="0.75" />
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
}

const PAGES: { route: Route; label: string; eyebrow: string; title: string; description: string }[] = [
  {
    route: 'overview',
    label: 'Overview',
    eyebrow: 'FEATUREGEN · START',
    title: 'Overview',
    description: 'What this platform is and where to start',
  },
  {
    route: 'workbench',
    label: 'Generate features',
    eyebrow: 'CATALOG · GENERATE',
    title: 'Feature generation',
    description:
      'State a hypothesis and goal, generate a safe candidate set, then register drafts or govern the '
      + 'ones that matter into signed contracts.',
  },
  {
    route: 'registry',
    label: 'Registry',
    eyebrow: 'CATALOG · REGISTRY',
    title: 'Feature registry',
    description: 'Browse registered features — open one for its hypothesis, lineage, and consumers.',
  },
  {
    route: 'search',
    label: 'Search',
    eyebrow: 'CATALOG · SEARCH',
    title: 'Search',
    description: 'Find columns you can trust',
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
    // The screen hosts three tabs — Joins (Pass C), Grain & availability (Pass B), Readiness —
    // so the header names the whole review surface, not just the joins tab.
    title: 'Governance review',
    description: 'Confirm the joins, grain, and availability facts the enrichment passes proposed.',
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
  // The gate console page exists only when its flag is on — checked per render (not module
  // scope) so vi.stubEnv works in tests, same as the WorkbenchScreen intent flags.
  const pages = gateConsoleEnabled() ? PAGES : PAGES.filter(p => p.route !== 'gate')
  // 'asset' is a detail sheet, not a nav tab (absent from PAGES, so no rail item highlights) — but
  // it still needs an honest page-head, so it selects a dedicated entry instead of falling back to
  // Overview's copy.
  const page = route === 'asset' ? ASSET_PAGE : (pages.find(p => p.route === route) ?? pages[0])
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
        <header className="page-head">
          <p className="page-head-eyebrow">{page.eyebrow}</p>
          <h1>{page.title}</h1>
          <p>{page.description}</p>
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
        {route === 'review' && <ReviewQueueScreen initialSource={params.get('source') ?? ''} />}
        {route === 'semantics' && (
          <SemanticsPendingScreen initialSource={params.get('source') ?? ''} />
        )}
        {route === 'governance' && (
          <GovernanceReviewScreen initialSource={params.get('source') ?? ''} />
        )}
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
        {route === 'gate' && gateConsoleEnabled() && <GateEvaluationScreen />}
        {route === 'workbench' && <WorkbenchScreen />}
      </main>
    </div>
  )
}
