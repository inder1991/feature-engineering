# Task 4c — the 71-concept recipe triage, banking-SME pass (2026-08-10)

**Scope:** every concept carried by live, enriched columns that NO recipe asks for — 35 on `ftr`
(payments transaction record), 36 on `cib` (customer master). Verdicts: **AUTHOR NOW** (full card
drafted below), **AUTHOR LATER** (real value, blocked or diluted by a named constraint), **KEEP
UNUSED** (correctly idle — plumbing, identity, or display). Every drafted card names the live
columns it grounds on TODAY, and carries the safety fields the registry requires (PIT declaration,
additivity, leakage flag, eligibility note).

---

## Three findings that outrank the triage itself

### F1 — `tenure_days` is MIS-AUTHORED, and the tie-break cannot save it

The plan's flagship Task-2 example ("tenure measured from a KYC completion date, picked
alphabetically") is not, at root, a tie-break defect. Verified against the registry and catalog:

```
tenure_days needs:  Need("origination", "effective_date")     ← the authored need
its own notes:      "'origination_date' is an acceptable alternate"

cib columns:        cust_acct_opn_dt    → origination_date    ← UNUSED by any recipe
                    cust_reln_start_dt  → origination_date    ← UNUSED by any recipe
                    cust_kyc_complete_dt / _last_dt / cust_reactv_dt → effective_date  ← the tie
```

All three tied KYC dates are **correctly classified** — they ARE effective dates. The template asks
for the wrong concept, so the RIGHT columns (true relationship-start dates) never enter the
candidate set. **Task 2's adjudicator can only choose among the tied candidates; when the whole tied
set is wrong, only authoring fixes it.** 4c and 2 are complements, not substitutes.

The clean fix needs a small engine capability: `Need` names exactly ONE concept, so "prefer
`origination_date`, else `effective_date`" is inexpressible — which is precisely why the author
wrote the alternate into a comment instead of the need. **Engineering ask: ordered alternate
concepts on `Need`** (`concept=("origination_date", "effective_date")`, first match wins). Flipping
the need outright would silently break any catalog whose signup lives on `effective_date` (the Part-F
churn fixture does exactly that).

Same defect, same fix, in the other three recipes bound to the KYC-date tie: `product_breadth`,
`relationship_deepening_breadth`, `tenure_upsell_readiness` (their `open_close` need).

### F2 — one field correction removes roughly HALF of ftr's ambiguity

`tran_time` is classified `event_timestamp`. It is a **time-of-day component** of `tran_date`, not
an independent event clock — no dormancy, trend or window recipe should ever bind it. Every
`event_ts` tie on `ftr` (`dormancy_days`, `txn_frequency_trend`, `merchant_mcc_diversity`,
`cross_channel_rail_anomaly`, `fan_in_fan_out`, …) is the pair `[tran_date, tran_time]`.

**Correct `tran_time`'s concept once** (human field correction; if the registry lacks a
time-of-day/timestamp-component concept, add one — `ftr` proves the need) and those ties cease to
exist at the source. Cheaper than adjudicating the same tie six times, and it removes ~6 of `ftr`'s
13 ambiguous bindings before Task 2 even ships. Adjudicate what remains; correct what shouldn't be
ambiguous at all.

### F3 — `cib` has a product-holding PROXY nobody noticed

`cib` grounds only 10 recipes largely because it holds no product/balance data. But it carries SIX
`source_system_status` columns (`cust_advent_stat_flg`, `cust_calypso_stat_flg`,
`cust_finacle_stat_flg`, `cust_finone_stat_flg`, `cust_finoneawai_stat_flg`, `cust_vision_stat_flg`
— core banking, treasury, lending, cards…). **A customer active in four source systems holds
products in four systems.** System-footprint breadth is a legitimate, bank-standard proxy for
product breadth in exactly the catalogs that lack product tables — and the `Template` engine's
`distinct_binding_group` exists for this multi-column shape. Card drafted below.

