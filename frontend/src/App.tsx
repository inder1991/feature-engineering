import { useState } from 'react'
import { SessionBar } from './SessionBar'

const TABS = ['Upload', 'Search', 'Review queue', 'Workbench'] as const
export type Tab = (typeof TABS)[number]

export default function App() {
  const [tab, setTab] = useState<Tab>('Search')
  return (
    <div className="app">
      <header>
        <h1>FeatureGen</h1>
        <nav>
          {TABS.map(t => (
            <button key={t} className={t === tab ? 'tab active' : 'tab'} onClick={() => setTab(t)}>
              {t}
            </button>
          ))}
        </nav>
        <SessionBar />
      </header>
      <main>
        {tab === 'Upload' && <section><h2>Upload</h2></section>}
        {tab === 'Search' && <section><h2>Search</h2></section>}
        {tab === 'Review queue' && <section><h2>Review queue</h2></section>}
        {tab === 'Workbench' && <section><h2>Workbench</h2></section>}
      </main>
    </div>
  )
}
