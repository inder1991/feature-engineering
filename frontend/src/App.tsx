import { useState } from 'react'
import { SessionBar } from './SessionBar'
import { ReviewQueueScreen } from './screens/ReviewQueueScreen'
import { SearchScreen } from './screens/SearchScreen'
import { UploadScreen } from './screens/UploadScreen'

const TABS = ['Upload', 'Search', 'Review queue', 'Workbench'] as const
export type Tab = (typeof TABS)[number]

export default function App() {
  const [tab, setTab] = useState<Tab>('Search')
  const [reviewSource, setReviewSource] = useState('')
  const openReview = (source: string) => {
    setReviewSource(source)
    setTab('Review queue')
  }
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
        {tab === 'Upload' && <UploadScreen onReviewQueue={openReview} />}
        {tab === 'Search' && <SearchScreen />}
        {tab === 'Review queue' && <ReviewQueueScreen initialSource={reviewSource} />}
        {tab === 'Workbench' && <section><h2>Workbench</h2></section>}
      </main>
    </div>
  )
}
