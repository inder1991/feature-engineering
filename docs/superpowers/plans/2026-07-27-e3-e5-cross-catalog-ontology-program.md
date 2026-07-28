# Cross-Catalog Identity & Ontology — Programme Plan (rev 6)

Date: 2026-07-27 · Revised: 2026-07-28 · Status: restructured — one document per job ·
Grounded against `origin/main@ef213a83` plus the on-branch fix `e6e5f31d`.

> **Sequencing, contract ownership and the link policy now live in
> [`2026-07-28-product-roadmap-and-contract-ownership.md`](2026-07-28-product-roadmap-and-contract-ownership.md).**
> That document is the master roadmap; this one is the architectural reference for the ontology
> core. Where the two disagree on order, ownership or link policy, the roadmap wins. The E5 screens
> here (E5.5-E5.7) are explicitly an optional later product, not a prerequisite.

**This document is the plan: contracts, phases, gates, targets.** It states *what to build and in
what order*. It deliberately does **not** carry the evidence base — every "already earned" claim
cites
**[`../../architecture/2026-07-28-verified-interfaces-cross-catalog.md`](../../architecture/2026-07-28-verified-interfaces-cross-catalog.md)**,
which owns the verified interfaces, the citations, and the measured numbers, and changes when the
code changes rather than when the plan does.

Parents: `../specs/2026-07-26-llm-metadata-enrichment-design.md` and
`../specs/2026-07-26-cross-catalog-ontology-view-design.md`

> **Rev 6 (2026-07-28) — restructured, and the identifier-link work folded in.** Rev 5 had grown to
> ~2,600 lines doing three jobs at once — reference, design, and plan — and the identifier-link
> design had been written as a *separate* spec, which promptly drifted: within a day the plan
> asserted the planner keeps to existing bridges while the spec had proposed links reaching
> materialization. Two documents describing one subject is what caused that. Rev 6 cuts by **job**
> rather than by topic: the evidence base moves to the verified-interfaces reference, and the
> identifier-link spec is folded in as **Phase E0b**, so there is exactly one home describing how
> cross-catalog links work. The retired spec's content is preserved in full — its verified-baseline
> section in the reference, everything else in E0b.

