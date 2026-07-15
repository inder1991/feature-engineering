# Ingestion review — triage

**Reviewed:** 32 external findings against `main` @ `5c20de7`, each independently verified + an adversarial refute pass. **Baseline for fixes:** `dbd2d40` (branch `fix/ingestion-review-hardening`).

## Headline

**0 of 6 claimed Criticals survived as Critical.** The review was substantially overstated — 20 of 31 auto-evaluated findings collapsed to Low / by-design / refuted under a second skeptic pass. But there is a real cluster of input-validation and audit hardening worth doing.

| Verdict | Count | Meaning |
|---|---|---|
| Confirmed (stand) | 11 (+#26 manual) | real defect, survived refute |
| Overturned | 17 | real mechanism, downgraded to Low / by-design |
| Refuted | 3 | not a real defect |

`#31` (their projection-lag finding) was **already fixed** by the e2e `_drain_projection` change (`768c60e`).

---

## Being fixed now (24 findings, fix wave running)

### High / Medium — always-on base pipeline
| # | What it is | Real sev | Fix |
|---|---|---|---|
| 14 | OpenMetadata default naming drops schema → two upstream tables collapse into one | **High** | detect FQN collisions on fold |
| 2 | Literal dots in table/column names collide on the graph key or mis-parse lineage | Medium | quarantine dotted names at validation |
| 11 | `column_joins` filters sensitivity on the *target* only → a non-`pii_reader` caller can read a sensitive source column's join endpoints | Medium | filter source sensitivity too |
| 16 | Whitespace in a source/service id → `"sales"` vs `"sales "` become separate catalogs | Medium | strip ids at the API boundary |
| 17 | CSV/XLSX readers accept structural corruption (dup headers, conflicting `table`/`tablename` aliases) | Medium | reject, not last-write-wins |
| 7 | Quarantine row-resolution skips some re-checks (narrowed residual) | Medium | fix the real residual |
| 26 | Malformed glossary FQN `schema..column` silently coerced to a 2-part table term | Low–Med | reject empty FQN components |
| 18 | Canonical model under-validated (whitespace-only fields, invalid bool→false, invalid as-of→posted_at) | Low | add enum/validation checks |
| 5 | Malformed `joins_to` still becomes a display edge on the ungoverned path | Low | validate format before writing edge |
| 29 | Wide tables: synth caps at 64, lineage overflows `max_nodes` | Low | column-count guard |
| 30 | `staled` counts changed objects, not facts staled (mislabel) | Low | relabel telemetry |
| 27 | Upload error mapping opaque after parse (concurrency → 500) | Low | map 409/422 with stage diagnostics |
| 28 | Frontend/backend upload contract mismatch (size limit, `.xlsm`) | Low | align both sides |
| 1 | Catalog identity raw vs normalized across stages → transient false drift + cache misses | Low | normalize once, key consistently |
| 10 | Join consumers don't return authority state to distinguish pending/rejected from verified | Low | return authority state |

### Medium / Low — only with governed flags or the LLM provider on
| # | What it is | Real sev | Fix |
|---|---|---|---|
| 20 | LLM egress audit written on the request connection → a later upload-tx rollback erases the record that data left the system | Med–Low | write on a separate connection |
| 19 | Glossary LLM egress hardcodes `raw_input_classification="clean"`; scanner misses personal names | Low (security) | classify honestly, run the scanner |
| 21 | Provider-call budget undercounts retries/repairs (batch reports `1`) | Low | report actual call count |
| 22 | Single mode caches unknown concepts permanently as `unclassified` | Low | don't poison the cache |
| 24 | LLM audit omits generation settings + provider token/cost usage | Low | record them |

### Connector-only
| # | What it is | Real sev | Fix |
|---|---|---|---|
| 25 | "Semantics pending" is a count with no review records, but the UI says they were queued | Low | create records or fix copy |
| 32 | Connector snapshot hash not canonical under folded-key collisions | Low | sort by a disambiguating key |
| 13 | Connector approval re-checks the remote snapshot but not the local baseline (TOCTOU) | Low (hardening) | revalidate local baseline at approval |

---

## Deliberately NOT fixed (8) — refuted, by-design, or already fixed

| # | Why skipped |
|---|---|
| 3 | Same-source ingest serialization — compound degraded precondition, atomic abort; a per-source advisory lock is a deliberate design call (deadlock-risky), deferred |
| 4 | Quarantine vs large-change brake — no real bypass; review-queue nit only |
| 6 | **Refuted** — `joins_to` change *does* create drift via other fingerprint fields |
| 8 | **Refuted** — each ingest uses its own source-scoped adapter, not a shared singleton |
| 9 | **Refuted** — glossary schema *is* preserved; multi-schema represents correctly |
| 12 | By-design — pending joins to not-yet-loaded targets are intended |
| 23 | **Refuted** — false premise; the idempotency lookup *is* used |
| 31 | Already fixed by the e2e `_drain_projection` change (`768c60e`) |

---

*Full per-finding evidence (verbatim, 167 KB) is the gitignored scratch file `.superpowers/sdd/ingestion-review-triage.md`.*
