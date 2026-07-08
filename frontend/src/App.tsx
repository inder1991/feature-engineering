import type { ReactElement } from 'react'
import { useHashRoute } from './nav'
import type { Route } from './nav'
import { SessionBar } from './SessionBar'
import { ContractScreen } from './screens/ContractScreen'
import { OverviewScreen } from './screens/OverviewScreen'
import { RegistryScreen } from './screens/RegistryScreen'
import { ReviewQueueScreen } from './screens/ReviewQueueScreen'
import { SearchScreen } from './screens/SearchScreen'
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
  workbench: (
    // Plus-in-circle: generation adds features to the catalog. Echoes the logomark's plus.
    <NavIcon>
      <circle cx="8" cy="8" r="6.25" />
      <path d="M8 5.5v5M5.5 8h5" />
    </NavIcon>
  ),
  contract: (
    // Document with a check: a signed, governed contract.
    <NavIcon>
      <path d="M4 2.75h5.5L12 5.25v8c0 .55-.45 1-1 1H4c-.55 0-1-.45-1-1v-9.5c0-.55.45-1 1-1z" />
      <path d="M5.75 8.5 7.25 10l3-3.25" />
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
      'State your goal, then take either path; both land in one candidate list you approve into the registry.',
  },
  {
    route: 'contract',
    label: 'Govern a feature',
    eyebrow: 'CATALOG · GOVERN',
    title: 'Govern a feature',
    description:
      'State a hypothesis, review the safe considered set, and approve one into a signed contract.',
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
    route: 'upload',
    label: 'Upload',
    eyebrow: 'CATALOG · UPLOAD',
    title: 'Upload',
    description: 'Bring a schema and facts file into the catalog',
  },
  {
    route: 'review',
    label: 'Review queue',
    eyebrow: 'CATALOG · REVIEW QUEUE',
    title: 'Review queue',
    description: 'Rows the catalog refused to trust',
  },
]

export default function App() {
  const { route, navigate, params } = useHashRoute()
  // The upload -> review handoff travels entirely in the URL (?source=). No component state:
  // the hash is the single source of truth, so back/forward and shared deep links always show
  // the queue the address bar names.
  const openReview = (source: string) => {
    navigate('review', { source })
  }
  const page = PAGES.find(p => p.route === route) ?? PAGES[0]
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
          {PAGES.map(p => (
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
        {route === 'upload' && <UploadScreen onReviewQueue={openReview} />}
        {route === 'search' && <SearchScreen />}
        {route === 'registry' && (
          <RegistryScreen featureId={params.get('id')} navigate={navigate} />
        )}
        {route === 'review' && <ReviewQueueScreen initialSource={params.get('source') ?? ''} />}
        {route === 'workbench' && <WorkbenchScreen />}
        {route === 'contract' && <ContractScreen />}
      </main>
    </div>
  )
}
