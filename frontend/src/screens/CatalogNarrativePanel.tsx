// The catalog NARRATIVE — what this catalog is, in the bank's own words (profile Task 2/3;
// the editor was deferred from Task 3 and lands here with profile Task 5).
//
// Minimal on purpose: the clients (`getCatalogProfile` / `putCatalogProfile`) already shipped with
// nobody calling them, and an unreachable surface is the same inert mechanism this programme has
// found repeatedly. This is the smallest thing that makes them reachable and honest.
//
// Three rules it follows:
//   * CAS in the BODY. `pointer_version` is echoed back exactly as it was read (0 claims the first
//     write); a 409 means someone else wrote first, and the panel reloads and asks for a
//     re-review rather than silently retrying over their words. THE AUTHOR'S DRAFT SURVIVES that
//     reload: the reload refreshes the CAS anchor and the comparison copy, never the form. The
//     first version reloaded straight into the form, so protecting the server's copy destroyed the
//     one only this person had — a paragraph nobody else could retype, lost to a race the person
//     did not cause.
//   * NO-BLOCKED FRAMING. A catalog with no narrative is not an error and not a gap — it is a
//     catalog nobody has described yet, and the empty state says exactly that.
//   * The routes 404 while FEATUREGEN_DATASET_PROFILES is off, and the panel then renders
//     NOTHING — a flag-off screen looks exactly like today's.
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, getCatalogProfile, putCatalogProfile } from '../api'
import type { CatalogProfile } from '../api'

export function CatalogNarrativePanel({ source }: { source: string }) {
  const [profile, setProfile] = useState<CatalogProfile | null>(null)
  const [absent, setAbsent] = useState(false)   // 404: the surface is off — render nothing
  const [editing, setEditing] = useState(false)
  const [conflicted, setConflicted] = useState(false)   // 409: show their version beside the draft
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [businessContext, setBusinessContext] = useState('')

  // `keepDraft` is what makes the conflict path non-destructive: it refreshes the CAS anchor and
  // the copy shown for comparison, and leaves the form exactly as the author left it. Without it
  // every reload is a load()-into-the-form-while-editing, which is precisely how the draft was lost.
  const load = useCallback(async (keepDraft = false) => {
    try {
      const got = await getCatalogProfile(source)
      setProfile(got)
      setAbsent(false)
      if (keepDraft) return
      setDisplayName(got.profile?.display_name ?? '')
      setDescription(got.profile?.description ?? '')
      setBusinessContext(got.profile?.business_context ?? '')
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setAbsent(true)
      else setNotice(e instanceof ApiError ? e.detail : String(e))
    }
  }, [source])

  useEffect(() => { void load() }, [load])

  if (absent || !profile) return null

  async function save(e: FormEvent) {
    e.preventDefault()
    if (!profile) return
    setBusy(true)
    setNotice('')
    try {
      await putCatalogProfile(source, {
        // Echoed EXACTLY as read. 0 is not "no version" — it is the claim that no narrative
        // existed when this form was opened, and the server refuses it if one appeared since.
        expectedPointerVersion: profile.pointer_version,
        displayName: displayName.trim() || undefined,
        description: description.trim() || undefined,
        businessContext: businessContext.trim() || undefined,
        businessDomains: profile.profile?.business_domains ?? [],
      })
      setNotice('Saved.')
      setEditing(false)
      setConflicted(false)
      await load()
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setNotice('Someone else described this catalog while you were writing — nothing was '
          + 'overwritten, and neither were your words. Their version is shown beside yours; '
          + 're-review it, then save again.')
        setConflicted(true)
        // KEEP THE DRAFT. This reload exists to refresh the CAS anchor (so the next save is
        // anchored on the version just read, not on the stale one that lost) and to fetch the
        // other person's words for comparison — not to overwrite the author's.
        await load(true)
      } else {
        setNotice(err instanceof ApiError ? err.detail : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  const current = profile.profile
  return (
    <section className="adg-section" data-testid="catalog-narrative">
      <h3 className="micro-label">About this catalog</h3>
      {notice && <p role="status" className="hint">{notice}</p>}
      {!editing && (
        <>
          {current ? (
            <div className="adg-kv">
              <div className="kv">
                <span className="k">Name</span>
                <span className="v">{current.display_name ?? <span className="hint">not set</span>}</span>
              </div>
              <div className="kv">
                <span className="k">Description</span>
                <span className="v">
                  {current.description ?? <span className="hint">not written yet</span>}
                </span>
              </div>
              <div className="kv">
                <span className="k">Business context</span>
                <span className="v">
                  {current.business_context ?? <span className="hint">not written yet</span>}
                </span>
              </div>
              <div className="kv">
                <span className="k">Written by</span>
                <span className="v">
                  <span className="badge" title={`lifecycle: ${current.lifecycle}`}>
                    {current.producer}/{current.strength}
                  </span>
                </span>
              </div>
            </div>
          ) : (
            <p className="hint" data-testid="catalog-narrative-empty">
              Nobody has described this catalog yet. Writing it here is what makes the catalog
              findable by what it is FOR, not only by its column names.
            </p>
          )}
          <button type="button" onClick={() => { setConflicted(false); setEditing(true) }}>
            {current ? 'Edit' : 'Describe this catalog'}
          </button>
        </>
      )}
      {editing && (
        <form onSubmit={save} style={{ display: 'grid', gap: 8, marginTop: 8 }}>
          {conflicted && current && (
            <div className="adg-kv" data-testid="catalog-narrative-theirs">
              <p className="hint">
                Their version — yours is still in the fields below, unchanged. Fold in anything
                worth keeping, then save.
              </p>
              <div className="kv">
                <span className="k">Their name</span>
                <span className="v">
                  {current.display_name ?? <span className="hint">not set</span>}
                </span>
              </div>
              <div className="kv">
                <span className="k">Their description</span>
                <span className="v">
                  {current.description ?? <span className="hint">not written</span>}
                </span>
              </div>
              <div className="kv">
                <span className="k">Their business context</span>
                <span className="v">
                  {current.business_context ?? <span className="hint">not written</span>}
                </span>
              </div>
            </div>
          )}
          <label>
            Name
            <input value={displayName} onChange={e => setDisplayName(e.target.value)}
                   maxLength={200} />
          </label>
          <label>
            Description
            <textarea value={description} onChange={e => setDescription(e.target.value)}
                      maxLength={4000} rows={2} />
          </label>
          <label>
            Business context
            <textarea value={businessContext} onChange={e => setBusinessContext(e.target.value)}
                      maxLength={4000} rows={2} />
          </label>
          <div>
            <button type="submit" disabled={busy}>Save</button>{' '}
            <button type="button"
                    onClick={() => { setEditing(false); setConflicted(false); void load() }}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </section>
  )
}