---

## Tier 1 — AUTHOR NOW (cards drafted, ground on live columns today)

Aggregation names are indicative (the registry's aggregation vocabulary is authored per-card;
`covenant_headroom` and `tenure` set the precedent). Engineering maps them at implementation.

**AUTHORING RULE (verified against the engine, `templates.py:515`): an OPTIONAL need never gates
grounding** — when unmet it records a degrade note and the recipe grounds anyway. Therefore **every
card must carry at least one REQUIRED distinctive need**, or it grounds on any table that has the
generic skeleton (`event_ts` + `entity`) and emits a feature measuring nothing. The first draft of
R4, R7 and R8 made exactly this mistake — all their distinctive ingredients were optional — caught
in architecture review and corrected below. Optionals are for BREADTH (extra members of a
distinct-binding group, a second reference type), never for the ingredient that justifies the card.

### R1 `correspondent_concentration` — ftr

* **family** `network_crime` · **intent** "How concentrated and how novel is the customer's
  correspondent-bank network: distinct correspondents, top-correspondent share, share routed via
  correspondents first seen inside the window."
* **needs** `correspondent → bank_bic` · `event_ts → event_timestamp` · `entity → customer_id`
* **grounds on** `sender_bic, receiver_bic, counter_party_bic, corres_bank_intermediary_bic` (+
  `clearing_member_code` variant below) × `tran_date` × `cif_id`
* **params** `window (90, 180, 365)` · `measure (distinct_count, top_share, new_share, hhi)`
* **aggregation** `network_concentration` · **additivity** n/a · **near_label** False
* **pit** counts events strictly ≤ as_of; "first seen" resolved against pre-window history only.
* **use_cases** `financial_crime, transaction_monitoring, correspondent_banking, aml`
* **eligibility** presence/圈 counting only — no name matching, no sanctioned-list lookup (that is a
  screening system's job, not a feature's).
* Sister card `clearing_scheme_concentration` over `clearing_member_code` — identical shape,
  scheme-level codes; author both or parameterise the need via F1's ordered alternates.

### R2 `currency_diversity` — ftr

* **family** `flow_mix` · **intent** "Multi-currency behaviour: distinct transaction currencies and
  the non-base-currency share of flow, per customer per window."
* **needs** `ccy → currency_code` · `event_ts → event_timestamp` · `entity → customer_id` ·
  optional `amount → monetary_flow` (share-of-flow measures)
* **grounds on** `tran_crncy` (+2 siblings) × `tran_date` × `cif_id` × `tran_amt*`
* **params** `window (90, 180)` · `measure (distinct_count, non_base_share, entropy)`
* **additivity** n/a · **near_label** False · **use_cases** `aml, fx_cross_sell, behaviour`
* **pit** trailing window ≤ as_of. **eligibility** none beyond standard.

### R3 `back_valuation_lag` — ftr

* **family** `ops_integrity` · **intent** "Value-dating discipline: distribution of
  value_date − booking_date; share of back-valued transactions. Chronic back-valuation is an
  operational-risk and AML manipulation signal."
* **needs** `value → value_date` · `booked → booking_date` · `event_ts → event_timestamp` ·
  `entity → customer_id`
* **grounds on** `value_date` × `pstd_date` × `tran_date` × `cif_id` — **this also puts the two
  currently-unused date concepts to work**, and partially answers the plan's open
  `effective_date`-vs-`value_date` question: `value_date` has its own recipe home; do NOT alias it
  into `effective_date`.
* **params** `window (90, 180)` · `measure (back_valued_share, mean_lag_days, max_lag_days)`
* **additivity** n/a · **near_label** False · **use_cases** `operational_risk, aml, payment_ops`
* **pit** both dates ≤ as_of; lag computed only on settled rows.

### R4 `party_chain_depth` — ftr

* **family** `network_crime` · **intent** "Layering signal: share of payments carrying ultimate
  debtor/creditor or initiating-party fields that differ from (or exist beside) the direct parties —
  on-behalf-of intensity and average chain depth."