> **Rev 5 keeps the Rev-4 contract design and repairs its factual base.** The fourth review
> found the contracts sound but the earned/not-earned ledger wrong in the direction that hurts:
> load-bearing "already earned" foundations that are inert, absent, or already solved differently.
> Rev 5: (1) removes the duplicate bounded-traversal and per-field-authority specifications —
> both shipped, and are now cited and reused rather than rebuilt; (2) replaces the false
> "persist before the public-flattening collision step" premise with an explicit **ingest schema
> contract** prerequisite (E3.Foundation.0), because `CanonicalRow` carries no schema at all and
> the collision is resolved inside the readers; (3) replaces every confirm-time CAS claim with the
> shipped spine's actual **propose-time singleton denial**; (4) reclassifies durable LLM result
> reuse from earned to new work, because the store persists no response and its probe is unwired;
> (5) states the cardinality-in-`fact_key` defect that makes a cardinality correction mint a second
> fact rather than demote the first; (6) states that `entity_bridge` is cross-catalog-only, single-
> admin-confirmed, and has **no creation surface in the product**, which E5.3 silently depends on;
> (7) resequences so a user-visible slice ships first instead of after six phases; and (8) adds
> measured baselines to the success criteria so the program cannot be green and deliver nothing.
>
> **Rev 5 deletes contracts with no named consumer.** Rev 4 defined ~30 `*V1` types, 13 of which
> appeared only in their own definition. A contract earns its place by being named in a Work or
> Acceptance item.
>
> **Rev 5.1 (2026-07-28) adds measured baselines.** Every M-number in "Measured criteria" was run
> against the deployed catalog rather than estimated. Two results change the program's footing:
> the catalog holds **one source and one table**, so every cross-catalog promise is currently
> undemonstrable (new measure M7, the plan's largest prior blind spot); and bridge candidacy was
> **structurally impossible** for glossary-sourced catalogs until `e6e5f31d`, because the matcher
> read `data_type` while a glossary populates `declared_type` (0 → 28 eligible identifier columns
> on FTR). A third result is a live finding outside this program's scope: 28 columns carry a
> governed sensitivity floor that no read-scope predicate consults.
>
> **Rev 3 retained the Rev-2 corrections and closed the second review's twelve
> findings.** It separates identifier
> equivalence from between-entity relationships; resolves objects only through complete
> governed grains; refuses defaulted/unknown cardinality; preserves physical schema
> identity; replaces the collapsed authority tier with independent derivation,
> verification, and eligibility axes; restores fresh-only default reads; scopes titles
> per local realization; defines a complete E3 handoff; fixes cross-catalog candidate
> identity; validates the concept hierarchy as a DAG; and makes read-scope closure apply
> to the whole returned graph. Rev 3 additionally separates stable candidate identity
> from evidence revisions, makes title selection a realization-scoped singleton, admits
> same-object-type business links, pins composite join mappings and link versions,
> reuses the shipped semantic-binding candidate/currentness substrate, defines
> projection-safe coherent reads, removes E5 component state from E3 contracts, and
> specifies freshness, diagnostic stubs, bounded graph closure, and the LLM payload.

## Outcome

Deliver a source-agnostic metadata ontology in which a user can:

1. find every fresh, source-entitled, readable property with attested physical identity classified
   as a concept across catalogs, while returning unknown/colliding physical identity only as
   explicitly non-operational conflict context;
2. inspect one business object type, its catalog-local realizations, governed keys,
   each realization's optional local title property, and properties;
3. inspect typed entity links with direction and realization-specific cardinality;
4. see which catalog joins physically realize entity links and which governed bridges
   connect identifier namespaces;
5. navigate the graph without an AI proposal, stale source, or hidden column being
   presented as verified truth; and
6. receive bounded, explicitly truncated results without an automatic table view walking the
   transitive closure of a connected bank catalog.

This is a **program**, not one implementation slice. E3 is a separately accepted
prerequisite for every relationship-bearing E5 slice. The concept-query slice E5.1 may be built
after E3.Foundation and before the complete E3 handoff, but E5.2–E5.7 do not start until E3
publishes a stable link read contract.

**Outcome item 1 ships first, in days, as Phase E0** — a concept facet on the already cross-catalog,
already fresh-only, already read-scoped `search()`. Everything else in this list requires the E3
prerequisite. That ordering is deliberate: it puts something in front of a user before the program
commits to six phases of foundation, and it publishes the concept-coverage baseline that tells us
whether the rest is worth building.

**Each promise above is conditional on a prerequisite that may not be funded.** Items 2–4 require
attested physical schema (E3.Foundation.0) and a bridge creation surface (E3.4); item 3's
cardinality requires the `approved_join` identity migration. Where a prerequisite is descoped, the
corresponding promise is struck from this list in the same decision — see the descope-honestly rule
in the build order.

> **A note on this document's size.** Rev 5 is ~2,500 lines, and a fair criticism of it is the same
> one levelled at Rev 4: a great deal is specified before contact with a user. The growth is mostly
> *verified ground truth* and *explicit decisions that were previously silent assumptions*, which is
> the opposite failure from over-design — but the reading cost is real. Recommended follow-up:
> split this file, moving "What is already earned" / "What is not earned" and the citation base into
> the E3.0 verified-interfaces reference where they belong, and leaving a short plan behind. The
> ledger is a reference artifact with a long life; the plan should be able to change without it.

## Verified baseline

Both ledgers — *what is already earned* and *what is not earned* — now live in
**`../../architecture/2026-07-28-verified-interfaces-cross-catalog.md`**, with a file citation for
every claim, alongside the identifier-link substrate, the evidence available about a column, the
`attest/` harness contract, the gauntlet and LLM seams, and the measured baselines.

They were moved out of this plan for two reasons. They are a **reference**: they change when the
code changes, not when the plan changes, and keeping them here meant every plan revision carried 300
lines of code archaeology. And they are **shared**: the identifier-link work cites the same facts,
and restating them in two documents is what produced a live contradiction between this plan and its
spec within a day.

**The rule this plan follows from here:** a claim that something is already earned cites that
document. It does not restate it. When an entry there turns out to be wrong, it is fixed there and
this plan inherits the correction.

The findings most load-bearing for what follows, by way of orientation only — the reference is
authoritative:

- attested physical **schema does not reach ingest** on the CSV/Excel path at all, and the
  FTR/glossary path quarantines cross-schema collisions inside the reader;
- **bounded join traversal and per-field property authority are shipped** and are reused here, not
  respecified;
- the fact spine has **no confirm-time CAS** — it denies a competing proposal at propose time;
- **cardinality is part of `approved_join` identity**, so a correction forks a second fact rather
  than demoting the first;
- there is **no product route to create an identifier link**, and confirmation is single-admin
  because no catalog has a recorded owner;
- **durable LLM result reuse does not exist**;
- read scope filters the **raw** sensitivity tag, not the governed floor.
## Corrections required in the E5 design before implementation

1. **Object-type resolution, not row-level entity resolution.** The control plane has
   metadata and key declarations; it does not read customer rows. It can establish
   that two catalog schemas model `customer` and that VERIFIED key bridges make two
   realizations interoperable. It cannot decide that two customer records are the
   same real person.
2. **One semantic object does not imply one connected key namespace.** The ontology
   may show one `customer` object type containing several catalog realizations while
   also showing two or more disconnected resolution components.
3. **Cardinality belongs to a realization and separates basis from authority.** Joint
   ownership and other catalog differences make a single universal
   `Customer 1:N Account` assertion dishonest. The semantic link has endpoints and
   meaning; each backed catalog realization has its own cardinality, explicit/defaulted/
   missing basis, and independent authority envelope. Missing/defaulted cardinality is
   `unknown`, not `N:1`; an identity-namespace bridge has cardinality `not_applicable`.
   A summary may say `varies`, never silently choose one.
4. **Primary key and title are different.** Governed `grain` supplies an ordered,
   possibly composite key. `is_grain` is not a title. A missing title remains missing
   until E3 proposes or governs a title binding.
5. **Registry size is dynamic.** The E5 design's fixed 281/50 counts are already stale.
   APIs expose a registry version and content fingerprint, never a hard-coded count.
6. **Search freshness remains fail-closed by default.** Existing `/search` removes
   stale sources because stale sensitivity metadata may no longer authorize visibility.
   Normal ontology reads are fresh-only. A separately permissioned diagnostic mode may
   return a stale source/object stub, but never stale properties, keys, titles, links,
   backing refs, counts, or facets based on stale sensitivity.
7. **Identifier namespaces are not entity relationships.** A VERIFIED
   `customer_id ↔ customer_id` bridge means two identifiers are sanctioned as the same
   namespace/equijoin basis. It is not `Customer → Customer`, carries no relationship
   cardinality, and never supplies a business predicate.
8. **A bridged foreign key does not change table grain.** A
   `transactions.customer_id` bridge can reference Customer while the table remains a
   Transaction realization. Only a complete governed grain attaches a table to an
   object type.
9. **Planner roll-ups are exposed only as roll-ups.** `account_to_customer` proves
   `rolls_up_to`/grain derivability. It does not prove `owned_by`, `belongs_to`, or any
   other business predicate.
10. **Physical identity requires physical storage.** `schema_name` attached to a flattened graph
    row is evidence, not a schema-safe key. E3 must persist every attested physical table/column
    before producing operational ontology identities. A graph attachment is optional and only
    valid when the physical-to-graph mapping is one-to-one.
11. **Object membership is table-governed.** A table enters an operational object type only through
    one current table-level `ObjectTypeBindingV1` backed by the exact current complete grain.
    Column entity assignments can support a proposal or identifier role; they cannot independently
    classify the table.
12. **Identifier equality is tuple equality.** Every identifier binding is an ordered non-empty
    tuple. A sanctioned bridge maps the complete left tuple to the complete right tuple in order.
    Existing single-column bridges adapt as length-one tuples; independently confirmed members
    never synthesize a composite bridge.
13. **Pagination is revision-checked, not snapshot-imagined.** Each page gets its own
    repeatable-read transaction. Continuation succeeds only while the server's monotonic metadata
    revision, registry fingerprints, source-scope policy, freshness policy, and authenticated scope
    still match the signed cursor.
14. **Bounds have a server owner.** Caller bounds may narrow work but never enlarge
    `OntologyServerLimitsV1`. Automatic neighborhoods are one hop. Deeper verified paths require an
    explicit request and remain subject to server hard limits.
15. **Property authority is field-level.** A verified concept does not launder an LLM definition,
    inherited domain, or unknown type into the same verification status.
16. **LLM replay reuses durable results — and that store must be BUILT first.** A retry of the
    same selection revision reuses its first valid persisted dispatch result. A deliberate
    re-evaluation is a new selection revision with an explicit reason; stochastic output is never
    treated as deterministic identity. **Rev-5 correction:** Rev 4 treated this as an existing
    capability. It is not (see "not earned"): no response body is persisted, the uniqueness key is
    per-attempt by design, and the only full-identity probe is unwired and `run_id`-scoped.
    E3.Foundation must add a content-addressed selection key derived from the selection revision,
    persist the validated response body/hash, and wire a reuse probe — or E3.2 drops the replay
    acceptance criterion entirely.

17. **Cardinality cannot be corrected while it lives in `fact_key`.** Before any ontology surface
    presents cardinality with a basis, the `approved_join` identity must stop hashing it, the value
    schema must admit `unknown` plus an explicit `basis`, and `graph.py`'s propose-time
    `or "N:1"` fabrication must be removed so missing-ness survives to the fact. Until then
    "missing cardinality stays `unknown`" is unachievable, two contradictory VERIFIED facts can
    coexist on one column pair, and a re-upload that flips `N:1` to `1:N` raises no divergence.
    This is a **safety** change, not an ontology nicety: a wrong `1:N` silently corrupts every
    aggregation built on it.

18. **A grain is not a key until it is unique.** The shipped `grain` fact carries
    `is_unique: boolean` and the profiler deliberately proposes `False` for sampled uniqueness in
    `[0.99, 1.0)`. Object identity, `object_grain` identifier bindings, and namespace attachment
    require `is_unique = true`. Bridging on a non-unique grain is the exact mechanism that merges
    two different real-world entities — which Correction 1 places out of scope.

19. **A bridge needs evidence that two namespaces are the same namespace.** Bounding the candidate
    enumeration is a cost control, not an evidence gate. The shipped matcher agrees on concept
    group, `entity_link` and a coarse type family only. Before E5.3 unions bridges transitively —
    where one wrong bridge silently merges a whole component — E3 must add key-shape/format/
    uniqueness evidence to the candidate, and the write gate must verify the endpoints are
    identifier-concept columns that exist and that `entity_id` is in `known_entities()` (the
    contrast is `_entity_assignment_write_error`, which already does this;
    `_bridge_write_error` does not).

20. **Bridges need a creation surface and a real confirmation rule before E5 consumes them.**
    There is no HTTP route to propose or confirm an `entity_bridge`, and the shipped confirmation
    is single-platform-admin with the dual-sign-off deferral explicitly conditioned on bridges
    becoming live-traversable — which is what E5 does. E3.4 must ship the bridge governance route,
    and E3.0 must decide whether bridges VERIFIED under the single-admin regime are re-confirmed
    or grandfathered under a named, audited exception. "Endpoint owner" authority is not available:
    every shipped `owner_of` returns `None`.

21. **Authority axes must be lossless against the shipped enums.** `derivation` must carry
    `profiler`, `parser`, `structural_connector` and `taxonomy` — folding a sampled statistical
    inference into `source` misattributes an observation as an attestation, and
    `structural_connector` proposes every bridge and Pass-C join. `verification` must carry
    `partially_confirmed`, `reverify` and fact-lifecycle `stale`; a drift-STALEd fact must not
    render as `verified`, and `partially_confirmed` must never map to `verified`.

22. **`AuthorityEnvelopeV1` keeps the influence ceiling.** The shipped kernel carries two
    independent guarantees — the `operational_rule` **and** the `InfluenceTier` ceiling, documented
    as "belt AND braces — the ceiling is the hard guarantee" (`field_policies.py:58-65`) and
    enforced structurally at `field_authority.py:296-297` before any rule is evaluated. Rev 4's
    four axes reproduce the rule and drop the ceiling. Rev 5 restores it: an envelope carries an
    `influence_max`, and no policy may raise `operational_eligibility` above it. In particular,
    LLM `confidence` may inform review routing and **must never** feed operational eligibility.

23. **"A policy-eligible source assertion" is a net authority widening — it is out of scope for
    v1.** Rev 4 let a source assertion write/revalidate the object-type and realization aggregates
    with no human confirmation. Today `primary_entity` and `entity` are advisory with
    `influence_max = RECOMMENDATION` (`field_policies.py:134-140, 161-166`). That escape hatch also
    re-opens the single-actor upload→approve round trip that `uploader_ne_confirmer` exists to close
    (`authority.py:219-235`): the human who uploads the file would unilaterally set the table's
    canonical object type, which then decides E5.2 grouping and E5.3 attachment. **v1 requires
    governed confirmation for object-type binding.** A source-assertion tier may be proposed later,
    on its own evidence, with its own review.

24. **The metadata revision is per-source, not global.** A single global counter bumped inside the
    ingest transaction would serialize every catalog behind one row for the duration of another
    source's LLM enrichment. Revisions are per-`catalog_source`; a cursor binds the revision vector
    for the sources it actually read. Continuation is refused when **one of those** sources moves,
    not when any catalog anywhere moves.

25. **Read scope must be fixed before the ontology multiplies its reach, not after.** The governed
    floor lands in `effective_restriction`, which no read-scope predicate consults; tables carry no
    sensitivity at all; and a re-upload that omits the sensitivity column silently blanks it. An
    ontology view is the highest-leverage consumer of this filter — it crosses catalogs and shows
    relationships. E3.0 states the decision and E3.Foundation lands it, ahead of any cross-catalog
    read. If the decision is to defer, the plan says plainly that ontology visibility inherits
    today's raw-tag semantics and the concept-derived floor does not restrict it.

## Non-negotiable boundaries

- Metadata/control-plane only. No HDFS/Hive row reads, value joins, customer matching,
  data virtualization, or operational write-back.
- Ontology v1 is explicitly **single-tenant** because the shipped catalog/event keys are
  deployment-global. Requests with a non-`None` tenant are refused by the ontology surface. A
  multi-tenant ontology requires a new contract version and migration that places tenant in every
  physical, fact, candidate, cursor, and projection identity.
- `catalog:read` is necessary but not sufficient. Every read and persistent candidate-generation
  dispatch intersects requested sources with the server-owned `OntologySourceScopePolicyV1`; absent
  entitlement fails closed.
- AI may propose links and title bindings. A proposal is visible and useful as
  advisory metadata. Operational eligibility is derived through the current governed
  authority policy **and is hard-capped by the field's `influence_max` ceiling**, which is
  evaluated structurally before any policy rule runs. Neither `source_declared` nor
  `human_verified` is a universal shortcut, and **LLM confidence is never an input to
  eligibility** — it may route review and nothing else.
- No new edge authorizes a feature-planner join **silently**. *(Revised 2026-07-28 by user
  decision — the earlier form of this boundary said the planner keeps using only its existing
  governed joins/bridges, which is no longer the intended behaviour.)* An identifier link that has
  survived admission (deterministic grounding + a two-critic panel) reaches the planner as
  `proposed` and feeds feature generation **and materialization**, confirmed or not. What the
  boundary now forbids is silence, not use: every feature and every materialized artifact standing
  on an unconfirmed link carries `JOIN_IDENTITY_UNCONFIRMED` naming that link, and a human
  confirming clears it while a rejection demotes what rests on it. Admission, the critic panel and
  the provenance rule are specified in **Phase E0b**. A later explicit adapter is still required
  before any **other** E3 link type (entity links, column semantic links) reaches the planner.
- Visibility is graph-closed: filter properties, grain keys, title bindings,
  identifier bindings, backing refs, and links before computing objects, counts, or
  facets. If an endpoint or backing ref is not visible, its link and counts are absent.
  A fully hidden realization/object is absent. “Object visible but key withheld” is
  allowed only when policy explicitly permits revealing the object independently.
- Every returned object, property, title binding, identifier binding/bridge, semantic
  link, and realization carries independent derivation, verification, freshness, and
  operational-eligibility axes plus underlying provenance.
- Derived verification inherits its root. A rulebook-derived value from an AI proposal
  is `(derivation=rulebook, verification=proposed)`, never silently verified.
- Normal reads are fresh-only. Stale sensitivity can never authorize visibility.
- Missing, conflicting, stale, or unverified metadata is represented explicitly.
  The read model does not invent a title, key, bridge, business predicate, direction,
  schema, or cardinality.
- Stable semantic identity never includes catalog snapshots, clocks, model runs,
  evidence observations, or policy versions. Those fields identify a revision and its
  provenance; changing them revalidates or supersedes the stable object rather than
  creating a second semantic object.
- A physical table id is `(catalog_source, attested_schema, table)`. A stable local-realization id
  adds canonical `entity_id`; the complete grain and evidence live in the realization revision,
  never in that stable id.
- Every identifier binding is one ordered tuple, even when its tuple has one member. Composite
  namespace bridges and link realizations swap whole endpoint tuples during canonicalization and
  never sort or independently reorder member pairs.
- Per-field authority remains available to consumers. A convenience property/object summary may be
  derived conservatively, but never replaces or upgrades the field envelopes it summarizes.
- E3 returns base realizations and verified/advisory facts. E5 alone computes
  read-scope-specific namespace components and attaches them to ontology views.
- Functional correctness, authority, source/read-scope, lifecycle demotion, conflict handling,
  finite enumeration, one-hop automatic neighborhoods, honest truncation, and cursor integrity ship
  now. Caches, incremental acceleration, bulk review, telemetry, and layout polish are deferred NFRs.

---

## Program contracts

The program uses the following stable, versioned contracts. `E3OntologyInputsV1`
explicitly lists the subset E3 publishes; E5-owned view contracts are assembled only
after that handoff. These semantic separations are fixed here; E3.0 verifies exact
repository interfaces and field names before implementation.

### The consumer gate

**No contract is built before its consumer is named, and the consumer must be built in this
programme.** Rev 4 defined ~30 `*V1` types, 13 of which appeared only in their own definition. Rev 5
cut those. Rev 6 applies a stronger test, because "named in a Work item" turned out to be too weak —
a contract can be named all the way through E3 and still reach nothing a user experiences.

A contract now needs one of:

- **a feature-generation consumer** — it arrives through one of the two doors in Phase C
  (a grounding input, or a gauntlet requirement), with a stated before/after feature count; or
- **a view consumer that someone has asked for** — a specific screen, where E0's evidence says
  people want it. "A screen we have not validated anyone wants" is not a consumer.

The plan already applies this rule to relation kinds: `converted_by` was cut because its consumer
(an extended formula grammar) does not exist. Rev 6 applies it to the whole contract set.

**What the gate does to the current inventory:**

| Contract | Consumer | Verdict |
| --- | --- | --- |
| `IdentifierBindingV1`, `IdentifierNamespaceBridgeV1`, `ObjectTypeBindingV1`, `RealizationCardinalityV1`, `ColumnSemanticLinkV1(denominated_in)`, `OntologyPropertyV1` | Phase C — grounding input or gauntlet requirement | **Build.** Each carries a feature-count claim. |
| `PhysicalObjectIdentityV1`, `PhysicalMetadataObjectV1`, `CandidateIdentityV1`, `AuthorityEnvelopeV1`, `OntologySourceScopePolicyV1`, `OntologyServerLimitsV1`, `OntologyMetadataRevisionV1`, `OntologyCursorV1`, `OntologyFreshnessPolicyV1`, `OntologyReadBoundsV1` | infrastructure the above cannot exist without | **Build**, but justified as *load-bearing for a consumer*, never on their own. |
| `EntityLinkTypeV1`, `EntityLinkRealizationV1` | needs a template-library change before a recipe can declare a relationship need | **Gated.** Build only once that template work is funded in the same breath. A typed link with no recipe that wants one changes nothing. |
| `TitlePropertyBindingV1`, `TitlePropertyBindingCandidateV1` | display only | **Gated on a view consumer.** Not feature generation — do not let it ride in on E3's coat-tails. |
| `SemanticObjectTypeV1`, `E3OntologyInputsV1`, the E5 view contracts | the three E5 views | **Gated on E0's evidence.** If E0 shows people want the semantic map, these earn their place; if it shows they want cross-catalog features instead, most of E5 waits. |
| `StaleObjectStubV1`, `ColumnSemanticLinkV1(converted_by, as_of)` | none | **Deferred** (already, in Rev 5). |

This is deliberately uncomfortable. It puts roughly half the contract set behind evidence that does
not exist yet — which is the point, since the alternative is building all of it and discovering
afterwards that feature generation consumed none of it.

### Contract inventory and v1 scope

**A contract earns its place by being named in a Work or Acceptance item.** Rev 4 defined ~30 `*V1`
types, 13 of which appeared exactly once — in their own definition — and one
(`EffectivePropertyFieldV1`) was referenced three times but never defined at all. Rev 5 classifies
every contract, and a type in the deferred column is **not implemented in v1** even though its
definition is retained here as design intent:

| Status | Contracts |
| --- | --- |
| **v1 — built, with a named consumer** | `OntologySourceScopePolicyV1`, `OntologyServerLimitsV1` (as an extension of shipped `join_path`), `OntologyMetadataRevisionV1`, `OntologyCursorV1`, `PhysicalObjectIdentityV1`, `PhysicalMetadataObjectV1`, `OntologyPropertyV1`, `EffectivePropertyFieldV1`, `OntologyPropertyPageV1`, `ObjectTypeBindingV1`, `ObjectTypeBindingCandidateV1`, `SemanticObjectTypeV1`, `LocalObjectRealizationV1`, `CandidateIdentityV1`, `IdentifierBindingV1`, `IdentifierNamespaceBridgeV1`, `EntityLinkTypeV1`, `RealizationCardinalityV1`, `EntityLinkRealizationV1`, `AuthorityEnvelopeV1`, `OntologyFreshnessPolicyV1`, `OntologyReadBoundsV1`, `E3OntologyInputsV1` |
| **v1 — read-side projections, no stored identity** | `LocalObjectRealizationCandidateV1` (view over `ObjectTypeBindingCandidateV1`) |
| **v1 — only if its own gate is funded** | `LLMSelectionEvidenceV1` (needs the content-addressed dispatch store; drop it and E3.2's replay acceptance together), `TitlePropertyBindingV1` / `TitlePropertyBindingCandidateV1` (need the new realization-scoped ref type), `IdentifierNamespaceBridgeCandidateV1` (reconcile with the shipped `entity_bridge_candidate_evidence` before adding a second store) |
| **Deferred — no named consumer in v1** | `StaleObjectStubV1` (see the deferral shape below), `ColumnSemanticLinkV1` kinds `converted_by` and `as_of` (see below) |

**How `StaleObjectStubV1` is deferred without destabilising every signature.** It appears in the
source-scope policy's `stale_diagnostic` operation, the freshness policy's permission name, both
service signatures' `stale_diagnostics` flag, `E3OntologyInputsV1.diagnostic_stubs`, and several
acceptance lists. Rev 5 keeps every one of those **shapes** and defers only the stub content:

- `stale_diagnostics=True` is accepted, permission-checked and audited exactly as specified, and in
  v1 returns a **typed refusal** (`STALE_DIAGNOSTICS_NOT_IMPLEMENTED`) rather than stubs;
- `diagnostic_stubs` is present and always empty in v1;
- the fresh-only guarantee — the property that actually matters — is unchanged and still tested:
  normal reads contain no stale protected metadata.

So the security surface is built and proven; only the diagnostic payload waits for a consumer who
asks for it. Acceptance items reading "privileged diagnostics return only its permitted stub" become
"a privileged diagnostic request is authorized, audited, and refused with a typed reason; an
unauthorized one is refused before source enumeration."

`ColumnSemanticLinkV1` states that "V1 relation kinds are admitted only when they have a named
consumer and an authority owner", then admits two that have neither: `converted_by` has no consumer
— the plan itself says it stays advisory until the formula grammar changes, and `FinalOperation` is
`identity`/`ratio`/`difference` only (`formula/schema.py:49-54`) — and `as_of`'s semantics are
explicitly undecided in the same paragraph. Rev 5 applies the rule to itself: **v1 admits
`denominated_in` only**, as an adapter over the shipped `currency_binding` fact. The other two are
added when their consumer exists.

### `OntologySourceScopePolicyV1`

The server-owned authorization and metadata-egress policy:

- stable policy id, version, and content fingerprint;
- explicit grants from authenticated subject/group/role to catalog sources;
- independent operations: `ontology_read`, `candidate_egress`, and `stale_diagnostic`;
- deny-by-default behavior for an absent source or operation;
- an explicit system execution principal for persistent candidate generation.

Requested `sources` are intersected with—not added to—the principal's entitlement. An empty request
means every source entitled for that operation, never every source in the database. Sensitivity
scope is applied after source entitlement and remains independently mandatory. Reviewer scope
cannot redefine the persistent system candidate universe.

### `OntologyServerLimitsV1`

**Extends the shipped `join_path` bounds; does not restate them.** Rev 4 specified this contract
field-for-field as new work while the same values, the same metadata shape and the same
deterministic-ordering rule were already merged. Rev 5 adopts the shipped module as the single
owner of table-neighbourhood bounds:

- **Reused verbatim from `overlay/upload/join_path.py`** — `MAX_HOPS_DEFAULT = 1`,
  `MAX_NEIGHBOUR_TABLES = 20`, `MAX_COLUMNS_CONSIDERED = 300`, `MAX_HOPS_CEILING = 3`, ordering by
  `(hop distance, table name)` before truncation with a stop-at-first-breach rule, typed
  `limit_reason`, and `JoinNeighbourhood.as_metadata()`. The ontology surface calls this module.
  If a limit needs to change, it changes **there**, once, for both consumers.
- **New in this contract — only the collections `join_path` does not bound:** returned
  nodes/edges, component members, candidates, identifier bindings per entity/source, source pairs,
  pairs examined, and LLM dispatches.
- stable pre-enumeration ordering and typed `limit_reason` values, matching `join_path`'s
  vocabulary rather than inventing a second one;
- a policy fingerprint carried by every response, candidate revision, and cursor.

Clients may request smaller values only. Startup refuses an absent, non-positive, internally
inconsistent, or unversioned limits policy. **E3.0 does not re-freeze the neighbourhood numbers** —
they were measured against a hub fixture at merge time (12,710 → 6,284 statements at 40 neighbours,
and flat when the fixture grew from 24 to 40 neighbours, i.e. bounded rather than merely smaller).
E3.0 freezes only the **new** maxima above, against the same fixture.
“Sort everything, then truncate” is not an implementation: input rosters are bounded and paged
before Cartesian pairing, and `pairs_examined` is decremented as work is attempted.

**Open scalability item inherited, not introduced, by this program:** the neighbourhood is bounded
but not cheap — the dominant remaining cost is ~2 queries per `(entity need × is_grain column)` via
`effective_entity`. Batching that is scheduled work outside this plan; E3 must not assume it.

Every bounded collection returns `considered`, `returned`, `available` when safely countable inside
the same bound (otherwise `unknown`), `truncated`, `max_hops`, `limit_reason`, and continuation state
when continuation is permitted.

### `OntologyMetadataRevisionV1` and `OntologyCursorV1`

`OntologyMetadataRevisionV1` is a **per-`catalog_source`** monotonic revision bumped in the same
transaction as every mutation capable of changing that source's contribution to an ontology
response: physical metadata ingest/reconciliation, freshness/watermark changes, relevant field
decisions, governed fact lifecycle transitions, candidate/currentness/proposal-link changes,
persisted LLM selection evidence, effective E3 projection changes, and source-entitlement changes.
A cross-source fact (a bridge) bumps **both** endpoint sources.

**It is deliberately not one global counter.** Ingest runs as one transaction holding a
`pg_advisory_xact_lock` keyed on the catalog source, held across the Pass-A LLM enrichment stages,
and it cannot be released mid-transaction — the in-code comment states both that "different sources
hash to different keys and never block each other" and that releasing across the LLM calls is
"NOT a safe small change", because a concurrent same-source ingest could slip into the release
window and be clobbered by the later whole-source `build_graph` rebuild (`ingest.py:1668-1686`).
A single global row inside that transaction would destroy exactly the property that comment
protects: every concurrent upload of every catalog would block on it for the duration of another
source's enrichment — minutes on a 126-column file. Per-source revisions preserve today's isolation
exactly.

Registry code changes are represented by registry content fingerprints — **which E3.Foundation must
first create.** No content fingerprint exists today (`entity_registry.GRAPH_VERSION` is a literal
with an explicit note that content hashing was omitted; `CONCEPT_REGISTRY_FOR_REALIZATION` is a
hand-maintained `"concepts@1"`), so editing a concept definition currently changes no fingerprint
anywhere. Until that lands, no cursor or candidate revision may claim registry-content binding.

Each request reads one revision **vector** — the revisions of the sources it actually read — under
one repeatable-read transaction. A later page starts a new transaction and is accepted only when
every source in its cursor's vector still matches. A source the page never read cannot invalidate
it.

`OntologyCursorV1` contains:

- metadata revision;
- normalized source filter and source-scope-policy fingerprint;
- authenticated read-scope fingerprint;
- freshness, registry, and server-limit policy fingerprints;
- normalized query/root, requested bounds, stable last sort key, original freshness-evaluation
  instant, `valid_until`, expiry, and signing-key id.

The serialized cursor is authenticated with a server MAC/signature and is never accepted as
client-authored state. Signature failure, expiry, identity/scope mismatch, policy change, registry
change, or a metadata-revision change **in one of the sources the page read** returns a typed
continuation refusal. No contract claims that a PostgreSQL transaction snapshot survives across HTTP
requests. API routes supply the server clock — clients cannot choose `now`.

**Freshness pinning, and why Rev 4's rule was self-defeating.** Rev 4 required `valid_until` to be
no later than the earliest freshness transition that could change the page's visible set. Freshness
is evaluated **per node** (`COALESCE(n.attested_at, w.last_completed_at) >= cutoff`,
`search.py:99-101`), and the revision bump matrix includes watermark changes, which fire on every
drift scan (`src/featuregen/overlay/catalog_changes.py`). On a live catalog a freshness transition is therefore
approximately continuous and `valid_until` collapses toward `now`. Yet E5.2 groups all realizations
and E5.3 builds components over the whole visible bridge set, both consuming **only** the paged
handoff and both forbidden to compute-then-filter — i.e. they *require* multi-page reads to
complete. Under Rev 4's rule a multi-page assembly could never finish on a live catalog.

Rev 5 pins the evaluation instant instead of chasing it:

- the cursor carries the **original freshness-evaluation instant**, and every continuation page
  evaluates freshness against that pinned instant, not against a moving `now`. A paged read is
  therefore internally consistent by construction;
- the pinned instant has a bounded **maximum age** (`freshness_pin_max_age`, a policy value). Past
  it, continuation is refused with a typed reason and the caller restarts. This bounds staleness
  explicitly rather than pretending it away;
- a **content** change (metadata revision in a read source, policy, registry, scope) still refuses
  continuation immediately. Only the passage of time is pinned;
- the response states the pinned instant and its expiry, so a caller can always tell how old the
  assembly it is holding is.

A read that cannot tolerate a pinned instant sets `require_complete_components=true` and takes the
single-page-or-refuse path.

### `PhysicalObjectIdentityV1`

A schema-preserving local identity whose identity-bearing fields are:

- `catalog_source`;
- attested physical `schema`;
- `table`;
- optional `column`.

Non-identity attachments carry `schema_authority` and the public-flattened `graph_ref` used by
existing graph readers.

The physical tuple is the physical-object identity and the base of a local-realization identity.
`graph_ref` is an attachment/read key, not physical identity. Unknown schema may appear as
display-only conflict context;
it cannot participate in an operational cross-catalog merge. E3 never silently
substitutes `public`.

### `PhysicalMetadataObjectV1`

The schema-preserving metadata substrate populated before E3 adapters run:

- `physical_object_id` over `(catalog_source, attested_schema, table, optional column)`;
- kind, table/column names, governed/effective metadata references, source snapshot, freshness,
  sensitivity, and provenance;
- optional `graph_ref` attachment plus proof that the physical-to-graph mapping is one-to-one;
- metadata revision and lifecycle status.

The backing projection is keyed by the full physical tuple. Ingest writes it atomically with the
existing flattened graph. Same-named objects in different attested schemas produce distinct rows;
they are no longer discarded merely because the display graph cannot key them. The flattened graph
may withhold an ambiguous attachment. Unknown schema remains quarantined/display-only and never
becomes an operational physical id.

**Hard prerequisite — the value source must exist before this contract can be populated.** This
store cannot be filled by a projection alone, because attested schema does not reach ingest on the
mainstream path: `CanonicalRow` has no schema field, `_headers._ALIASES` has no schema alias, and
the FTR/glossary reader quarantines cross-schema collisions in its own pass before any
`CanonicalRow` exists. E3.Foundation.0 delivers the ingest schema contract; this contract consumes
it. Coverage is therefore explicitly staged, and the plan states which sources are in scope at each
stage rather than implying catalog-wide operational identity from day one.

**Where per-field VALUES come from for a schema-preserving object.** Every shipped reader takes a
field's display value from the flat `graph_node` column and only its *authority* from the decision
layer — "the decision log stores only a value HASH, so a reader NEVER dereferences a decision's
load-bearing value" (`column_authority.py:1-8`; same pattern in `asset_detail.py:70-81`) — and
`graph_node` is PK'd on the public-flattened `(catalog_source, object_ref)`. So an object that gets
**no** graph attachment (the ambiguous-collision case this contract exists to represent) has no
flat column to read. E3.0 must decide and record which of these v1 takes:
(a) values for unattached physical objects come from `field_evidence.proposed_value` at the
schema-preserving `logical_ref` — available only for evidence-bearing fields, and only for rows that
survived the reader; or (b) unattached physical objects are identity-and-conflict-only in v1 and
carry no property values at all. Option (b) is the honest default until (a)'s coverage is measured.

### `OntologyPropertyV1`

One readable schema-preserving property. **This is an adapter over the shipped per-field authority
stack, not a new one.** `field_evidence` + `field_policies._POLICIES` +
`field_authority.resolve_field_authority` + `asset_detail.build_asset_detail` already produce
per-field effective value, authority, provenance and evidence for concept, definition, domain,
sensitivity, data_type, unit, currency, entity, additivity and temporal_role. E3 reuses that
resolver; it may not re-derive authority against tables.

- stable `property_id = physical_object_id` for the column;
- owning physical table/local realization;
- one `EffectivePropertyFieldV1` per returned field — concept, definition, domain, operational and
  declared type, sensitivity/classification, analytical behavior, unit/currency, and synonyms;
- optional conservative property summary derived from, but never replacing, its field envelopes.

### `EffectivePropertyFieldV1`

One field's envelope — the unit `OntologyPropertyV1` is assembled from. Rev 4 referenced this type
in three places and never defined it:

- `field_name` (a key present in `field_policies._POLICIES`);
- `value` — the resolved display value, read the way every shipped reader reads it (the flat
  column), never by dereferencing a decision's hashed load-bearing value;
- `AuthorityEnvelopeV1`, including the field's `influence_max` ceiling;
- `freshness` and `operational_eligibility` with typed reasons;
- evidence ids, decision id, and **root** provenance (a derived value is only as trusted as its
  root concept);
- `inherited: bool` plus the inheritance rule when the value was not asserted at this object;
- typed conflicts.

**Two gaps E3.Foundation must close before this contract is complete:**

1. **`semantic_terms` (synonyms) has no `_POLICIES` entry.** E1a writes synonyms as `llm/proposed`
   `semantic_terms` field evidence, but `policy_for('semantic_terms')` returns `None`, so there is
   no resolvable value or authority for the field this contract lists. Either add the policy —
   display rule, operational rule, conflict strategy, `influence_max` — or remove synonyms from the
   v1 envelope set. Do not ship a field whose authority is undefined.
2. **The shipped resolver is anchor-scoped and single-catalog.** `build_asset_detail` assembles one
   asset under one snapshot. E3 needs the same envelopes over a paged, source-entitled,
   cross-catalog result set. That is the real new work here — batching and paging the existing
   resolver, not re-implementing it.

Inherited values are labelled inherited and retain the authority of the source value plus the
inheritance rule. A human-verified concept cannot upgrade an LLM definition, an inherited domain,
or an ungoverned type. Visibility is decided before any other field, count, facet, or property
summary is returned — **using the same predicate the rest of the platform uses.** Note honestly
that today that predicate reads `graph_node.sensitivity` (the raw tag), not
`effective_restriction` (the governed floor); Correction 25 governs whether E3 changes that or
inherits it, and this contract must state which, because "the effective governed sensitivity"
is not what any shipped read-scope filter consults.

### `OntologyPropertyPageV1`

The bounded Foundation read contract that lets E5.1 ship before the relationship-bearing E3
handoff:

- readable `OntologyPropertyV1` records and their permitted physical table context;
- the immutable concept-registry snapshot needed by those records;
- typed conflicts and privileged stale stubs;
- metadata revision, source/freshness/limit policy fingerprints, signed cursor, and honest
  totals/truncation.

Public/internal service:

```python
read_ontology_properties(
    conn,
    *,
    identity: IdentityEnvelope,
    now: datetime,
    concept_ids: tuple[str, ...],
    include_descendants: bool,
    sources: tuple[str, ...] = (),
    freshness_policy: OntologyFreshnessPolicyV1,
    bounds: OntologyReadBoundsV1,
    cursor: str | None = None,
    stale_diagnostics: bool = False,
) -> OntologyPropertyPageV1
```

Routes supply the server clock. This service uses the same source-entitlement, sensitivity,
freshness, metadata-revision, signed-cursor, and server-limit implementation as the later E3
handoff; E5.1 may not reimplement those rules against tables.

### `ObjectTypeBindingV1`

The governed singleton selecting which canonical object type one physical table models:

- one fact aggregate keyed by `(physical_table_id, "object_type_binding")`;
- selected canonical `entity_id` in the fact value;
- exact current complete-grain fact key and confirmed event id as dependencies;
- the grain's `is_unique` must be `true` (Correction 18) — a non-unique grain is not a key and
  cannot establish object identity;
- authority envelope, freshness, provenance, conflicts, and operational eligibility.

The binding is operational only while the same physical table, canonical entity, complete ordered
grain, and exact confirmed grain event remain current and eligible. A grain or entity change demotes
the realization before it can be read operationally. Column `entity_assignment`, concept
`entity_link`, and advisory `primary_entity` may propose this binding; none authorizes it by itself.

**Singleton semantics — corrected in Rev 5.** Rev 4 specified confirmation-time compare-and-swap.
The shipped spine has none: `propose_fact` denies **at propose time** whenever a non-terminal fact
exists for the `fact_key`, and sticky-denies a rejected proposal fingerprint
(`proposal_commands.py:34-40, 71-80`). Since `fact_key` hashes `(ref, fact_type, use_case)` only and
the selected entity lives in the **value**, two competing object types collide on **one** key and
the second is refused before confirmation. The singleton is therefore already guaranteed — by
first-writer-wins at propose, not by a race at confirm.

What this changes, and must be specified rather than glossed:

- the product behaviour is **"first proposal blocks all rivals until explicitly rejected"**, which
  needs a visible reject path in the review surface — otherwise one wrong early proposal silently
  locks a table's object type;
- E3.0 must decide whether that denial is the desired UX or whether object-type binding needs a
  `correct`-style supersede path like the one semantic bindings already have
  (`governance.py:442`);
- acceptance tests assert **propose-time denial and the reject-then-repropose path**. Rev 4's three
  CAS acceptance items are deleted: they described a race the system cannot exhibit.

**No source-assertion escape hatch in v1** (Correction 23). `source_declared` does not write or
revalidate this aggregate. Today `primary_entity`/`entity` are advisory with
`influence_max = RECOMMENDATION`, structurally enforced; admitting a source assertion here would be
a net authority widening and would re-open the single-actor upload→approve round trip that
`uploader_ne_confirmer` exists to close. Governed confirmation only.

### `ObjectTypeBindingCandidateV1`

An advisory table-level object-membership proposal:

- stable candidate id over physical table and canonical `entity_id`;
- immutable candidate revision over current complete-grain evidence, supporting column/entity
  evidence, metadata snapshot, registries, derivation version, and policy versions;
- disposition, authority envelope, freshness, provenance, and conflicts.

Governed confirmation writes the selected entity into the one table-scoped
`ObjectTypeBindingV1` aggregate. **There is no source-assertion path in v1** (Correction 23): a
source assertion here would be a net authority widening over the shipped
`influence_max = RECOMMENDATION` ceiling on `primary_entity`/`entity`, and would re-open the
single-actor upload→approve round trip. The candidate remains immutable audit evidence.

### `SemanticObjectTypeV1`

The E5-owned stable business-object read model, assembled from the E3 handoff:

- `object_type_id = "entity:<entity_id>"`;
- canonical `entity_id` from `known_entities()`;
- registry display label;
- visible local realization ids;
- authority/conflict summary derived from those realizations.

It does not contain a global data-title property and does not imply that all local
realizations share an interoperable key namespace.

### `LocalObjectRealizationV1`

One physical table modelling one semantic object:

- `local_realization_id` over `(physical_table_id, canonical entity_id)`;
- immutable `local_realization_revision_id` over that stable id plus the exact complete-grain
  identity/event and other revision-bearing evidence;
- canonical `entity_id` and its table-level `ObjectTypeBindingV1`;
- complete ordered governed grain columns and their fact/event provenance;
- zero or one operational **local** title-binding id;
- readable properties;
- identifier bindings;
- freshness, authority axes, and conflicts.

A table becomes an operational object realization only when its current complete governed
grain and table-level `ObjectTypeBindingV1` are operationally eligible. A foreign-key/
reference column never changes the table's object type. A composite grain is attached
only as the complete ordered tuple. E3 never attaches an E5 namespace component.
Changing the grain changes the revision, not the stable realization id. Changing the selected
entity demotes the old realization and creates/revalidates the stable id for the new entity. Any
old title binding remains audit evidence but is ineligible until explicitly revalidated against
the new realization revision.

### `LocalObjectRealizationCandidateV1`

**A projection, not a second candidate identity.** Rev 4 gave this type and
`ObjectTypeBindingCandidateV1` the *same* stable id — "over the physical table and canonical
`entity_id`" in both — i.e. two contracts competing for one identity, which would have produced two
rows in the candidate substrate for one proposal. Rev 5 keeps exactly one stored candidate:
`ObjectTypeBindingCandidateV1`. This type is a **read-side view** over it joined to its grain
evidence, carrying:

- the underlying `object_type_binding_candidate_id` (it has no id of its own);
- schema-preserving table identity and complete ordered grain, when available;
- authority envelope, freshness, disposition, and conflicts;
- readable advisory property/title-candidate references.

It never receives a namespace-component attachment and never enters an operational
object set. Governed confirmation produces/revalidates the corresponding
`LocalObjectRealizationV1`; the candidate remains as immutable audit evidence. **No source-assertion
path** (Correction 23).

### `TitlePropertyBindingV1`

The governed selection of one realization's display column:

- one stable governed fact identity keyed by
  `(local_realization_id, "title_property")`, never by the candidate property;
- the selected schema-preserving property identity in the fact value;
- the exact local-realization revision/object-binding/grain event ids it was verified against;
- schema-preserving local realization and property identities;
- authority envelope, freshness, provenance, and conflict reasons;
- operational eligibility.

The property must belong to the same local realization. The realization-scoped fact
aggregate can select at most one operational property. There is no global
title property on `SemanticObjectTypeV1`. A realization-revision change demotes the title until
same-table membership and applicability are revalidated.

**Singleton semantics — same correction as `ObjectTypeBindingV1`.** Enforcement is the shipped
propose-time denial on a shared `fact_key`, not a confirm-time CAS. Two competing title properties
collide on one key (the selected property lives in the value) and the second proposal is refused.
The acceptance test is propose-time denial plus the reject-then-repropose path; Rev 4's "two
concurrent confirmations yield exactly one winner and one typed CAS conflict" is deleted.

**`(local_realization_id, "title_property")` is not an expressible ref today.** A realization-scoped
ref must carry `entity_id`, but every governed ref is a
`CatalogObjectRef(catalog_source, object_kind, schema, table, column)` and
`_CATALOG_OBJECT_REF_SCHEMA` sets `additionalProperties: False`. This is a **new ref type**, not a
new value schema: it needs `_ref_from_payload` decoding, `fact_key` canonicalization, and updates to
every poller that decodes payload refs. E3.Foundation's registration work must name it explicitly.

### `TitlePropertyBindingCandidateV1`

One advisory title candidate:

- stable candidate id over the local realization and proposed property;
- immutable revision id over the stable id, source snapshot, endpoint/evidence
  fingerprints, derivation versions, and model/config policy where applicable;
- schema-preserving local realization and property identities;
- disposition, authority envelope, freshness, provenance, and conflict reasons.

Many advisory candidates may coexist. Confirmation writes the selected property into
the one realization-scoped `TitlePropertyBindingV1` aggregate; a candidate is never
itself the operational fact.

### `CandidateIdentityV1`

The migrated shared candidate substrate used by existing semantic bindings and new E3 kinds:

- `stable_candidate_id` over candidate family, canonical semantic endpoints/roles, and semantic
  target only;
- `candidate_revision_id` over the stable id plus evidence/source snapshots, endpoint fingerprints,
  registry and derivation versions, model/prompt/config, egress policy, and current dependencies;
- immutable candidate-set/revision rows;
- CAS currentness keyed by `(candidate_family, scope_kind, scope_id)`, not table alone;
- proposal links from the exact candidate revision to the governed fact/event it justified.

Algorithm, model, prompt, policy, observation, and snapshot versions never enter the stable id.
Identity-schema versions may enter it only when endpoint canonicalization semantics change.

**The backfill cannot be an UPDATE — corrected in Rev 5.** `semantic_binding_candidate_set` and
`semantic_binding_candidate` are physically immutable: `BEFORE UPDATE OR DELETE ... RAISE
EXCEPTION` row triggers plus `REVOKE UPDATE, DELETE, TRUNCATE ... FROM featuregen_app`
(`1014_semantic_binding_candidate.sql`). Adding a nullable column is DDL and fine; **populating it
on existing rows is an UPDATE the store refuses for every role under normal
`session_replication_role`.** Rev 4 named no side table, no derive-on-read mapping, and no
trigger-suspension policy. Rev 5 requires the WORM guarantee to be preserved:

- existing candidate ids are retained as legacy revision ids and **mapped**, not rewritten;
- the stable id for a legacy row lives in a **side mapping table**
  `semantic_binding_candidate_stable_id (candidate_id PK, stable_candidate_id, derived_at,
  derivation_version)`, written once and never updated;
- readers resolve `legacy candidate_id → stable_candidate_id` through that mapping, so rebuild and
  currentness stay byte-comparable against a pre-migration control;
- **suspending the WORM triggers is prohibited.** If a design requires mutating those rows, that
  design is wrong.

**`scope_kind=physical_table` is a conflation this plan forbids elsewhere.**
`current_semantic_binding_candidate_set` is keyed `PRIMARY KEY (catalog_source, table_graph_ref)`
(`1014:120-128`) — a **public-flattened graph ref**. Backfilling those rows as
`scope_kind=physical_table` asserts that `graph_ref` *is* physical identity, which
`PhysicalObjectIdentityV1` explicitly denies ("`graph_ref` is an attachment/read key, not physical
identity"), and for exactly the same-named cross-schema rows Foundation exists to rescue the mapping
is many-to-one and unrecoverable. Legacy rows therefore backfill as `scope_kind=graph_table`, a
distinct and honestly-named scope. Only rows whose physical mapping is proven one-to-one may be
re-scoped to `physical_table`, and that re-scope is a forward migration with its own proof — never
part of the backfill.

Currentness applies to deterministic bounded candidate rosters. An LLM selection is immutable
evidence over a roster revision, not a second competing “current candidate set.”

### `LLMSelectionEvidenceV1`

The immutable result of selecting/labeling a bounded candidate roster:

- selection revision id and candidate-roster revision ids;
- durable `llm_dispatch.dispatch_ref` and idempotency key;
- exact sanitized request hash, response hash, model/prompt/schema/config versions, and egress
  policy fingerprint;
- validated selected candidate ids, closed labels, disposition, bounded rationale,
  completion status, and provider diagnostics;
- `confidence`, **for review routing and display only.** It may order a review queue. It may never
  feed `operational_eligibility` (Correction 22) — that is the confidence-gated auto-attestation
  direction this codebase has already reviewed and deliberately split apart.

The first valid persisted result for one selection revision is reused on replay. A transient retry
uses the same dispatch/idempotency key. A deliberate re-evaluation creates a new selection revision
with an explicit cause. Provider output never enters stable candidate identity.

**The store this depends on must be BUILT — corrected in Rev 5.** Rev 4 described "the existing
durable dispatch store" and made replay an acceptance criterion. Verified against main:
`llm_dispatch` persists `redacted_input` and **no response body**; `llm_dispatch_outcome` records
only `response_received|transport_failed`; `UNIQUE(logical_call_ref, attempt_no)` is not
content-addressed because `logical_call_ref` is a fresh `mint_id("lc")` per invocation, and it is
documented as one record **per attempt** — a retry deliberately mints a new key, the opposite of
replay; the only store with `raw_output` is `llm_call`, keyed `(run_id, task, input_hash)` with
`run_id NOT NULL`, which a run-less system principal cannot form; and `find_llm_call`, the one
full-identity probe, has **zero production callers** and includes `run_id` in its identity.

E3.Foundation therefore delivers, as named work:

- a **content-addressed selection key** derived from the selection revision (roster revision ids +
  sanitized request hash + model/prompt/schema/config versions + egress policy fingerprint) — not a
  minted id, and not `run_id`-scoped;
- persistence of the **validated response body and its hash**;
- a reuse probe wired into the dispatch path, with a transient retry reusing the key and an
  explicit re-evaluation minting a new selection revision.

If that work is descoped, E3.2's replay acceptance criterion is deleted with it. The plan does not
keep an acceptance test whose mechanism is unfunded.

### `IdentifierBindingV1`

One ordered, non-empty tuple participating in an entity identifier namespace:

- stable binding id over its canonical entity, role, owning physical table, and ordered
  schema-preserving column tuple;
- canonical `entity_id`;
- role: `object_grain`, `foreign_key`, or `reference`;
- owning local realization;
- ordered columns and key-shape/type evidence;
- concept/entity/grain provenance;
- freshness, visibility, and authority axes.

Length one represents a single-column identifier; there is no separate scalar form. This is the
attachment point between physical columns, object realizations, and namespace bridges. The
`object_grain` role is valid only when the tuple exactly equals the realization's complete ordered
governed grain; one member of a composite grain is never an `object_grain` binding by itself.

### `IdentifierNamespaceBridgeV1`

A sanctioned equivalence between two identifier bindings for the **same** entity:

- stable unordered bridge/fact identity;
- left and right binding ids;
- the ordered `left_column → right_column` pairs, preserved as pairs;
- canonical `entity_id`;
- VERIFIED bridge fact/event provenance;
- key-shape/type compatibility;
- freshness and operational eligibility;
- conflict reasons.

It has no direction, business predicate, or `RealizationCardinalityV1`. A generic
edge renderer may display its cardinality as `not_applicable`. Connected components
are built over these namespace bridges, not over tables. Canonicalizing the unordered endpoints
may swap the two complete tuples and invert every pair; it never sorts columns within a tuple.
Every member pair must be present, shape/type-compatible, and backed by the same governed bridge
revision. Independent single-column facts never synthesize a composite bridge.

A new `identifier_namespace_bridge_v2` fact represents ordered composite mappings and uses the same
propose/confirm/demote lifecycle. Its ref is **tuple-scoped** — another new ref type, subject to the
same `_ref_from_payload` / `fact_key` / poller work as the realization-scoped title ref.

**Four shipped facts constrain what "adapt the existing bridge" can mean.** Rev 4 asserted the
adaptation as settled; each of these needs an explicit E3.0 decision:

1. **Cross-catalog only.** `_bridge_write_error` refuses same-source pairs
   (`identity.py:131-133`), `derive_bridge_candidates` skips them (`bridge_candidates.py:98`), and
   migration `0989` enforces `CHECK (left_catalog_source <> right_catalog_source)`. So
   `sales.orders.id ↔ hr.orders.id` under one source — the motivating example for physical
   identity — **cannot be expressed by the shipped fact at all.** Intra-source namespaces are new
   capability, with their own schema change and their own budget.
2. **`public` is fabricated into existing identities.** `bridge_candidates._col_ref` hard-codes
   `schema="public"` and that ref is hashed into `fact_key`, with `entity_bridge_edge` storing only
   flattened endpoint refs. Under this plan's own rule that unknown/substituted schema cannot be
   operational, **every existing bridge is non-operational.** E3.0 chooses: re-attest legacy bridges
   against real schema, or admit them under a named, audited, time-boxed legacy exception. It does
   not quietly adapt them as compliant one-element tuples.
3. **The candidate has no identity evidence.** Matching is concept group `identifier` + identical
   `entity_link` + coarse type family (which collapses `int4` and `bigserial`). The write gate
   checks only cross-source-ness and ref/value consistency — it does **not** verify
   `entity_id ∈ known_entities()`, that the endpoints are identifier-concept columns, or that they
   exist (contrast `_entity_assignment_write_error`, which does). Since E5.3 unions bridges
   transitively, one wrong bridge merges a whole component. Correction 19 governs: key-shape,
   format and uniqueness evidence on the candidate, and a real write gate.
4. **Until 2026-07-28, a glossary-sourced catalog could not produce a bridge candidate at all.**
   `_identifier_columns` classified every column by `graph_node.data_type` alone. A glossary
   upload attests no physical type — the FTR adapter emits `CanonicalRow.type='unknown'` and puts
   the file's own answer in `graph_node.declared_type` — so every glossary column resolved to the
   `other` family and was dropped before pairing. Measured on the deployed FTR catalog: 126/126
   columns `data_type='unknown'`, 113 of them `declared_type='string'`, and **0 of the 28
   identifier-concept columns eligible**, including the single `customer_id` (`cif_id`) the whole
   cross-catalog story depends on. Loading a second catalog would have yielded zero suggestions
   with no visible reason. **Fixed** (`e6e5f31d`): the attested type still decides whenever it
   classifies, `declared_type` is consulted only when nothing was attested, an unclassifiable
   declared value is still excluded, and `BridgeCandidateV1.type_basis`
   (`attested|declared|mixed`) records the basis on the proposal evidence for the human confirmer.
   Verified 0 → 28 eligible on the real catalog.
   **The general lesson for E3, which is why this is recorded here rather than only in git:** the
   platform stores *declared* and *attested* metadata in separate columns on purpose, and a reader
   that consults only one silently excludes an entire class of source. Every E3 adapter that reads
   a physical attribute — type, schema, grain, nullability — must state which of the two it reads
   and what becomes of the sources that populate the other. `PhysicalMetadataObjectV1` and
   `IdentifierBindingV1`'s "key-shape/type evidence" both need that decision written down, and
   E3.0's falsification pass must check it for each adapter it adopts.
5. **There is no way to create one in the product.** No bridge route exists in `api/routes/`;
   confirmation is single-platform-admin with dual sign-off deferred "to 3C, when a bridge becomes
   live-traversable". E5 is what makes them live-traversable. E3.4 ships the governance route and
   E3.0 sets the confirmation rule (Correction 20). **Until both land, every E5 slice built on
   VERIFIED bridges is demonstrable only on fixtures** — which the plan states plainly rather than
   discovering at E5.8.

### `IdentifierNamespaceBridgeCandidateV1`

A bounded proposal that two identifier bindings may belong to the same namespace:

- stable `candidate_id` over candidate kind, canonical unordered binding ids, and canonical
  `entity_id`;
- immutable `candidate_revision_id` over the stable id plus every source snapshot,
  endpoint fingerprint, supporting-evidence dependency, registry fingerprint,
  derivation algorithm version, generation-principal policy version, and shortlist/config version;
- left and right binding ids and canonical `entity_id`;
- proposal evidence, disposition, freshness, and authority envelope;
- key-shape/type compatibility and conflict reasons.

It is advisory and never participates in a resolution component. Confirmation creates
or updates a governed bridge fact; only the resulting VERIFIED
`IdentifierNamespaceBridgeV1` is operational. A dependency or policy change supersedes
the candidate revision and revalidates the stable candidate; it never creates a second
semantic candidate solely because an observation changed.

### `EntityLinkTypeV1`

A semantic relationship between two business-object roles. The entity types may be
different or equal:

- stable `link_type_id`;
- `from_entity` and `to_entity` from `known_entities()`;
- stable `from_role` and `to_role`, required to distinguish a same-type relationship;
- forward label and optional inverse label;
- semantic category;
- status and version;
- authority and provenance.

It deliberately carries **no universal cardinality**.

A same-type link such as `employee(manager) → employee(report)` is a business
relationship, not an identifier bridge. Its physical realization must use distinct
endpoint roles and a named predicate; identity/equality still belongs exclusively to
`IdentifierNamespaceBridgeV1`.

The five current planner relationships adapt only as
`semantic_category=rollup`, `forward_label=rolls_up_to`. A richer predicate such as
`owned_by` requires its own source/human semantic evidence.

### `RealizationCardinalityV1`

The cardinality attached to one physical link realization:

- value: `one_to_one`, `one_to_many`, `many_to_one`, `many_to_many`, or `unknown`;
- basis: `explicit`, `defaulted`, or `missing`;
- direction relative to the semantic link;
- `AuthorityEnvelopeV1` and exact evidence provenance.

Derivation and verification are read from the authority envelope, so a source-declared
value may also be human-verified without losing either fact. Operational eligibility is
computed by the current authority policy. The existing `None → MANY_TO_ONE` planner
default is exposed, if at all, as `(many_to_one, basis=defaulted)` and remains advisory.
It is never relabelled explicit.

**This contract is unimplementable until the `approved_join` fact changes** (Correction 17). Four
shipped facts each independently defeat it, and Rev 4 scheduled no fact-schema work:

1. `graph.py:93` fabricates the default **at propose time** — `cardinality=row.cardinality or
   "N:1"` — so the governed fact VALUE already asserts `N:1` for a blank upload. `basis=missing` can
   never be observed downstream.
2. The `approved_join` value schema **requires** cardinality and admits only `1:1|1:N|N:1`
   (`overlay/facts.py:100, 118`). There is no `unknown` and no `basis` field to write.
3. After dual confirmation the projection **overwrites** the honest `NULL` on
   `graph_edge.cardinality` with the fabricated value (`passc/projection.py:139-149`), erasing the
   raw missing-ness from the edge too.
4. `derive_catalog_realizations` discards the token
   (`catalog_realizations.py:37-43, 189, 206`); `Cardinality` has no `unknown` member.

**And cardinality is part of the fact's IDENTITY**, hashed into `fact_key` (`identity.py:99`), so a
correction mints a second VERIFIED fact on the same column pair rather than demoting the first;
`passc/projection.py:109-118` then picks `min(verified, key=fact_key)` — the lower sha256 — and
`join_drift` compares only `from_ref → to_ref`, so an `N:1 → 1:N` flip on re-upload raises no
divergence at all.

E3.Foundation therefore owns a migration and a fact-schema change: remove cardinality from
`approved_join` identity, admit `unknown` plus an explicit `basis` in the value, stop the
propose-time fabrication, and make a cardinality correction demote-and-supersede rather than fork.
This is safety work — a wrong `1:N` silently corrupts every aggregation built on it — and it is a
funding gate for any surface that displays cardinality with a basis.

### `EntityLinkRealizationV1`

One physical realization of an entity link:

- `link_type_id` plus the exact `link_type_version`;
- schema-preserving from/to object references and identifier-binding ids;
- the ordered tuple of physical `from_column → to_column` pairs, preserving each pair
  as a unit;
- `RealizationCardinalityV1`;
- backing kind, fact key, confirmed event id, and backing identifiers
  (`approved_join` or catalog realization);
- authority, provenance, freshness, and operational eligibility;
- conflict reasons, if any.

An identifier bridge is never an `EntityLinkRealizationV1`. A composite realization is
operational only when every required ordered pair is present, compatible, and backed;
partial or independently re-ordered pairs are conflicts.

### `ColumnSemanticLinkV1`

A typed relationship between two schema-preserving physical columns:

- stable link identity;
- closed relation kind plus its registry/version pin;
- directed from/to logical references;
- backing fact/candidate reference;
- authority, provenance, freshness, and operational eligibility.

V1 relation kinds are admitted only when they have a named consumer and an authority
owner. At minimum:

- `denominated_in` is an adapter over the existing governed `currency_binding` fact,
  not a second source of truth;
- a new relation such as `converted_by` may use the E3 fact lifecycle, but remains
  advisory to feature generation until the formula grammar can express it;
- `as_of` must not duplicate `availability_time`: the E3 design must state whether it
  describes a value's semantic date or the table's knowledge-time basis.

### `AuthorityEnvelopeV1`

Authority is a product of independent axes, not one ordered tier. **Every axis is a lossless
superset of the shipped enums** (Correction 21) — Rev 4's were lossy in both directions:

- `derivation`: `source`, `registry`, `rulebook`, `llm`, `human`, `legacy`, and — added in Rev 5 to
  cover `EvidenceProducer` (`overlay/evidence.py:15-30`) — `profiler`, `parser`,
  `structural_connector`, `taxonomy`. These are not cosmetic: `structural_connector` proposes
  **every** bridge and Pass-C join, and folding the sampled-statistics `profiler` into `source`
  would misattribute an observation as an attestation.
- `verification`: `proposed`, `verified`, `rejected`, `expired`, `unknown_legacy`, and — added in
  Rev 5 — `partially_confirmed`, `reverify`, `stale` (the fold states in `overlay/state.py:67-124`
  and `_types.py:33-42`). Mapping `partially_confirmed` to `verified` is an outright fail-open;
  mapping a drift-STALEd fact to `verification=verified, freshness=stale` renders a **demoted** fact
  as verified.
- `freshness`: `fresh`, `stale`, or `unknown`;
- `operational_eligibility`: `eligible`, `advisory`, `ineligible`, or `conflicted`, plus typed
  reasons;
- **`influence_max`** — the field's influence ceiling, carried explicitly (Correction 22). The
  shipped kernel keeps two independent guarantees: the `operational_rule` **and** the
  `InfluenceTier` ceiling, documented as "belt AND braces — the ceiling is the hard guarantee, the
  `operational_rule` documents intent" (`field_policies.py:58-65`) and enforced structurally at
  `field_authority.py:296-297` *before* any rule runs. Rev 4 reproduced the rule and dropped the
  ceiling, leaving eligibility to an unwritten policy. **No policy may raise
  `operational_eligibility` above `influence_max`.** LLM `confidence` is never an input to it.
- original producer/strength, fact key, event id, evidence id, decision id, root
  evidence, and authority basis where present.

A contract test asserts the mapping is **total and injective** in both directions against the
shipped enums, and fails when a new `EvidenceProducer` or fold state is added upstream without a
slot here. `legacy_unspecified` remains `verification=unknown_legacy`; it is never rewritten as
human-verified. Registry-curated and rulebook-derived are distinct derivations.
Staleness never erases whether a fact was proposed or verified. Read visibility is
evaluated separately; hidden records are filtered before response assembly rather than
represented as an authority value.

### `OntologyFreshnessPolicyV1`

The explicit freshness input to every normal or diagnostic ontology read:

- stable policy id and version;
- per-source or default `fresh_within` duration;
- accepted watermark/attestation bases;
- overlay and E3 projection-readiness requirements;
- diagnostic-stub permission name.

`now` is an observation; it is never a substitute for the policy. A source is configured when it
has an explicit entry or the policy contains an explicit versioned default. A source covered by
neither is refused; the service never inherits `/search`'s convenience default implicitly.

### `StaleObjectStubV1`

The only type a privileged stale-diagnostic read may return for stale catalog content:

- catalog source and policy-approved object display identity;
- stale reason and last-attested freshness instant (never mislabelled “last safe”);
- no property, key, title, entity membership, bridge/link, backing reference, child
  count, facet, sensitivity-derived field, or namespace information.

Diagnostic stubs live in their own collection and can never be decoded as
`LocalObjectRealizationV1`.

### `OntologyReadBoundsV1`

The caller's functional, deterministic request bounds, always clamped by
`OntologyServerLimitsV1`:

- explicit maximums for returned nodes, edges, candidates, and per-entity/source-pair
  candidate dispatch;
- stable sort keys and a signed `OntologyCursorV1`;
- root/source filters and maximum neighborhood depth;
- a `require_complete_components` flag for callers that intend to assert namespace
  connectedness.

A cursor is bound to the authenticated read-scope fingerprint, normalized source filter,
source-scope policy, freshness policy, registry fingerprints, server limits, and metadata revision.
It is rejected after any of those change. Each page is node-closed: an edge is returned only when
both endpoints are present. A truncated component is marked incomplete and has no authoritative
component id or connectedness claim. With `require_complete_components=false`, the page may return
that explicit incomplete view. With `true`, the service returns the complete component only if it
fits the server hard component limit; otherwise it refuses with
`COMPONENT_EXCEEDS_SERVER_BOUND` and never asserts partial connectedness.

### `E3OntologyInputsV1`

The single E3→E5 handoff envelope:

- an immutable `entity_registry_snapshot` containing the canonical entity refs needed
  by the returned endpoints;
- schema-preserving `physical_objects` and `properties`;
- operational `object_type_bindings`;
- advisory `object_type_binding_candidates`;
- operational `local_realizations`;
- advisory `local_realization_candidates`;
- `title_property_bindings`;
- `title_property_binding_candidates`;
- `identifier_bindings`;
- `namespace_bridges`;
- `namespace_bridge_candidates`;
- `entity_link_types`;
- `entity_link_realizations`;
- `column_semantic_links`;
- privileged `diagnostic_stubs`, empty for normal reads;
- typed conflicts;
- concept/entity/link registry versions and content fingerprints;
- metadata revision, authoritative overlay event head, overlay/E3 projection
  checkpoints, and consistency verdict;
- response freshness/source-scope/server-limit policy fingerprints, signed collection cursors, and
  truncation/completeness state.

Public service:

```python
read_e3_ontology_inputs(
    conn,
    *,
    identity: IdentityEnvelope,
    now: datetime,
    sources: tuple[str, ...] = (),
    freshness_policy: OntologyFreshnessPolicyV1,
    bounds: OntologyReadBoundsV1,
    cursor: str | None = None,
    stale_diagnostics: bool = False,
) -> E3OntologyInputsV1
```

The authenticated identity is threaded from the request and never minted. Ontology v1 refuses a
non-`None` identity tenant. `sources=()` means all sources permitted by
`OntologySourceScopePolicyV1` for this identity and operation, never an unscoped all-source read.
Normal reads are fresh-only under the explicit policy.
`stale_diagnostics=True` requires its separate named permission and returns
`StaleObjectStubV1` records only—never stale protected metadata or counts.

The service executes each page in one repeatable-read snapshot. It captures registry/content
fingerprints, the metadata revision, authoritative event head, and projection checkpoints before
assembly. A continuation cursor is verified before any graph work. Operational rows
are never trusted from a projection alone: the service verifies the backing fact's
folded current state and exact confirmed event id. A lagged/degraded checkpoint, changed
fingerprint, mismatched event id, or status other than currently eligible fails closed;
the service returns no operational graph under a mixed snapshot. The metadata revision is checked
again when a later page starts; no database snapshot is claimed to survive between requests.

---

## Phases that ship now, on shipped machinery

E0 and E0b depend on nothing in E3. They run against what is already merged, they are the only two
phases a user can contradict in days rather than months, and they are where the programme earns the
right to continue.

### Phase E0 — Semantic map v0

**Purpose**

Put the Outcome's first promise in front of a user in days, not after six phases. Rev 4's build
order shipped nothing user-visible until step 6 of 9 — docs → storage → adapters nobody consumes →
candidates with no review UI → facts with no UI → handoff service → headless read models → first
screen — and gated its own designated "earliest payoff" (E5.1) behind an eight-item Foundation gate,
none of which a concept query needs. That is the failure mode this program exists to avoid.

**Why it is nearly free.** Outcome item 1 — "find every fresh, source-entitled, readable property
classified as a concept across catalogs" — is approximately one facet from shipping. `search()` is
already cross-catalog, already fresh-only, already read-scoped, and already returns `n.concept` in
its hit projection (`overlay/upload/search.py:33-38`); the UI already renders it
(`SearchScreen.tsx:435`). `concept` is simply absent from `_COLUMN_FACETS` (`search.py:15-22`).

**Work**

- Add self-parent and `is_a` cycle rejection to `concepts._validate_registry`, which today checks
  only duplicate names, `is_a` resolvability, and that `CONCEPTS` mirrors the registry keys
  (`concepts.py:856-867`). It runs at import and fails fast, so this is a cheap, self-contained
  change — and it is a prerequisite for **any** descendant traversal anywhere in the program,
  including E5.1's.
- Add `concept` to `_COLUMN_FACETS` so a user can filter and facet the existing cross-catalog search
  by business concept.
- Add exact-vs-descendant concept selection, ordered **after** the validation above so descendant
  traversal never runs over an unvalidated graph. (Both land in E0; the ordering is within the
  phase, not a gate between phases.)
- Do **not** add a new read model, cursor protocol, freshness policy, or property contract here.
  E0 is a facet on a shipped, already-scoped query. Everything else is E5.1's job on the Foundation
  service.

**Acceptance**

- One concept filter returns matching readable columns from two catalogs in the existing search UI.
- A restricted column is absent from hits, counts and facets under a caller without the role
  (inherits `search()`'s existing read-scope test).
- A stale source is absent (inherits `search()`'s existing fresh-only behaviour).
- A self-parent and a two-node `is_a` cycle fail registry validation.
- **Measured baseline published:** what fraction of catalog columns carry a concept today, per
  source. This number is the honest ceiling on E0's usefulness and the baseline every later phase is
  measured against.

**Why E0 does not make E5.1 redundant.** E0 is a facet over one flattened, single-authority
projection. E5.1 adds per-field authority envelopes, schema-preserving identity, source entitlement,
an explicit freshness policy, node-closed pagination and signed cursors. E0 buys the time to build
that properly, and — more importantly — tells us whether anyone actually wants the semantic map
before the program spends six phases on it.

---

### Phase E0b — Identifier-link admission, critic panel, and proposed-link planning

**Purpose**

Make cross-catalog identity links flow all the way to features, and make what flows trustworthy
enough to be worth flowing. This phase runs against the **shipped single-column `entity_bridge`**,
not E3's tuple bridges, so it does not wait for Foundation.

*(Folded in from the standalone bridge-critic spec, 2026-07-28. That document is retired — this is
the single home for identifier-link admission.)*

**Directives**

1. Links are created by the system, shown as `proposed`, and used by the feature planner, feature
   generation **and materialization** regardless of confirmation status. Human confirmation changes
   status; it is not an admission gate.
2. A link only becomes `proposed` if it survives strong evidence — deterministic corroboration plus
   an independent LLM critic panel that can say no.

**Decisions**

- **D1 — Proposed links reach the planner.** `active_bridges` widens from VERIFIED-only to
  `VERIFIED | PROPOSED`, each row carrying its status.
- **D2 — The critic gates admission, not features.** A `no` prevents the link from becoming
  `proposed` at all, so it never reaches the planner; it remains visible with reasons and is
  human-overridable. The AI therefore only ever *narrows* what features may join — the filter sits
  before the planner, not as a gate after it.
- **D3 — Features on unconfirmed links are created and flagged, never blocked.**
  `JOIN_IDENTITY_UNCONFIRMED` puts the feature in `NEEDS_EXTERNAL_VALIDATION` naming the link it
  rests on; confirmation clears it, rejection demotes. This is the E4a unit loop applied to identity.
- **D4 — Materialization runs on unconfirmed links too. No gate anywhere.** *(User decision, after
  the alternative was raised and declined.)* What replaces the gate is provenance that cannot be
  separated from the output: the requirement serializes onto the governed contract via
  `contract/_serial.py::requirements_to_json` and travels onto the materialized artifact beside
  `access_requirements` — the pattern `materialize/classify.py:40` already establishes. Stated
  plainly: a wrong link produces numbers that look entirely reasonable, and for glossary catalogs
  the evidence is semantic rather than value-level. The panel and grounding keep the false-match
  rate down; the flag is what makes a mistake findable afterwards.
- **D5 — The critic's verdict is evidence, not authority.** Recorded with reasons, replayable,
  human-overridable in both directions, override audited.
- **D6 — A panel of two critics with different lenses; unanimity to admit.** *(User decision.)*

**The panel**

Two identical critics cost double and catch nothing extra, so the lenses differ:

| Lens | Question |
| --- | --- |
| **Meaning** | Do these denote the same *kind* of identifier? Definitions, concept, domain, taxonomy. |
| **Population** | Do they identify the same *set of real-world things*? A bank customer and a cardholder are both "customers" and are not the same population. |

Both `same_namespace` → admit. Any `different_namespace` or `insufficient_evidence` → suppress.
The two disagree → suppress **and** surface as a distinct `critics_disagree` state, because
disagreement marks where human judgement is worth most and is the natural top of the review queue.
Dispatched independently so the opinions stay uncorrelated; one failed call is not unanimity.

**Architecture**

```
identifier columns (read-scoped)
   → [1] deterministic pre-checks  ──reject──▶ dropped, reason recorded, no model call spent
   → [2] ground_bridge()  → GroundingV1(checks, coverage, conflict)
   → [3] blind critic panel → BridgeCriticPanelV1(verdicts, reasons, panel_outcome)
   → [4] fuse_bridge()    → FusionV1(confidence, agreement)      [pure]
        ├─ conflict OR not-unanimous ──▶ suppressed, retained as audit evidence
        └─ otherwise ──▶ propose_bridge() → PROPOSED → active_bridges → planner
                                    → feature + JOIN_IDENTITY_UNCONFIRMED → materialization
```

Stages [2] and [4] reuse the `attest/` contracts (`GroundingV1`, `fuse`) unchanged; stage [3]
follows `ColumnContext`'s blindness rule — the critic is never shown that the system proposed this
pair, nor any confidence, and is asked adversarially for *the strongest reason these are NOT the
same namespace*, with `insufficient_evidence` an expected, unpenalised answer.

`ground_bridge` checks, each `agree | disagree | absent`: `type_family` (carrying `type_basis`),
`entity_link`, `domain_compatible`, `name_tokens` (reuse `grounding._name_tokens`),
`definition_overlap`, `synonym_overlap`, `grain_role` (one side being a table's key corroborates;
**neither** being a key is not a conflict — an FK↔FK link is legitimate), `uniqueness` (`absent`
when unprofiled — the FTR case), `taxonomy_alignment`.

**Work**

- Deterministic pre-checks as a named, testable stage that records *why* a pair was dropped.
- `ground_bridge` returning the `attest` grounding shape so `fuse` is reused, not reimplemented.
- The two-lens critic panel over `drive_structured_call`, closed response vocabulary, schema
  validated, fail-closed on off-vocabulary or failed calls.
- `fuse_bridge`; confidence is **display and review-routing only** and may never feed
  `operational_eligibility` — the influence ceiling is the hard guarantee.
- Widen `bridge_projection` and `active_bridges` to `VERIFIED | PROPOSED` with status carried;
  review all 12 planner consumers. **`status` stays out of every hash** so confirming a link does
  not invalidate existing plans.
- `JOIN_IDENTITY_UNCONFIRMED` added to `REQUIREMENT_CODES` and the versioned
  `validation_requirements` registry, operand naming the link's `fact_key`; carried onto the
  contract and the materialized artifact; cleared on confirm, demoted on reject.
- ~~Persist the taxonomy evidence.~~ **Corrected 2026-07-28: already persisted.** BIAN/FIBO live in
  `field_evidence` (migration 0983), written at `ingest.py:1044` and listed in `_SOURCE_FIELDS`;
  the deployment holds 114 rows each. `attest/grounding.py` already implements a `path_agreement`
  check over `(bian_path, fibo_path, business_term)`, and `attest/runner.py:253` reads them back.
  Anything needing taxonomy corroboration EXTENDS that check. Still genuinely unpersisted:
  `term_type`, `process_path`, `related_terms`.
- Bound the candidate roster per entity/source, order deterministically, page, and carry a
  `pairs_examined` budget, recording honestly when it stopped early. One critic call per surviving
  pair, so unbounded pairing plus a model call each is the failure mode this closes.

**Prerequisites — both hard**

- **The identifier-link governance route.** None exists (`api/routes/governance.py` covers joins,
  table facts and semantic bindings only), so every bridge anywhere came from a fixture or a script.
  Under D4 links now reach materialized numbers, which makes the ability to **reject** a wrong one
  and demote what rests on it the only correction mechanism in the loop. This is task one.
- **Deterministic replay.** The same pair with the same metadata must return the same verdict rather
  than re-rolling the model. Requires a content-addressed key over
  `(candidate_id, both endpoints' metadata fingerprint, prompt/schema/model versions)`, the
  validated response body persisted, and a reuse probe on the dispatch path. Without it,
  re-derivation admits different links on identical inputs and shadow measurement is meaningless.

**Acceptance**

- A `string`↔`string` identifier pair across two sources is admitted and reaches the planner as
  `proposed`; a feature built on it is created, carries `JOIN_IDENTITY_UNCONFIRMED` naming the link,
  and **materializes**.
- Confirming the link clears the requirement on every feature resting on it; rejecting demotes them,
  including already-materialized artifacts.
- A pair the panel splits on is suppressed and surfaced as `critics_disagree`, distinct from an
  ordinary rejection.
- A failed critic call suppresses; it never defaults to admit.
- **A pair with no taxonomy on either side reaches the same admission outcome as the same pair with
  matching taxonomy**, differing only in confidence. This is what stops an optional field quietly
  becoming mandatory.
- Contradictory BIAN level-1 paths, where both sides have them, set `conflict` and suppress.
- Confirming a link does not change any existing plan's fingerprint.
- LLM confidence never changes `operational_eligibility`; a mutation raising eligibility from
  confidence fails a test.
- Re-running derivation on unchanged inputs makes no second provider call and admits an identical
  set.

**Shadow first**

The panel runs in shadow via the existing `attest/` runner and `shadow_store`, suppressing nothing,
until two numbers are published: **agreement with human confirm/reject decisions**, and **rejection
rate**. A critic that never says no is theatre — it adds cost and false assurance. If it approves
everything in shadow, it does not gate. The ~40 provisional gold labels from the P0 work are a
starting point, not a sufficient sample.

---

## Phase E3 — Relational enrichment prerequisite

E3 gets its own spec, implementation plan, and acceptance run. E5 does not hide E3
inside an ontology task.

### E3.0 — Verified-interface reference and design decisions

**Deliverables**

- `docs/architecture/2026-07-27-verified-interfaces-e3-e5.md`
- `docs/superpowers/specs/2026-07-27-e3-relational-enrichment-design.md`

**Work**

- Record exact current contracts for overlay fact identity/write gates, proposal and
  confirmation, dependency invalidation, `graph_node`, `graph_edge`,
  `entity_bridge_edge`, `derive_catalog_realizations`, the semantic-binding immutable
  candidate/current-set/proposal-link stores, projection readiness, read-scope, API
  routing, frontend graph types, multi-schema quarantine behavior, metadata mutation
  transaction boundaries, and durable LLM dispatch/result reuse.
- Verify and pin all contracts above: semantic objects, local realizations, identifier
  candidates, title bindings, identifier bindings/namespaces, namespace bridges,
  entity links, column links, field-level property authority, table-level object-type
  binding, authority axes, cardinality evidence, source scope, server limits,
  freshness/bounds, cursor/revision semantics, and the handoff envelope. Do not force
  them into one nullable table-shaped object.
- Define the schema-preserving physical-object projection and the resolver between its
  identities, public-flattened graph refs, and schema-preserving evidence refs. Unknown schema is
  a typed conflict and never defaults to `public`; same-named attested schemas are distinct
  physical rows even when no unambiguous graph attachment exists.
- Define the v1 single-tenant refusal, the `OntologySourceScopePolicyV1` grant store/evaluator, and
  the independent read/candidate-egress/stale-diagnostic operations.
- Define the metadata-revision bump matrix covering every ontology-affecting write, the signed cursor
  wire format/key rotation/expiry, and exact continuation refusals.
- Freeze finite numeric values for the **new** `OntologyServerLimitsV1` collections only (nodes,
  edges, component members, candidates, bindings per entity/source, source pairs, pairs examined,
  LLM dispatches). **Do not re-freeze the table-neighbourhood numbers** — `MAX_HOPS_DEFAULT`,
  `MAX_NEIGHBOUR_TABLES`, `MAX_COLUMNS_CONSIDERED`, `MAX_HOPS_CEILING` are shipped in `join_path.py`
  and were measured against a hub fixture at merge time. Cite them; do not restate or fork them.
- **Falsification pass — run this BEFORE any other E3.0 work.** For every claim in "What is already
  earned", open the cited file and confirm the capability is not merely present but *reachable*:
  it has a production caller, its gate is not a denylist that omits the new type, and its
  behaviour matches the description. Rev 4's exit gate asked for citations for interfaces it
  already believed existed, which is why three inert foundations survived three reviews. The
  deliverable is a short refutation log — what was checked, what held, what did not.
- Decide the **read-scope column question** (Correction 25): does E3 move read scoping onto
  `effective_restriction` (the governed floor), extend `SENSITIVITY_ROLES` to cover
  `confidential`/`prohibited`, and derive table visibility from column visibility — or does ontology
  visibility explicitly inherit today's raw-`sensitivity` semantics? Either answer is acceptable;
  silence is not, because this plan's text currently promises the governed value.
- Decide the **legacy-bridge disposition** (Correction 20): re-attest, or a named audited exception.
  Decide the **bridge confirmation rule** given that every shipped `owner_of` returns `None`, so
  "endpoint owner" authority does not exist and four-eyes degenerates to two platform admins.
- Decide the **candidate-home question**: `entity_suggestion`, `semantic_binding_candidate`,
  `field_evidence('entity')` and the new family are four homes for one proposal class. Name one home
  per class and the migration for the rest.
- Specify the **`approved_join` cardinality migration** (Correction 17): remove cardinality from
  `fact_key`, admit `unknown` + `basis` in the value schema, remove the propose-time `or "N:1"`,
  and define demote-and-supersede for a correction. Include the backfill treatment of existing
  facts whose cardinality was fabricated.
- Specify the **content-addressed LLM selection key** and result store (Correction 16), or record
  the decision to descope it together with E3.2's replay acceptance criterion.
- Specify the **registry content fingerprint** for the concept and entity registries. It does not
  exist today; every cursor and candidate-revision binding in this plan depends on it.
- Specify the **new ref types** — realization-scoped and tuple-scoped — including
  `_ref_from_payload` decoding, `fact_key` canonicalization, and every poller that decodes payload
  refs. These are new ref types, not new value schemas.
- Specify the **fact-type registration checklist** so a new type cannot fail open: the `enter_fact`
  denylist, a `resolve_authority` branch (its fallthrough raises `TypeError` on a non-
  `CatalogObjectRef`), `Authority.dual`/`task_assignees` per-side planning, the `FactType` Literal,
  and the reverify/expiry/drift paths.
- Define link identity, same-type endpoint roles, direction, inverse direction,
  canonical cardinality, version pinning, ordered composite-column mapping, status,
  backing-reference rules, and demotion.
- Define `unknown`, `not_applicable`, and cardinality-basis behavior. The existing
  planner's missing→N:1 default may not cross the ontology boundary as declared truth.
- Define the closed v1 relation vocabulary and a named consumer/authority owner for
  every member.
- Define the one-fact-per-realization title aggregate, property-valued revisions,
  same-table validation, **propose-time singleton denial and its reject-then-repropose UX**, and the
  database/projection uniqueness backstop. (There is no confirmation CAS in the shipped spine; see
  `TitlePropertyBindingV1`.)
- Define the one-fact-per-physical-table object-type aggregate, its complete-grain dependency,
  its `is_unique` requirement, **propose-time singleton denial**, and demotion.
- Define every identifier binding as an ordered tuple and define
  `identifier_namespace_bridge_v2` over complete ordered pairs. Existing `entity_bridge` is the
  length-one compatibility adapter.
- Define stable candidate identity separately from immutable evidence/policy revisions; define
  candidate-family/scoped currentness and the legacy semantic-binding backfill.
- Define the execution principal/egress policy for persistent candidate generation,
  separately from reviewer visibility.
- Define the durable LLM-selection dispatch idempotency key, result-reuse rule, explicit
  re-evaluation cause, and partial/transient-failure behavior.
- Define cross-source confirmation authority: which endpoint owners/admin roles must
  confirm, how four-eyes applies, and how disagreement is represented.
- Define `OntologyFreshnessPolicyV1`, graph-wide visibility closure, the exact
  `StaleObjectStubV1` allowlist, and privileged stale-diagnostic behavior.
- Define node-closed pagination, signed cursor binding, revision-check order,
  filter/build/bound order, incomplete/required-complete component behavior, and deterministic
  pre-enumeration candidate/graph bounds.
- Define one-snapshot assembly and the projection protocol: checkpoint readiness plus
  folded-current-state/event-id verification before any operational row is served.
- Define the canonical plural dependency set for cross-catalog candidates.
- Resolve migration numbers against main at implementation time. `1031`
  (`1031_graph_node_measure_annotation_links.sql`) is occupied; the next free number is **1032**,
  and main moves — re-check at implementation time rather than reserving now.

**Exit gate**

- **The falsification log is complete and every "already earned" claim either held or was moved to
  "not earned".** This gate comes first; the remaining gates are void until it passes.
- Every named interface has a source citation and a contract test sketch.
- No open question changes persisted identity, physical schema, authority axes,
  object-type singleton identity, tuple-bridge shape, candidate/revision hashing and migration,
  source authorization, metadata-revision bumps, cursor integrity, LLM replay, endpoint-owner
  confirmation, direction, link-version pinning, projection readiness, server bounds, or
  cardinality.
- The design contains adversarial examples for a foreign key bridge, a partial
  composite-key bridge/join, two same-named tables in different schemas, missing
  cardinality, a same-type business link, concurrent title confirmations, a hidden
  bridge intermediary, a lagged projection, legacy-unspecified authority, a hub table,
  cursor tampering/replay after mutation or freshness transition, a source-entitlement denial, a
  non-`None` tenant, conflicting table object types, field-level mixed authority, and repeated LLM
  dispatch.

### E3.Foundation.0 — The ingest schema contract (new in Rev 5; funding gate)

**Purpose**

Give schema a way into the system at all. Rev 4 compressed this into one bullet — "persist the
roster before the current public-flattening collision step" — which is not achievable, because
there is no such step and no schema to persist on the mainstream path.

**The situation, stated exactly**

- `CanonicalRow` has **no schema field** (`canonical.py:44-69`) and `_headers._ALIASES` has **no
  schema alias** (`_headers.py:13-28`). A CSV/Excel/OpenMetadata upload cannot express a schema.
- Schema exists only on the FTR/glossary path, and there the **reader** quarantines same-`(table,
  column)` cross-schema terms fail-closed in its own pass 2 — no `CanonicalRow`, no sidecar
  (`glossary_reader.py:183-205`; `ftr_adapter.py:259`) — precisely because the schema is dropped
  downstream.
- `validate_rows` dedups on `(source, table, column)` with no schema (`canonical.py:208`).
- `_cross_schema_conflicts` (`ingest.py:836-902`) is a **cross-upload** fence against persisted
  nodes; within one file, first schema wins (`ingest.py:870`).

**Work**

- Extend the reader contract end to end: a `schema` alias in `_headers._ALIASES`, a `schema` field
  on `CanonicalRow`, and schema propagation through the CSV, Excel and OpenMetadata readers.
- Make `validate_rows` schema-aware: dedup on `(source, schema, table, column)`, and detect
  **within-upload** same-name cross-schema objects as a typed conflict rather than merging them.
- Relax the glossary reader's pass-2 quarantine from "discard" to "emit as distinct physical rows
  with an ambiguous graph attachment", preserving its fail-closed behaviour for the genuinely
  ambiguous cases.
- Follow through in `build_graph`, the drift snapshot, the brake, and readiness — each of which
  keys on the flattened identity today.
- State the policy for the **legacy NULL-schema corpus** explicitly: those rows keep unknown schema,
  are never inferred, and are non-operational under this plan's own gate.

**Acceptance**

- A CSV declaring `sales.orders.id` and `hr.orders.id` ingests as two physical objects.
- The same two objects in one glossary upload ingest as two physical objects rather than being
  quarantined away.
- A re-upload that changes a table's schema is still held by the existing cross-upload fence.
- An upload with no schema column behaves **byte-identically** to today, proven against a control.
- Legacy NULL-schema rows remain readable and are explicitly non-operational.

**Scope honesty**

Until this phase lands, **E3 operational identity covers FTR/glossary-attested sources only.** If
E3.Foundation.0 is descoped, that limitation is stated in the Outcome rather than discovered at
E3.1, and every "operational realization / identifier binding / namespace bridge" count for a
CSV-ingested catalog is zero by construction.

---

### E3.Foundation — Physical identity, revision/scope, candidate-store, and fact prerequisites

**Purpose**

Create the storage and security primitives E3.1 needs. A pure adapter cannot manufacture
schema-preserving objects, source authorization, stable candidate identity, table object membership,
or composite-key equivalence from the current flattened stores.

**Depends on E3.Foundation.0.** The physical store below has no values to persist without it.

**Work**

- Add a schema-preserving physical metadata projection keyed by
  `(catalog_source, attested_schema, table, optional column)`, populated from the schema-carrying
  `CanonicalRow` delivered by E3.Foundation.0 and written atomically with the existing graph
  compatibility/display projection. A same-name cross-schema collision becomes an ambiguous
  graph-attachment conflict, not a reason to discard the physical rows. Attach `graph_ref` only
  after a one-to-one mapping proof.
- Backfill only rows with attested schema. Record unknown-schema and irrecoverable historical
  collision gaps as typed conflicts; never invent or infer the missing physical rows. **Historical
  collisions from before E3.Foundation.0 are unrecoverable and largely undetectable** — the
  colliding rows were dropped in the reader or deduped in `validate_rows` and left no durable
  record. Report the gap as "unknown", never as "none".
- **Add the `approved_join` cardinality migration** (Correction 17): remove cardinality from
  `fact_key`, admit `unknown` + explicit `basis` in the value schema, remove `graph.py:93`'s
  propose-time `or "N:1"`, stop the projection overwriting a NULL `graph_edge.cardinality` with a
  fabricated value, and make a correction demote-and-supersede rather than fork. Prove that two
  contradictory VERIFIED facts can no longer coexist on one column pair.
- **Add the `semantic_terms` field policy** so synonyms have a resolvable value and authority, or
  remove synonyms from `OntologyPropertyV1`'s v1 envelope set.
- **Add registry content fingerprints** for the concept and entity registries. Prove that editing a
  concept definition changes the fingerprint (today it changes nothing).
- **Add the content-addressed LLM selection key and result store** (Correction 16), or descope it
  with E3.2's replay acceptance criterion in the same decision.
- Add the **per-`catalog_source`** metadata-revision row/function and wire every write in the E3.0
  bump matrix in the same transaction; a cross-source fact bumps both endpoints. Add an audit test
  that fails when any listed mutation changes an ontology response without changing the revision,
  **and a contention test proving that ingesting source A does not block ingesting source B** —
  ingest holds a per-source advisory lock across LLM enrichment, so a single global counter would
  serialize every catalog behind one row for minutes.
- Implement `OntologySourceScopePolicyV1` storage/evaluation, deny-by-default source grants, the
  system candidate-generation principal, audited admin/config-only grant mutation, and v1 refusal
  of non-`None` tenants. A request body or header can never self-grant a source.
- Implement `OntologyServerLimitsV1` startup validation and request clamping **for the new
  collections only**. Table neighbourhoods call the shipped `join_path` bounds — do not reimplement
  hop/table/column limits, ordering, truncation metadata or `limit_reason`. A test asserts the
  ontology surface and the P4 suggestions surface report the **same** limits from the same
  constants, so the two can never drift apart.
- Implement signed, expiring, scope/policy/revision-bound `OntologyCursorV1` encoding and decoding.
  Bind the original freshness-evaluation instant and expire no later than the earliest visibility
  transition. No graph query runs before cursor verification.
- Implement `OntologyPropertyV1` field-envelope assembly and
  `read_ontology_properties(...) -> OntologyPropertyPageV1` **by batching and paging the shipped
  per-field resolver** (`field_authority.resolve_field_authority` over `field_policies._POLICIES`,
  as already assembled by `build_asset_detail`). Do not re-derive authority against tables. A test
  asserts a single property's envelopes are **identical** to what `build_asset_detail` returns for
  the same column, so the two readers cannot diverge. E5.1 and the later E3 handoff reuse this
  service; neither reads property tables independently.
- Migrate the semantic-binding candidate substrate to `CandidateIdentityV1`: stable candidate id,
  immutable revision id, candidate family, generic scope kind/id, family-scoped currentness, and
  revision-to-fact links. **Backfill via the append-only side mapping table, never an UPDATE** — the
  candidate tables are WORM (row triggers + revoked grants), and suspending those triggers is
  prohibited. Legacy rows take `scope_kind=graph_table`, not `physical_table`, because their key is
  a public-flattened graph ref. Prove rebuild/currentness compatibility against a pre-migration
  control.
- Register the table-scoped `object_type_binding` and tuple-scoped
  `identifier_namespace_bridge_v2` fact/ref/value schemas, write gates, dependency extraction,
  singleton denial, and fold behavior — **plus every authority seam from the E3.0 registration
  checklist**: the `enter_fact` denylist (a new owner-known fact type is otherwise single-party
  self-assertable), a `resolve_authority` branch (its fallthrough raises `TypeError` on a tuple
  ref), `Authority.dual`/`task_assignees` per-side planning (today `approved_join`-only), the
  `FactType` Literal, and reverify/expiry/drift. A test asserts an unregistered new fact type fails
  **closed**, not open.
- Register the two **new ref types** (realization-scoped, tuple-scoped): `_ref_from_payload`
  decoding, `fact_key` canonicalization, and every poller that decodes payload refs. Existing
  single-column `entity_bridge` remains supported.

**Acceptance**

- `sales.orders.id` and `hr.orders.id` under one source persist as two physical objects; neither is
  collapsed into `public.orders.id`. An ambiguous flattened attachment is absent/conflicted.
- An unknown-schema legacy row is visible only as permitted conflict context and cannot become an
  operational realization, identifier binding, or bridge endpoint.
- Every ontology-affecting mutation in the bump matrix changes the metadata revision atomically; a
  failed transaction changes neither data nor revision.
- A cursor modified by the client, replayed by another scope, or replayed after a metadata/source
  policy revision—or after its earliest freshness transition—is refused before graph work.
- A caller with `catalog:read` but no source entitlement cannot read, count, facet, diagnose, or
  dispatch metadata from that source. An unauthorized grant mutation is audited and refused. A
  non-`None` tenant is refused.
- A property page preserves mixed field authority, is fresh/source/read-scoped before counts, and
  rejects a stale/tampered cursor under the same protocol as the full handoff.
- Rebuilding the migrated candidate currentness preserves existing semantic-binding outcomes,
  **with the WORM triggers still armed** (the migration is proven not to require an UPDATE).
  Publishing a title-family roster cannot replace currency/entity currentness for the same table.
- **A second competing object-type proposal is denied at propose time** while the first is
  non-terminal, and a rejected fingerprint stays sticky-denied; after an explicit reject, a rival
  entity can be proposed and confirmed. (Rev 4's "two competing confirmations produce one CAS
  winner" is deleted — the spine denies at propose, so that race cannot occur.)
- Changing/demoting the exact grain event makes the winning binding non-operational before a
  realization read can serve it. A grain with `is_unique = false` never produces an operational
  binding.
- A complete two-column bridge is one atomic governed tuple mapping. One member or swapped pairs
  cannot become a composite bridge.
- **The legacy-bridge disposition chosen in E3.0 is implemented and tested as chosen** — either
  legacy bridges are re-attested against real schema, or they are admitted under the named audited
  exception and are visibly marked as such. A legacy bridge is *not* silently adapted as a
  compliant one-element tuple: its `public` schema was fabricated into its immutable identity.
- A cardinality correction **demotes and supersedes**; two contradictory VERIFIED facts cannot
  coexist on one column pair; a re-upload flipping `N:1` to `1:N` raises a divergence.
- An unregistered new fact type fails closed at every authority seam (`enter_fact`,
  `resolve_authority`, dual planning, `FactType`).
- Ingesting source A does not block ingesting source B (per-source revisions).
- The ontology surface and the P4 suggestions surface report identical neighbourhood limits from
  identical constants.
- A single property's field envelopes from `read_ontology_properties` are identical to
  `build_asset_detail`'s for the same column.
- **A hub fixture is not re-run for the shipped bounds** — those were measured at merge time. The
  hub fixture here proves only the **new** bounds: identifiers/source pairs/pairs examined before
  LLM dispatch.

### E3.1 — Pure contracts and adapters over already-governed links

**Purpose**

Produce the E3 read contract from already-governed facts and the E3.Foundation fact types before
adding E3 LLM proposals.

**Work**

- Implement the frozen typed contracts and canonical hasher.
- Read physical objects only from the schema-preserving physical metadata projection. Preserve
  graph/evidence refs as attachment/provenance fields; never use them as physical identity.
- Assemble `OntologyPropertyV1` with independent effective-field authority; never assign one field's
  verification to the whole property.
- Build local object realizations only from the complete ordered governed `grain` fact
  plus an operational table-level `ObjectTypeBindingV1`. Return proposed canonical memberships
  separately as `LocalObjectRealizationCandidateV1`. Do not reuse
  `catalog_realizations.object_grain()`'s first-flat-column result or a column
  `entity_assignment` as proof.
- Adapt the five `ENTITY_RELATIONSHIPS_V1` entries only as `rolls_up_to` semantic link
  types.
- **Gate both derivation helpers on concept authority before adapting either.** This is the
  single easiest way for this program to violate its own rule that "derived verification inherits
  its root". `bridge_candidates._identifier_columns` (`bridge_candidates.py:53-67`) and
  `catalog_realizations.object_grain`/`key_entity` (`catalog_realizations.py:99-117`) derive entity
  identity from `graph_node.concept` **read raw, with no authority check**. Since E1a, `concept` may
  be `llm/proposed`, and the shipped kernel caps it at `InfluenceTier.RECOMMENDATION` with an
  operational rule requiring a source or human signal (`field_policies.py:80-86`, ceiling at `:67`).
  Adapting these helpers as-is would launder LLM-proposed concepts into object grains, key entities
  and identifier bindings. E3.1 resolves concept authority through
  `resolve_field_authority` first and admits only operationally eligible concepts into identity;
  `llm/proposed` concepts may support a **candidate** and nothing more.
- Adapt `derive_catalog_realizations` outputs into entity-link realizations only after
  revalidating physical schema, complete governed grains, key entities, and the raw
  backing join/fact cardinality. Do not trust the helper's normalized cardinality
  field when the raw value was missing. Note that until E3.Foundation's cardinality migration lands,
  the raw missing-ness has already been erased at propose time — so "preserve missing cardinality"
  is only testable *after* that migration, and E3.1's corresponding acceptance criterion is gated on
  it. Preserve the exact link-type version and ordered column pairs.
- Adapt VERIFIED `entity_bridge_edge` rows into
  `IdentifierNamespaceBridgeV1`, never entity-link realizations, only after the backing
  bridge stream still folds to VERIFIED and its confirmed event id matches. Adapt them as
  length-one tuple bridges only. A member of a composite grain remains a reference/non-grain binding;
  it is not promoted to `object_grain`. Adapt VERIFIED `identifier_namespace_bridge_v2` facts as
  complete ordered tuple bridges.
- Reuse the semantic-binding current-set/proposal-link substrate. Adapt currently
  eligible `currency_binding` facts into
  `ColumnSemanticLinkV1(kind=denominated_in)` and eligible `entity_assignment` facts
  into identifier-binding evidence only; return current proposals as advisory candidates, not
  table object facts.
- Normalize explicit `1:1`, `1:N`, `N:1`, `N:N`, unknown, and not-applicable in one
  direction-aware adapter; carry basis plus authority and test inversion explicitly.
- Centralize `AuthorityEnvelopeV1` assembly without discarding root provenance or
  rewriting `legacy_unspecified`.
- Assemble in one repeatable-read snapshot and apply source entitlement, sensitivity scope,
  metadata revision, checkpoint, folded-state, freshness, node-closure, signed cursor, and
  server-bound protocols in the frozen order.

**Acceptance**

- A fixture with `customer`, `account`, and `transaction` returns semantic links and
  their distinct catalog realizations.
- Reversing an endpoint reverses cardinality exactly once.
- A missing cardinality remains `unknown`; the existing planner default is visible only
  as `basis=defaulted` and is not operational.
- A catalog cardinality conflict is returned as a conflict, not replaced by the global
  registry value.
- `account_to_customer` renders as `rolls_up_to`, never `owned_by`.
- A customer foreign-key bridge on a transaction table produces a namespace bridge
  and reference binding; it does not make the transaction table a Customer realization.
- A bridge covering only one column of a composite grain does not connect the object
  realization.
- A complete governed two-column bridge connects the two tuple bindings atomically; pair order is
  preserved under endpoint reversal.
- Two same-named tables in distinct attested schemas remain distinct; a missing schema
  refuses operational merging.
- An existing currency binding appears once, with no duplicate E3 fact.
- A proposed object-type binding appears only as a realization candidate. A proposed or verified
  column entity assignment alone never creates a local object realization.
- A property with a verified concept and proposed LLM definition returns those two field authorities
  unchanged rather than a single “verified property” label.
- **An `llm/proposed` concept never reaches an object grain, key entity or identifier binding.** It
  may support a candidate; it cannot classify a table or bind an identifier. A mutation that removes
  the concept-authority gate must fail this test.
- A `PARTIALLY_CONFIRMED` fact never renders as `verification=verified`; a drift-STALEd fact renders
  as demoted, not as verified-but-stale.
- A VERIFIED projection row whose stream is stale, demoted, or points at another
  confirmed event is excluded. A lagged/degraded checkpoint refuses the operational
  handoff.

### E3.2 — Candidate generation and LLM selection

**Purpose**

Let the LLM label bounded, physically backed candidates without inventing endpoints.

**Work**

- Generate entity-link candidates only from existing joins and catalog realizations.
  A key-equivalence bridge or bridge candidate cannot supply a business predicate.
- Replace `derive_bridge_candidates`' unbounded column-pair enumeration with the
  E3.Foundation bounded tuple-binding roster. Bound and deterministically page bindings per
  entity/source, then source pairs and pairs examined, before producing complete-shape bridge
  candidates. Keep these candidates separate from entity links.
- Use the migrated `CandidateIdentityV1` substrate for object-type, namespace-bridge,
  entity-link, column-link, and title-property families; do not create a parallel candidate ledger.
  V1 column links are same-table unless the E3 design names a governed cross-table backing.
- Generate object-type-binding candidates from a complete eligible grain plus supporting
  column-entity/concept and advisory table evidence. Persist the support and conflicts; no signal
  becomes the table fact without confirmation.
- Generate title-property candidates only from readable columns belonging to the
  candidate's schema-preserving local realization. The LLM may select among supplied
  properties but cannot invent a property or bind a property from another realization.
- Run persistent generation under the approved system execution principal and explicit
  metadata-egress policy. Reviewer roles filter reads/review surfaces; they do not
  redefine the persisted candidate universe.
- Send a bounded, sanitized candidate envelope: opaque candidate id; endpoint role
  slots; governed entity/concept ids; bounded sanitized definitions/labels; backing
  kind and join shape; and the closed link-kind vocabulary. Never send sample values,
  unrestricted metadata, or raw logical refs. The response may contain only the supplied
  candidate id, a closed relation kind, disposition/confidence, and bounded rationale.
- Cardinality comes only from explicit backing evidence. The LLM may flag uncertainty
  but cannot convert unknown/defaulted cardinality into fact.
- Stable candidate identity uses only the candidate kind, schema-preserving canonical
  endpoints/roles, and semantic target. An immutable
  candidate revision hashes that id with every source snapshot, endpoint metadata
  fingerprint, backing fact key/event/status, concept/entity/link registry fingerprint,
  derivation/shortlist/bounds versions, and generation-principal metadata-egress-policy version.
- Apply `OntologyServerLimitsV1` before input enumeration and dispatch; caller bounds may reduce
  them. Sort each bounded input roster before streaming pair generation, stop when the
  `pairs_examined` budget is exhausted, persist the complete/partial verdict, and never present an
  over-bound omitted candidate as rejected.
- Persist/validate the LLM result as `LLMSelectionEvidenceV1` through the **content-addressed
  selection store delivered by E3.Foundation** — not through today's `llm_dispatch`, which persists
  no response and whose key is per-attempt by design. Replay the first valid result for the same
  selection revision; only an explicit re-evaluation cause creates a new selection revision.
  **If that Foundation item was descoped, this bullet and its acceptance criterion are descoped with
  it** — E3.2 then dispatches per attempt and says so, rather than claiming a replay it cannot
  perform.
- Persist immutable proposal evidence with the dependency set, prompt/model/schema/
  config versions, candidate inputs, rationale, and disposition.
- Reconcile dropped, withheld, transient-failed, and stale candidates using the E1
  lifecycle pattern.

**Acceptance**

- An invented endpoint or relation kind is rejected before persistence.
- A namespace-bridge candidate cannot be relabelled as an entity relationship and
  cannot enter a resolution component before governed confirmation.
- An invented or cross-realization title property is rejected before persistence.
- The execution principal's egress policy is enforced before dispatch. A reviewer
  outside an endpoint's read scope cannot read or count the stored candidate, but their
  role does not mutate the system candidate set.
- The same physical inputs produce the same candidate set regardless of which reviewer
  later opens it.
- Generating or projecting one candidate family never replaces another family's current roster for
  the same physical table/source pair.
- Changing either catalog snapshot, endpoint metadata, backing fact status, registry,
  shortlist bounds, or generation egress policy creates a new revision/recomputes
  currentness while preserving the stable candidate id. Candidates no longer allowed
  are retired without erasing their audit history.
- An unknown-cardinality candidate remains advisory after LLM selection.
- Re-enrichment retires a deliberately dropped proposal but preserves a proposal
  after a transient provider failure.
- A proposed link is returned as `ai_proposed` and is **tier-gated** per the roadmap's link policy
  (`2026-07-28-product-roadmap-and-contract-ownership.md` §4): usable for discovery and feature
  suggestion, usable in a marked sandbox analysis, and **never** in production materialization
  without a VERIFIED link or policy-based automatic attestation. The earlier blanket "never
  planner-operational" contradicted Phase E0b in this same document.
- A same-type candidate such as `employee(manager) → employee(report)` survives
  validation; a same-entity identifier equivalence cannot be relabelled as that link.
- Over-bound enumeration and partial LLM completion are explicit and deterministic.
- **(Gated on the E3.Foundation selection store.)** Running the same selection revision twice makes
  one provider dispatch and reuses one persisted validated result. A transient retry uses the same
  idempotency key; an explicit re-evaluation has a new revision and retains both audit records.
- LLM `confidence` never changes `operational_eligibility` — a mutation raising eligibility from
  confidence must fail a test.

### E3.3 — Governed facts, projection, title binding, and demotion

**Purpose**

Make confirmed links and title bindings durable and safely reversible.

**Work**

- Add the smallest new overlay fact/ref types required for semantic links and object
  title bindings. Reuse generic propose/confirm/reject/expire/stale event machinery.
- Validate endpoint existence, entity vocabulary, direction, same-object title
  binding, backing fact identity, and confirmer authority at every write entry point.
- Key the governed title fact by the local schema-preserving realization only; keep the
  selected local property and exact realization revision/object-binding/grain events in the
  proposed/confirmed value. Candidate revisions may be property-specific, but only one proposal per
  realization can be live: the shared `fact_key` makes `propose_fact` deny a rival while the first
  is non-terminal. A revision dependency change demotes the title.
- Apply the E3.0 cross-source confirmation rule to any fact whose endpoints span
  owners/sources. A platform admin shortcut, if allowed, is explicit and audited.
- Project only operationally eligible facts into a rebuildable, event-seq-stamped E3
  projection registered with the projection protocol and bump the metadata revision in the same
  projection-changing transaction.
- Extend dependency extraction and drift handling so changing an endpoint or losing
  its applicable backing authority—a join for an entity link or a bridge for
  identifier equivalence—demotes the projection.
- Keep proposal evidence visible after rejection/demotion for audit, while removing it
  from the operational projection.
- Make the read path authoritative even if cleanup fails: require projection readiness
  and verify each projected row against the folded current fact status and exact
  confirmed event id before returning it.

**Acceptance**

- Proposal → distinct authorized confirmation → projection works end to end.
- Same actor cannot self-confirm where four-eyes applies.
- A cross-source link cannot become VERIFIED with only an unauthorized endpoint owner.
- Reject/expire/stale or backing-authority loss must make the fact fail closed before
  any operational read can serve it. Synchronous projection is preferred for prompt
  visibility, but checkpoint/fold verification is the correctness backstop: if cleanup
  fails, the last row is still non-operational.
- A composite grain remains an ordered composite key.
- A complete composite namespace bridge remains one ordered tuple mapping across projection rebuild;
  a member loss demotes the whole bridge.
- Missing local title stays missing; different catalog realizations of Customer may
  bind different title properties; an AI title is visibly proposed and a confirmed
  title is visibly human-verified.
- **A second title proposal for the same realization is denied at propose time** while the first is
  non-terminal; after an explicit reject, a different property can be proposed and confirmed;
  rebuilding the projection preserves the winner. (Rev 4's concurrent-confirmation CAS test is
  deleted — `propose_fact` denies before confirmation, so the race cannot occur.)
- A bridge can be proposed and confirmed **through the product**, by the confirmation rule E3.0
  chose, and the four-eyes claim is tested against what the shipped authority resolver actually
  does — `owner_of` returns `None` everywhere, so "endpoint owner" is not an available authority and
  the test asserts the real rule, not an aspirational one.

### E3.4 — Review/API surface and E5 handoff

**Work**

- Add source/read-scoped endpoints to list object/link/title candidates and inspect evidence.
- Extend the established governance command surface to confirm/correct/reject them.
- **Ship the identifier-namespace bridge governance route.** There is none today — `api/routes/
  governance.py` covers joins, table facts and semantic bindings only, so every VERIFIED bridge in
  existence came from a fixture or a script, while E5.3/E5.6/E5.7/E5.8 are all built on VERIFIED
  bridges. The route implements the confirmation rule E3.0 chose (noting that `owner_of` returns
  `None` everywhere, so endpoint-owner authority is unavailable and four-eyes degenerates to two
  platform admins), and it covers both the legacy `entity_bridge` and
  `identifier_namespace_bridge_v2`. Without this route, every downstream E5 slice is fixture-only —
  which the build order now states rather than discovering at E5.8.
- Publish `read_e3_ontology_inputs(...) -> E3OntologyInputsV1` with the exact signature
  above. E5 depends only on this service, not on E3 tables.
- Implement the exact read order: authenticate cursor/tenant → resolve source entitlement → clamp
  caller bounds to server limits → start repeatable-read page → capture metadata revision and
  checkpoint/fingerprints → freshness → sensitivity scope over nodes and backing evidence → folded
  fact verification → node-closed bounds → totals/facets/truncation. Hidden children cannot be
  inferred through parents, component topology, cursors, or counts.
- Emit signed cursors bound to identity scope, normalized entitled sources, source/freshness/limit
  policies, registry fingerprints, and metadata revision. Reject tampering or replay under a
  different identity, policy, registry, or revision before graph work.
- Provide a fixture corpus spanning at least two catalogs, one disconnected entity
  namespace, a composite key, conflicting realization cardinality, a restricted
  endpoint, same-named tables in two schemas, legacy-unspecified authority, and one
  stale source. Include a hidden bridge intermediary and a projection row whose backing
  fact has already demoted.

**E3 exit gate**

- The handoff service returns the immutable entity-registry snapshot, local
  physical objects/properties with per-field authority, object-type bindings and advisory
  candidates, realizations and advisory candidates, local title-property bindings and their
  advisory candidates, tuple identifier bindings, verified complete-tuple namespace bridges,
  advisory namespace-bridge candidates, typed entity links, realization cardinality
  basis+authority, column links, conflicts, fingerprints, metadata-revision/checkpoint stamps,
  source scope, limits, freshness, and honest authority axes. It does not pre-assemble E5
  `SemanticObjectTypeV1` views or namespace components.
- Proposed and verified links are distinguishable by exact tests.
- No proposed link clears an existing join/feature authority gate.
- A fully hidden object is absent, including from counts/facets. A visible object with
  a hidden key appears only under an explicitly tested policy.
- Normal reads contain no stale protected metadata. A privileged stale-diagnostic request is
  authorized, audited, and refused with a typed reason in v1 (`diagnostic_stubs` empty); an
  unauthorized one is refused before source enumeration.
- A cursor replay under another role, source policy, freshness/limit policy, registry fingerprint,
  metadata revision, or crossed freshness transition is rejected; a modified/expired cursor is
  also rejected.
- Every returned edge has both endpoints in the same page. A truncated namespace is
  explicitly incomplete and makes no authoritative connectedness claim.
- Mutation controls include a must-die semantic mutant and a must-survive no-op.

---

## Phase C — Feature-generation consumption

**Purpose**

Connect E3's outputs to the thing this tool exists to do. Without this phase the programme builds an
ontology and hands it to a viewer: E3 terminates at `read_e3_ontology_inputs`, its only specified
consumer is E5, and E5 builds screens. **No E3 contract would reach feature generation at all.**

**There are exactly two doors into feature generation. Verified, not assumed.**

1. **Grounding inputs.** `templates._load_columns` (`templates.py:258-260`) loads precisely twelve
   fields per column — `catalog_source, object_ref, table_name, column_name, data_type, is_grain,
   is_as_of, concept, entity, additivity, sensitivity, currency` — plus governed joins via
   `join_path` and confirmed identity links via `active_bridges`. That is the entire surface a
   recipe can bind against.
2. **Gauntlet requirements.** `REQUIREMENT_CODES` (`feature_assist.py:110-114`), a closed
   vocabulary, riding on a `NEEDS_EXTERNAL_VALIDATION` idea and naming the operand it concerns.

Anything that does not arrive through one of these two doors does not affect a single generated
feature, however well modelled it is.

**The mapping — every E3 output, and the door it uses**

| E3 output | Door | What actually changes |
| --- | --- | --- |
| `ObjectTypeBindingV1` | grounding input | An entity need binds to a **governed** table entity instead of inferring one. Today `_load_columns` selects `graph_node.entity`, which is empty for all 126 columns in the live catalog, so entity resolution falls back to the concept registry's `entity_link`. |
| `IdentifierBindingV1` (tuple) | grounding + `join_path` | Composite-key joins become expressible. Currently impossible at any key width above one. |
| `IdentifierNamespaceBridgeV1` (tuple) | `active_bridges` | Cross-catalog features over composite keys. The single-column path is already live (E0b). |
| `RealizationCardinalityV1` | gauntlet requirement | A new `CARDINALITY_UNVERIFIED` requirement: a recipe aggregating across a relationship whose cardinality is `unknown` or `defaulted` is **created and flagged**, not silently trusted. Today the value is fabricated at propose time and the recipe cannot tell. |
| `ColumnSemanticLinkV1(denominated_in)` | gauntlet | Strengthens the existing `CURRENCY_CONSISTENT` check — a governed currency link is better evidence than a flat column, for a check that already exists. |
| `OntologyPropertyV1` per-field authority | gauntlet requirement | `CONCEPT_UNCONFIRMED`: a feature whose binding rests on an `llm/proposed` concept is created and flagged, in the E4a shape. Today the gauntlet cannot see authority at all. |
| `EntityLinkTypeV1` / `EntityLinkRealizationV1` | **needs template work first** | Typed relationships only pay off when a recipe can *declare* it wants one. That is a template-library change on top of the contract, and it must be budgeted as such rather than assumed free. |
| `TitlePropertyBindingV1` | **none** | Display only. |
| `SemanticObjectTypeV1`, E5 view contracts | **none** | Human comprehension. Legitimate, but not feature generation. |

**Work**

- Widen `_load_columns` to carry the governed table entity, keeping the read-scope hard filter and
  proving the catalog-wide path byte-identical when no governed binding exists.
- Thread tuple identifier bindings through `join_path` and `active_bridges`.
- Add `CARDINALITY_UNVERIFIED` and `CONCEPT_UNCONFIRMED` to `REQUIREMENT_CODES` and the versioned
  `validation_requirements` registry, each naming its operand, each clearing on the corresponding
  confirmation — the E4a loop, which is the proven pattern here.
- Route the governed currency link into the existing `CURRENCY_CONSISTENT` evaluation rather than
  adding a parallel check.
- For each item: measure `DESIGN_CHECKED` feature count before and after.

**Acceptance — feature counts, not structure**

This phase is judged the way E4a was judged, because that is the only measure that means anything
here. E4a moved `DESIGN_CHECKED` from **0 → 5 of 10** by narrowing the unit check structurally, then
operand roles took it **5 → 10 of 10**. Each item below states its own before/after:

- A governed table entity binds at least one recipe that previously failed to ground.
- A composite-key bridge yields at least one cross-catalog feature that is not expressible today.
- A recipe aggregating across an `unknown`-cardinality relationship is **created** and carries
  `CARDINALITY_UNVERIFIED`; confirming the cardinality clears it.
- A feature bound through an `llm/proposed` concept is **created** and carries
  `CONCEPT_UNCONFIRMED`; a human confirming the concept clears it.
- The catalog-wide (`table=None`) grounding path is pinned byte-identical wherever no governed E3
  input exists, so the phase can never regress today's behaviour.
- **Every item that does not move a feature count is reported as not moving one.** A consumption
  item that grounds nothing new is a finding, not a failure to hide.

---

## Phase E5 — Cross-catalog ontology

### E5.0 — Revise and freeze the E5 design

Update the E5 design with the twenty-five corrections above and the Rev-5 E3
contracts.
Freeze:

- semantic object identity;
- schema-preserving physical-object identity, stable local-realization identity, revision identity,
  and graph/evidence ref separation;
- identifier binding/namespace identity;
- namespace-resolution-component identity;
- exact-vs-descendant concept query behavior;
- concept-hierarchy DAG validation and traversal bounds;
- fresh-only default and privileged stale-stub behavior;
- authority-envelope mapping;
- graph-wide visibility-closure behavior;
- source entitlement, metadata revision, signed cursor behavior, and API/server bounds that affect
  response meaning.

**Recommended identities**

- Stable object route/id: `entity:<entity_id>`.
- Physical table: `(catalog_source, attested_schema, table)`.
- Stable local realization: `(physical_table_id, canonical entity_id)`.
- Local-realization revision: stable local id plus exact complete-grain identity/event and other
  revision evidence. Public-flattened refs never substitute for physical identity.
- Visible namespace component: a derived hash of canonical visible
  `IdentifierBindingV1` member ids after freshness/read-scope closure, plus the
  component-algorithm version. Hash vertices, not bridge edges, so adding a redundant
  A↔C edge to an existing A↔B↔C component does not re-key it. The id is scoped to the
  returned visible member set; the response carries the metadata revision and visibility
  fingerprint separately. It changes only when visible membership or the algorithm
  version changes and is never the public object-type or local-realization id. An
  incomplete component has no authoritative component id.

### E5.1 — Concept hierarchy and semantic-map query

**Purpose**

Replace E0's facet with the governed, authority-bearing version. **E0 already shipped the cheap
version of this** on the existing search path; E5.1 is what makes it honest about per-field
authority, schema-preserving identity, source entitlement and pagination. Its success is measured as
a **delta against E0's published baseline**, not against zero.

**Work**

- Expose registry groups, concepts, `is_a` edges, descriptions, behavior fields,
  registry version, and content fingerprint. (The content fingerprint is created in
  E3.Foundation; it does not exist today.)
- Registry cycle/self-parent validation already landed in E0 — E5.1 consumes it rather than
  repeating it, and E0's descendant traversal is enabled here once the governed reader exists.
- **Migrate E0's facet to this service and retire the direct-search path** so there is exactly one
  cross-catalog concept query, not two with different authority semantics.
- Add an exact concept query across all catalogs, optionally including descendants.
- Consume only `read_ontology_properties(...) -> OntologyPropertyPageV1`; do not reconstruct
  physical identity, field authority, source scope, freshness, counts, or cursors from Foundation
  tables.
- Return `OntologyPropertyV1`: schema-preserving identity, object/table context, effective metadata,
  and independent per-field authority, provenance, and freshness.
- Resolve source entitlement before source enumeration. Apply server bounds before hierarchy or
  property expansion and sign any continuation cursor.
- Require the explicit `OntologyFreshnessPolicyV1`; do not inherit `/search`'s
  convenience default.
- Apply freshness and graph-wide read-scope closure before hits, object context,
  totals, facets, or truncation counts.
- Normal reads are fresh-only. A separately authorized diagnostic request may return
  `StaleObjectStubV1` records but no stale properties or protected metadata.

**Acceptance**

- One exact concept query returns matching readable columns from two catalogs.
- `include_descendants=false` never widens silently.
- `include_descendants=true` follows only validated `is_a` edges.
- A self-parent or two-node cycle fails registry validation; a deep acyclic chain is
  bounded and deterministically ordered.
- A stale property is absent from normal reads. A privileged diagnostic request is authorized,
  audited, and refused with a typed reason (v1); an unauthorized one is refused before source
  enumeration.
- A restricted property is absent from hits, counts, and facets.
- A property whose concept and definition have different authority preserves both field envelopes.
- A source outside the caller's entitlement is absent even when named directly.
- A registry content change alters the fingerprint.

### E5.2 — Object-type assembly

**Purpose**

Show the business object model without yet claiming physical interoperability.

**Work**

- Consume only `E3OntologyInputsV1`; E5 does not reconstruct identities, authority, or
  bridges from E3 tables.
- Group operational catalog-local realizations under stable canonical semantic entity
  ids only after complete governed-grain and table-level object-type-binding validation. Free-text/
  legacy entity names never create stable object ids.
- Assemble each realization's governed ordered key, zero-or-one operational local
  title, advisory local title candidates, readable properties, source, schema/table
  identity, authority, and freshness.
- Show `LocalObjectRealizationCandidateV1` records in a separate advisory collection;
  never insert them into the operational realization-id set or namespace components.
- Surface conflicts: missing grain, multiple competing grains, entity disagreement,
  invalid local title binding, unknown/colliding schema, partial composite key, and
  unreadable required key.
- Apply graph-wide visibility closure after child filtering. Do not retain an empty
  object merely because hidden children proved it exists.

**Acceptance**

- `customer` has one stable object-type id and multiple catalog realizations.
- Two same-entity catalogs with no VERIFIED bridge are grouped semantically but shown
  as not operationally connected.
- Composite keys preserve order.
- No property from another entity/table is attached by name similarity.
- Different Customer realizations may have different local title bindings. A missing
  title renders as missing, not as an invented fallback.
- `Customer`, `customer`, and an unknown free-text tag do not silently normalize into
  one object; only canonical eligible vocabulary ids enter the operational group.
- A canonical proposed membership is visible only in the advisory candidate collection.
- A column entity assignment, including one on a foreign key, cannot create a table realization
  without the table-level object-type binding.
- A fully hidden realization/object and all derived counts are absent.

### E5.3 — VERIFIED resolution components and conflicts

**Purpose**

Describe which identifier namespaces are sanctioned as interoperable and which local
object grains attach completely to those namespaces.

**Work**

- Build connected components from `IdentifierNamespaceBridgeV1` supplied by the E3
  handoff after freshness and read-scope closure. Never compute a global component and
  filter its members afterward.
- Attach a local object realization to a component only when the component covers its
  complete ordered governed grain with compatible entity/key shape/type **and that grain is
  `is_unique = true`** (Correction 18). A VERIFIED grain with `is_unique = false` is not a key —
  the profiler deliberately proposes `False` for sampled uniqueness in `[0.99, 1.0)` so a human
  adjudicates — and bridging on a non-unique grain is exactly the mechanism that merges two
  different real-world entities, which Correction 1 places out of scope.
- **Require identity evidence on every bridge in the component** (Correction 19). Transitive union
  means one wrong bridge merges a whole component, so a bridge whose only evidence is "same concept
  group, same `entity_link`, same coarse type family" is not sufficient basis for a component edge.
- Treat each component vertex as a complete ordered `IdentifierBindingV1` tuple. Never union
  independently bridged members into an implied composite binding.
- Keep foreign-key/reference bindings in the component as references. Their owning
  tables retain their own object grain.
- Keep semantic grouping and operational connectedness as separate axes.
- Surface, never auto-merge, entity-id disagreement, partial composite coverage,
  incompatible key shape/type, and component conflicts. A non-grain bridge is valid
  namespace/reference evidence; it simply does not attach its owning table as that
  object.
- Recompute deterministically after bridge confirmation/demotion and visibility closure, while
  enforcing the server's finite examined/member limits, then apply response bounds. Hash canonical
  visible binding members, not bridge edges. If bounds truncate a component, return it as incomplete
  without an authoritative id; if the caller required completeness and the component exceeds the
  server limit, refuse with `COMPONENT_EXCEEDS_SERVER_BOUND`.

**Acceptance**

- A namespace chain A↔B and B↔C yields one component independent of row/input order.
- Adding redundant A↔C does not change the component id.
- Demoting B↔C splits the component without changing the public
  `entity:<entity_id>` route.
- Two disconnected `customer` components remain visible under the same object type.
- A bridged `transactions.customer_id` remains a Customer reference on a Transaction
  realization; it never turns the table into a Customer realization.
- One bridged member of a two-column grain does not attach the object to the component.
- One complete ordered two-column tuple bridge attaches it; swapped or independently confirmed
  member pairs do not.
- A conflicting bridge is quarantined from the component and returned as a conflict.
- For visible A↔hidden H↔visible B, filtering H and its bridges happens before
  union-find; A and B are not shown as connected and no hidden-member count leaks.
- A page that cannot include a complete component marks it incomplete and publishes no
  component id.
- Tests never claim row-level customer identity.

### E5.4 — Relationship assembly and headless ontology read model

**Purpose**

Combine objects, properties, E3 links, realizations, and concept classes into the
single source-agnostic contract the UI consumes.

**Work**

- Define `OntologyGraphV1` with typed object, local realization, property, concept,
  identifier binding/verified namespace bridge, advisory bridge-candidate, link-type,
  and link-realization nodes/edges.
- Join E3 link types to E5 object ids while retaining every physical realization.
- Compute a cardinality summary only when all visible operational realizations carry
  explicit cardinality and agree. Otherwise return `unknown` or `varies` plus each
  realization's value, basis, and authority envelope.
- Keep proposed links in an advisory collection/edge state; never merge their
  authority with verified realizations.
- Render proposed namespace bridges as advisory candidate edges only. Never include
  them in VERIFIED namespace components.
- Render derivation, verification, and eligibility separately. Do not collapse them
  into a single trust color/value.
- Add bounded object detail and neighborhood endpoints. Do not reuse
  `lineage_graph`'s source anchor or freshness behavior implicitly; reuse only its
  tested traversal/read-scope patterns. Automatic neighborhoods use exactly one hop and the
  server's table/node/edge caps. Explicit deeper expansion remains capped. Apply node-closed
  pagination and refuse cursors whose signature/scope/policy/revision binding no longer matches.

**Acceptance**

- Object detail returns properties, keys/title, components, link types, physical
  backing, authority, conflicts, and freshness in one coherent response.
- A proposed `owned_by` and a VERIFIED `owned_by` remain distinct records with distinct
  authority envelopes, not one truthy edge.
- A proposed namespace bridge is visible as advisory evidence but does not connect
  components.
- Mixed realization cardinality returns `varies`.
- Any unknown/defaulted realization prevents a falsely concrete cardinality summary.
- A same-type `employee(manager) → employee(report)` link retains its two roles and is
  not rendered as identifier equivalence.
- A link realization pins its link-type version and exact ordered composite pairs.
- Removing read permission for one endpoint removes the edge and backing details.
- Removing visibility from every child removes the empty object and its counts.
- A hub object returns deterministic one-hop/count truncation metadata; it never walks the catalog
  transitively by default.
- Contract snapshots and backend/frontend types agree.

### E5.5 — View A: semantic map

- Add an Ontology route and source-agnostic concept/object search.
- Show exact/descendant mode, source, physical local object, per-field derivation/verification/
  eligibility/freshness, and any conservative property summary as a labelled summary.
- Show source-scope/limit policy version and honest hit/facet truncation; never label an unknown
  available count as a complete total.
- Deep-link a property to the existing asset-detail screen.
- Prove the worked query: “all readable columns that mean `balance` across catalogs.”

### E5.6 — View B: entity-relationship model

- Render object types, local keys/titles, namespace connectedness, link labels, and
  realization-specific cardinality basis+authority.
- Allow filtering by source, authority, and operational eligibility.
- Selecting an entity link shows its backing joins and conflicts; selecting an
  identifier-namespace bridge shows its bridge fact and conflicts.
- Do not render `varies` as a single 1:N/N:1 label.

### E5.7 — View C: navigable ontology graph

- Reuse committed ReactFlow/LineageView interaction patterns.
- Navigate object → property/concept and object → typed link → object.
- Render derivation, verification, and operational eligibility as separate,
  accessible visual signals.
- Load one hop automatically. Preserve server graph/count bounds, show truncation explicitly, and
  require an intentional action for capped deeper expansion.
- The local hard-coded asset prototype may inform styling only after it is reviewed;
  production data comes exclusively from `OntologyGraphV1`.

### E5.8 — End-to-end acceptance

Run one fixture/demo containing:

- at least two catalog sources modelling `customer`;
- one VERIFIED namespace bridge and one advisory namespace-bridge candidate;
- one disconnected key namespace;
- one Customer foreign key on a Transaction table;
- customer/account/transaction relationships;
- one explicit and one missing/defaulted realization cardinality;
- one realization-cardinality conflict;
- one composite grain;
- one partially bridged composite grain;
- one fully bridged two-column grain through one atomic ordered tuple bridge;
- one composite approved join with ordered column pairs plus one partial/swapped-pair
  refusal;
- two same-named tables in different schemas plus one unknown-schema table;
- one missing local title and two confirmed local titles on different realizations;
- two competing title proposals on one realization used to prove propose-time singleton denial,
  plus the reject-then-repropose path that follows it;
- one proposed object-realization membership;
- one same-type business link with distinct endpoint roles;
- one governed currency binding surfaced as `ColumnSemanticLinkV1(kind=denominated_in)` — the only
  v1 relation kind, since `converted_by` and `as_of` are deferred for want of a consumer;
- one restricted object whose every child is hidden;
- one visible A↔hidden H↔visible B bridge chain and one redundant A↔C bridge;
- one stale source;
- one stale projected edge whose authoritative fact has demoted;
- one legacy-unspecified authority record;
- one property with a verified concept, proposed definition, inherited domain, and ungoverned type;
- one source denied by source entitlement despite `catalog:read`, plus one non-`None` tenant request;
- one hub object whose one-hop neighborhood exceeds both table and node limits;
- one bound-truncated component, one cursor bit-flip, one cursor replay under a changed scope, and
  one replay after a metadata revision plus one after its earliest freshness transition;
- existing semantic-binding and new title candidate families on the same physical table;
- one repeated successful LLM selection revision plus an explicit re-evaluation revision;
- an `is_a` cycle fixture used to prove validation refusal.

The program passes when:

1. a concept query spans both catalogs without leaking the restricted column;
2. one `customer` object type shows all readable realizations and separate resolution
   components honestly;
3. foreign-key bridges remain references and partial composite bridges do not attach
   whole object realizations, while the atomic complete tuple bridge does;
4. schema-colliding physical objects stay distinct and unknown schema does not merge;
5. the ER view shows typed links and cardinality
   value+basis+authority/conflicts without turning missing cardinality into N:1;
6. the graph traverses across a VERIFIED identifier-namespace component;
7. every node/edge shows separate derivation, verification, eligibility, and freshness;
   every property field retains its own envelope;
8. a **demoted** link never appears operational, and a **proposed** link appears only at the tiers
   the roadmap's link policy permits — never in production materialization;
9. stale or fully hidden metadata is absent from normal results and all counts;
10. hidden intermediaries do not connect visible components, redundant edges do not
    re-key a component, and incomplete components make no authoritative connectivity
    claim;
11. a demoted fact cannot remain operational through a stale projection;
12. one realization can never publish two operational titles;
13. no test or UI claims that metadata resolution matched real customer rows;
14. a source entitlement denial and v1 tenant refusal happen before source enumeration;
15. automatic neighborhood loading is one hop and count-bounded with explicit truncation;
16. candidate-family currentness is independent, and a repeated LLM selection reuses its durable
    result; and
17. cursor tampering or replay after scope/policy/registry/metadata-revision/freshness transition is
    refused before graph work.

### Measured criteria

The correctness conditions above are assertions about a synthetic fixture. **A programme can satisfy
all of them and deliver nothing observable.** These are the criteria that say whether it worked.

**The measured values live in the verified-interfaces reference** (§8), because they are
observations about the current catalog and go stale as the catalog changes. The plan owns the
targets; the reference owns the numbers.

| # | Measure | Target |
| --- | --- | --- |
| M1 | Columns carrying a concept | states the ceiling on every semantic-map claim; published at E0 |
| M2 | Tables with a complete governed grain **and** `is_unique = true` | if near zero, E5.2 has nothing to assemble and the programme stops |
| M3 | VERIFIED links creatable **through the product** | ≥ 1 through the UI before any phase consuming links is called done |
| M3a | Identifier columns eligible for candidacy | parity with the identifier-concept count; a drop to zero means a reader is consulting the wrong metadata column again |
| M4 | Sources with attested schema | states which sources E3 operational identity actually covers |
| M5 | Columns whose governed floor is stricter than the tag read-scope consults | drives the Correction-25 decision with a number rather than an opinion |
| M6 | Distinct `(concept, source)` pairs reachable in one query | > 0 after E0 — the first honest proof the idea has value |
| M7 | Catalog sources loaded with a concept-bearing catalog | **≥ 2.** The binding constraint on the whole programme |
| M8 | Panel rejection rate in shadow (E0b) | **> 0.** A critic that never rejects is theatre and does not gate |
| M9 | Panel agreement with human confirm/reject decisions (E0b) | published before the panel gates anything |
| **M10** | **`DESIGN_CHECKED` features attributable to an E3 output** (Phase C) | **> 0 per consumption item, or the item is reported as moving nothing.** This is the programme's headline number: it is the only measure that says the ontology changed what gets generated |
| **M11** | **Cross-catalog features expressible** (E0b, then Phase C for composite keys) | **> 0.** Currently zero — one catalog, zero confirmed links |

**M10 is the measure the programme lives or dies by.** E4a is the precedent: it moved
`DESIGN_CHECKED` from 0 → 5 of 10, then operand roles took it to 10 of 10, and both were judged on
that number rather than on having shipped a contract. Any E3 contract whose Phase-C item reports
M10 = 0 has not earned its place, however complete its modelling.

**Stop rule.** If M2, M4 or **M7** is below target at E3.0 and the corresponding prerequisite is not
funded, the programme does not proceed to E3.1 on the assumption it will improve later. It amends
the Outcome and says which of the six promises it can still keep. **M7 is the one that binds today**
— the catalog holds one source and one table, so every cross-catalog promise is currently
undemonstrable regardless of how much of E3 gets built.

---

## Build order and funding gates

**The ordering rule: something a user can contradict ships first, and every later phase is measured
as a delta against it.** Rev 4 shipped nothing user-visible until step 6 of 9 and could have been
fully green while delivering nothing observable.

0. **E0 — semantic map v0.** A concept facet on the shipped cross-catalog search, plus registry
   cycle validation. Days, not phases. Publishes the concept-coverage baseline.
0b. **E0b — identifier-link admission.** The governance route first, then deterministic grounding,
   the two-lens critic panel, proposed links reaching the planner, and the
   `JOIN_IDENTITY_UNCONFIRMED` provenance loop. Runs on the shipped single-column link and the
   shipped `attest/` harness, so it does not wait for Foundation. **This is where cross-catalog
   features actually come from** — the planner already consumes links, so admission plus a
   confirm/reject surface is the whole path.
1. **E3.0** — approve the contracts, exact limits, migration/backfill, scope, cursor, and vocabulary.
   **Begins with the falsification pass**, which gates everything else in the phase.
2. **E3.Foundation.0 — the ingest schema contract.** Readers, `CanonicalRow`, `validate_rows`.
   Without it, every operational count for a CSV-ingested catalog is zero by construction, and the
   rest of Foundation has nothing to persist.
3. **E3.Foundation** — physical identity, per-source metadata revision, source scope, signed
   cursors, new server-limit collections, candidate-store migration (WORM-safe), object-type
   binding, tuple-bridge prerequisites, the `approved_join` cardinality migration, the
   `semantic_terms` policy, registry content fingerprints, and the LLM selection store.
4. Start two bounded tracks after the Foundation gate:

   - **E3.1** — adapt already-earned governed relationships.
   - **E5.1** — the governed semantic-map service, superseding E0's facet.
5. **E3.2–E3.4** — proposals, governance, title binding, **the bridge governance route**, stable E5
   handoff.
5b. **Phase C — feature-generation consumption.** Every E3 output that is going to affect a feature
   arrives through a grounding input or a gauntlet requirement, each with a measured before/after
   feature count. **This runs before E5, not after.** If it runs after, the programme spends its
   remaining budget on screens without ever having proved the ontology changes what gets generated —
   and Phase C is the only phase whose success is measured in features.
6. **E5.2–E5.4** — object types, verified components, unified read model.
7. **E5.5** — semantic-map UI.
8. **E5.6** — ER UI.
9. **E5.7** — visual explorer.
10. **E5.8** — cross-catalog acceptance.

**Descope-honestly rule.** Several phases have a named prerequisite that may not be funded
(E3.Foundation.0, the cardinality migration, the LLM selection store, the bridge route). If a
prerequisite is descoped, the dependent acceptance criteria are descoped **in the same decision**
and the Outcome is amended to say what the program no longer delivers. This plan does not retain an
acceptance test whose mechanism is unfunded — that is how Rev 4 accumulated three inert foundations.

Stop/go gates:

- **Do not start E3.0's contract work until its falsification pass is complete.** Three adversarial
  reviews passed over Rev 4's earned/not-earned ledger without catching that its central Foundation
  premise, its LLM replay mechanism, and its CAS singleton were all absent from the code, because
  the exit gate asked for citations for interfaces the plan already believed existed.
- Do not start E3.Foundation until E3.Foundation.0 lands, or until the plan is amended to state
  that E3 operational identity covers FTR/glossary-attested sources only.

- Do not start E3.Foundation until E3.0 accepts the physical-store migration/backfill,
  table-object singleton, tuple-bridge schema, candidate-store migration, v1 single-tenant/source
  scope, metadata-revision bump matrix, signed cursor protocol, exact server limits, field-authority
  contract, link/version/cardinality semantics, and LLM replay contract.
- Do not start E3.1 or E5.1 until E3.Foundation passes its physical collision, unknown-schema,
  source/tenant denial, revision/cursor, independent candidate-family, object-binding
  propose-time-denial, composite-bridge, and new-bounds acceptance tests.
- **E0 and E0b are not gated on any of the above** and do not wait for E3.0. Both run on shipped
  machinery. E0's only new dependency, registry cycle validation, is self-contained; E0b's two
  prerequisites — the governance route and deterministic LLM replay — are inside its own scope.
- **E0b's panel does not gate until its shadow numbers are published** (M8, M9). Until then it
  observes and suppresses nothing.
- Do not start E3 persistence or LLM work until E3.1 passes the read-only adversarial
  fixture for foreign keys, partial composite keys, schema collisions, missing
  cardinality, projection lag, candidate currentness, and hidden/stale metadata.
- Do not start E5 object/relationship assembly until E3.4's handoff is accepted **and Phase C has
  reported M10 for every consumption item.** If M10 is zero across the board, E3 produced an
  ontology that feature generation does not consume, and the correct response is to amend the
  Outcome rather than proceed to build screens on top of it.
- **Do not build a contract whose consumer is gated** (see the consumer gate above). In particular:
  no typed entity links until the template-library change that lets a recipe declare a relationship
  need is funded alongside them; no title bindings until a view consumer exists.
- E5.1 may proceed after E3.Foundation without waiting for E3.2–E3.4, but only after source
  entitlement, per-field property authority, exact server traversal bounds, fresh-only
  policy-driven reads, the authorized-and-refused diagnostic path, node-closed pagination, and
  signed scope/policy/revision-bound cursors are working. (Registry cycle rejection is no longer
  listed here — it lands in E0, well before this gate.)
- Do not claim executable `converted_by` feature generation until the formula grammar,
  compiler, and materializer support the required operation.

## Deferred NFRs

- Large-catalog materialized views/caching and incremental component maintenance.
- Bulk-by-convention link review and reviewer routing.
- Layout persistence, collaborative ontology editing, and custom visual layouts.
- Confidence calibration and model-quality dashboards.
- Query latency SLOs, telemetry, cost controls, and operational alerts.
- Row-level entity resolution, survivorship, golden records, and master-data matching.
- Ontology-backed data virtualization or operational actions/functions.

## First executable planning task

**Build E0, and run the E3.0 falsification pass alongside it.** These are the only two things this
plan is confident enough to start.

E0 ships a concept facet on the shipped cross-catalog search plus registry cycle validation. It is
days of work on machinery that already exists, it publishes the M1/M6 baselines, and it is the first
point at which a user can tell us the semantic map is not what they wanted — before the program
spends six phases on it.

The falsification pass is the other half. Rev 4 survived three adversarial reviews with three inert
foundations in its "already earned" list — the schema premise, the LLM replay store, and the CAS
singleton — because its exit gate asked for source citations for interfaces it had already assumed
existed. The pass inverts that: open every cited file and try to prove the capability is **not**
reachable. Its output is a refutation log, and it gates the rest of E3.0.

Only then write **E3.0**, whose largest open questions are now explicit: the read-scope column
decision, the legacy-bridge disposition, the bridge confirmation rule given that no ownership data
exists, the candidate-home consolidation, and whether E3.Foundation.0, the cardinality migration,
the LLM selection store and the bridge governance route are funded — because each of those, if not,
removes a promise from the Outcome rather than a test from a phase.

The first *implementation* slice after that is **E3.Foundation.0**, not E3.Foundation: the current
readers cannot express the physical identities E3.1 promises, so the physical store has nothing to
persist until the ingest contract carries schema. E3.1 then proves the handoff from schema-safe
physical metadata and governed facts before the program adds E3 proposal and relationship-selection
complexity.
