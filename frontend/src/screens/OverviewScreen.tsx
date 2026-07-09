import type { MouseEvent } from 'react'
import type { Route } from '../nav'

const LOOP: { route: Route; name: string; line: string }[] = [
  // The nav labels this screen 'Ingest' (route hash stays #/upload); the action label matches.
  // The line still names uploading a file honestly — it is the primary path — and now also names
  // the connector, since Ingest offers two peer paths into the same pipeline.
  {
    route: 'upload',
    name: 'Ingest',
    line: 'Upload a schema+facts file, or connect a metadata service, per source.',
  },
  { route: 'search', name: 'Search', line: 'Search the freshness-vouched catalog.' },
  { route: 'review', name: 'Review queue', line: 'Review what the catalog refused to trust.' },
  {
    route: 'workbench',
    name: 'Generate features',
    line: 'Generate features: the engine proposes, you approve and register.',
  },
]

export function OverviewScreen({
  navigate,
}: {
  navigate: (r: Route, params?: Record<string, string>) => void
}) {
  const go = (route: Route) => (e: MouseEvent<HTMLAnchorElement>) => {
    e.preventDefault()
    navigate(route)
  }
  return (
    <section>
      <p>
        FeatureGen is the bank's upload-driven data catalog and feature-engineering workbench.
        Teams upload schema and facts files per source; the catalog serves a searchable,
        freshness-vouched map of what data exists, quarantines what it cannot trust, and helps
        assemble ML features with leakage and staleness checks.
      </p>

      <h2>The loop</h2>
      <ol className="flow-list">
        {LOOP.map((step, i) => (
          <li key={step.route}>
            <span className="flow-num" aria-hidden="true">
              {String(i + 1).padStart(2, '0')}
            </span>
            <div className="flow-body">
              <a href={`#/${step.route}`} onClick={go(step.route)}>
                {step.name}
              </a>
              <p>{step.line}</p>
            </div>
          </li>
        ))}
      </ol>

      <h2>Start here</h2>
      <div className="callout callout--hero">
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
            <path d="M2.75 8h10.5M9 3.75 13.25 8 9 12.25" />
          </svg>
        </span>
        <div className="callout-body">
          <p>
            Upload <span className="mono">docs/examples/deposits.csv</span> as source{' '}
            <span className="mono">deposits</span>, then search for{' '}
            <span className="mono">balance</span>. Once data is in, Generate features is where the
            engine works for you.
          </p>
          <button type="button" className="btn btn--primary" onClick={() => navigate('upload')}>
            Go to Ingest
          </button>
        </div>
      </div>

      <h2>About this session</h2>
      <div className="callout">
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
            <circle cx="8" cy="8" r="6.25" />
            <path d="M8 7.25v3.5M8 5.25v.01" />
          </svg>
        </span>
        <div className="callout-body">
          <p>This is a stub dev session. Roles switch in the rail.</p>
          <p>
            AI assist depends on the deployment's LLM provider; the Generate features screen shows
            its live status.
          </p>
          <p>Search serves only freshness-vouched facts.</p>
        </div>
      </div>
    </section>
  )
}