* **needs** `event_ts → event_timestamp` · `entity → customer_id` · **required**
  `party_1 → ultimate_debtor` · optional, distinct-binding-group `party_2 → ultimate_creditor` ·
  `party_3 → initiating_party` — one required party field or the card grounds on any transaction
  table and measures nothing (see authoring rule). Ordered alternates (capability ask #1) would let
  `party_1` accept any of the three; until then `ultimate_debtor` anchors it.
* **grounds on** `ultimate_debtor, ultimate_creditor, initiating_party` × `tran_date` × `cif_id`
* **params** `window (90, 180)` · `measure (ultimate_present_share, mean_chain_depth)`
* **additivity** n/a · **near_label** False · **use_cases** `aml, sanctions_ops, financial_crime`
* **eligibility** PRESENCE-based only. The party fields are PII names; the feature may count them,
  never read, compare or export them. No name similarity — that needs entity resolution the
  platform does not have (see capability gaps).

### R5 `narrative_completeness` — ftr

* **family** `payment_transparency` · **intent** "FATF-R16 transparency hygiene: share of payments
  with an empty or near-empty narrative/purpose text, per customer per window."
* **needs** `narrative → payment_narrative` · `event_ts → event_timestamp` · `entity → customer_id`
* **grounds on** 7 `payment_narrative` columns × `tran_date` × `cif_id`
* **params** `window (90, 180)` · `min_length (0, 5, 20)`
* **additivity** n/a · **near_label** False · **use_cases** `aml, payment_transparency, data_quality`
* **eligibility** LENGTH/PRESENCE only. Keyword semantics ("salary", "rent", "gift") are deferred —
  the aggregation engine has no governed text-match capability, and free-text matching without one is
  how false SAR volumes are born. Recorded under capability gaps.

### R6 `type_mix_shift` — ftr

* **family** `behaviour_mix` · **intent** "Transaction-type mix and its drift: entropy of type
  distribution, and distance between recent-window and prior-window mix."
* **needs** `type → transaction_type` (alternate `instrument_type` via F1's ordered alternates) ·
  `event_ts → event_timestamp` · `entity → customer_id`
* **params** `window (90, 180)` · `measure (entropy, mix_shift)`
* **additivity** n/a · **near_label** False · **use_cases** `fraud, engagement, behaviour`

### R7 `stp_reference_quality` — ftr

* **family** `payment_ops` · **intent** "Straight-through-processing hygiene: share of payments
  carrying an end-to-end reference; share carrying a gpi UETR."
* **needs** `event_ts → event_timestamp` · `entity → customer_id` · **required** `e2e →
  end_to_end_reference` · optional `uetr → swift_uetr` — the e2e reference is the card's reason to
  exist and must gate grounding; the UETR is breadth.
* **params** `window (90, 180)` · **additivity** n/a · **near_label** False
* **use_cases** `payment_ops, gpi_traceability, data_quality`

### R8 `system_footprint_breadth` — cib  (finding F3)

* **family** `relationship` · **intent** "Cross-system activation: in how many core source systems
  is this customer active — the product-holding proxy for a catalog with no product table."
* **needs** `entity → customer_id` · `asof → as_of_date` (verified: `cib.business_dt` carries it) ·
  **required** `sys_1 → source_system_status` · optional, **distinct_binding_group** `sys_2..sys_6 →
  source_system_status` — one required member or the card grounds on any customer snapshot with zero
  system flags and reports an "active count" over nothing.
* **grounds on** the six `cust_*_stat_flg` columns × `cust_num`
* **params** `measure (active_count, active_share)` · `active_tokens (governed value list)`
* **additivity** n/a · **near_label** False · **use_cases** `cross_sell, engagement,
  single_customer_view`
* **pit** snapshot semantics — cib is a dimension; the value is as-at the snapshot, and the card must
  carry the same advisory `data_role=snapshot` framing the existing snapshot recipes do.
* **eligibility** the "active" status tokens differ per source system — the token list is a governed
  parameter, not a hardcode.

### R9 `restriction_lifecycle` — cib  ⚠ near-label

* **family** `conduct_status` · **intent** "Suspension/blacklist lifecycle: currently restricted,
  ever restricted, and recency of the last restriction event."
* **needs** `status → restriction_status` · `entity → customer_id` · `asof → as_of_date` · optional
  `reason → restriction_reason` · optional `rel_status → customer_relationship_status`
* **grounds on** `cust_blacklist_flg, cust_negated_flg, cust_susp_flg` (+4 reason, +4 status cols) ×
  `cust_num`
* **params** `measure (currently_restricted, ever_restricted, days_since_restriction)`
* **additivity** n/a · **near_label TRUE** — mandatory.
* **eligibility** "⚠ NEAR-LABEL: for AML-investigation, exit/derisking and credit-default targets, a
  restriction IS the outcome or its direct administrative echo. The near-label control must compare
  the restriction event date to the label window; forbid when the target is any
  restriction/suspension/exit event."
* **use_cases** `aml, credit_risk, early_warning` — never conduct-outcome prediction.

### R10 `kyc_freshness` — cib  ⚠ near-label (compliance targets)

* **family** `compliance_ops` · **intent** "KYC review overdue-ness: days since last KYC refresh
  versus the policy cycle for the customer's risk rating; document-set completeness breadth."
* **needs** `kyc_date → effective_date` · `rating → customer_risk_rating` · `entity → customer_id` ·
  `asof → as_of_date` · optional `doc → kyc_document`
* **grounds on** `cust_kyc_last_dt` (yes — the tie again: this card inherits the
  `[kyc_complete, kyc_last, reactv]` ambiguity and is exactly what Task 2's adjudicator is FOR; the
  intent text names "last KYC refresh" so the adjudicator has something to reason from) ×
  `cust_kyc_risk_rating` × `cust_num`
* **params** `cycle_days_by_rating (governed: e.g. high=365, medium=730, low=1095)`
* **additivity** n/a · **near_label TRUE** for KYC-remediation targets (overdue-ness ≈ the backlog
  label). **use_cases** `compliance_ops, aml`.

### R11 `staff_account_flag` — cib

* **family** `conduct_status` · **intent** "Staff-account marker for conflict-of-interest and
  internal-fraud monitoring populations."
* **needs** `staff → staff_indicator` · `entity → customer_id` · `asof → as_of_date`
* **additivity** n/a · **near_label** False · **use_cases** `internal_fraud, conduct_monitoring`
* **eligibility** population-selection and monitoring only; forbidden as a credit or pricing input
  (staff status is an employment attribute — treat as fairness-adjacent).

### R12 `industry_risk_exposure` — cib

* **family** `segmentation_risk` · **intent** "Sector risk: membership of a governed high-risk /
  cash-intensive industry list, and (multi-account) sector concentration."
* **needs** `industry → industry_code` · `entity → customer_id` · `asof → as_of_date`
* **grounds on** 6 industry/sector columns × `cust_num`
* **params** `risk_list (a GOVERNED list id — never a hardcoded set of codes)`
* **additivity** n/a · **near_label** False · **use_cases** `aml, credit_concentration`
* **eligibility** the high-risk industry list is a policy artifact with an owner and a version; the
  card depends on it existing as governed reference data. Do not ship with an inline list.

---

## Tier 2 — AUTHOR LATER (named blocker or dilution)

| concept (cols) | recipe it wants | why not now |
|---|---|---|
| `new_to_bank_flag` (2, cib) | new-to-bank cohort flag / interaction | trivially authorable; near-label for onboarding-fraud targets — bundle with R9's eligibility pattern |
| `nominee_indicator` (1, cib) | nominee-account AML marker | single column; fold into a conduct/status card rather than standalone |
| `npe_flag` (1, cib) | non-performing marker | ⚠ effectively a credit LABEL — author only with near_label=True and a hard eligibility gate |
| `statement_visibility_flag` (2, ftr) | statement-suppression signal (concealment/dormancy) | real AML tell, thin evidence (2 cols); author after R1–R7 prove the ftr pipeline |
| `virtual_account_id` (1, ftr) | virtual-account usage breadth | single column; wait for a second vA source |
| `branch_id/branch_name` (4) | branch/geo concentration; branch-vs-digital migration | static segmentation on a snapshot — weak alone; stronger once transactions can see it (cross-catalog, Task 0) |
| `relationship_manager_name` (2, cib) | RM book concentration | staff PII as a key; needs an RM **id**, not a name — data ask, not a recipe |
| `legal_entity_type` (4, cib) | corporate-form segmentation | hint-grade segmentation; fold into R12's family later |
| `residency_status` (3, cib) | non-resident AML factor | **fairness proxy** (nationality-adjacent). FATF-legitimate for AML only: author restricted to `use_cases=(aml,)` with an explicit proxy eligibility note, after fairness review |
| `fatca_crs_classification` (1, cib) | reporting-status completeness | compliance-ops reporting, not model features; revisit if a compliance-ops surface appears |
| `customer_group_id` (1, cib) | household/group aggregation | needs group-level aggregation the engine lacks (cross-row entity rollup) |
| `kyc_narrative` (3, cib) | KYC prose signals | same text-capability gap as R5's keyword half |

## Tier 3 — KEEP UNUSED (correctly idle)

* **Plumbing / lineage:** `row_hash`, `record_deleted_flag`, `record_author`, `system_time`,
  `module_id`, `source_system`, `internal_transaction_serial`, `reporting_period`, `code_label`
  (label text FOR codes — the code columns are the signal), `boolean_flag` (a catch-all that should
  be re-classified per column, not fed to recipes), `fx_conversion_rate` (market data, not
  behaviour).
* **Identifiers (join/dedup material, not feature values):** `transaction_id`,
  `external_account_ref`, `account_name`, `lei`, `rating` (1 col, semantics unclear — clarify before
  anything), `alt_tran_ref_num` (unclassified anyway).
* **PII display / identity resolution:** `party_name` (11!), `counterparty_name`, `postal_address`,
  `phone_number`, `email_address`, `merchant_name`. The 11 `party_name` columns LOOK like the
  biggest untapped cluster and are the most tempting mistake in the list: name/address/phone
  features are network-analytics material (shared-attribute detection), which requires entity
  resolution and fuzzy matching this platform deliberately does not have. Counting them is R4's
  presence logic; anything more is a different product.

## Capability gaps the triage surfaced (engineering asks, ranked)

1. **Ordered alternate concepts on `Need`** — unblocks F1 cleanly, plus R1's sister card and R6.
2. **Governed reference-value parameters** — R8 (active tokens), R10 (cycle by rating), R12
   (high-risk industry list): parameters whose values are governed data, not authored literals.
3. **Governed text predicates** (length/presence now exist implicitly; keyword lists do not) — gates
   R5's second half and `kyc_narrative`.
4. **Entity/group rollup** — gates `customer_group_id` household features.

## What this buys, measured against the plan's own metric

Tier 1 alone: **+12 recipes grounding on live columns** (ftr 23 → ~30, cib 10 → ~15, exact counts to
be verified by the Step-0 instrumentation once cards land), consuming 40+ currently-idle columns —
against a plan whose next-best lever was worth 2. F2 removes ~6 of 13 ambiguous bindings by data
correction. F1 fixes the plan's flagship mis-binding at the root.

**Review protocol:** these cards are SME-drafted, not SME-approved. Each needs a second banking
reviewer (the four-eyes rule this platform applies to every governed fact applies to recipe
authoring too), and each lands with the standard registry tests: grounds on the live catalog,
survives the gauntlet, near-label flags render.
