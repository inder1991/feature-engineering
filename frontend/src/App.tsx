import { useState } from 'react'
import type { ReactElement } from 'react'
import { useHashRoute } from './nav'
import type { Route } from './nav'
import { SessionBar } from './SessionBar'
import { OverviewScreen } from './screens/OverviewScreen'
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
    <NavIcon>
      <path d="M2.75 4.75h6M11.75 4.75h1.5M2.75 11.25h1.5M7.25 11.25h6" />
      <circle cx="10.25" cy="4.75" r="1.5" />
      <circle cx="5.75" cy="11.25" r="1.5" />
    </NavIcon>
  ),
}

const PAGES: { route: Route; label: string; title: string; description: string }[] = [
  {
    route: 'overview',
    label: 'Overview',
    title: 'Overview',
    description: 'What this platform is and where to start',
  },
  {
    route: 'upload',
    label: 'Upload',
    title: 'Upload',
    description: 'Bring a schema and facts file into the catalog',
  },
  {
    route: 'search',
    label: 'Search',
    title: 'Search',
    description: 'Find columns you can trust',
  },
  {
    route: 'review',
    label: 'Review queue',
    title: 'Review queue',
    description: 'Rows the catalog refused to trust',
  },
  {
    route: 'workbench',
    label: 'Workbench',
    title: 'Workbench',
    description: 'Assemble features with checked suggestions',
  },
]

export default function App() {
  const { route, navigate, params } = useHashRoute()
  const [reviewSource, setReviewSource] = useState('')
  const openReview = (source: string) => {
    setReviewSource(source)
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
          <p className="page-head-eyebrow">
            {route === 'overview' ? 'FEATUREGEN · START' : `CATALOG · ${page.label.toUpperCase()}`}
          </p>
          <h1>{page.title}</h1>
          <p>{page.description}</p>
        </header>
        {route === 'overview' && <OverviewScreen navigate={navigate} />}
        {route === 'upload' && <UploadScreen onReviewQueue={openReview} />}
        {route === 'search' && <SearchScreen />}
        {route === 'review' && (
          <ReviewQueueScreen initialSource={reviewSource || params.get('source') || ''} />
        )}
        {route === 'workbench' && <WorkbenchScreen />}
      </main>
    </div>
  )
}
