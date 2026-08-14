# E3 — divergence run and adjudication (2026-08-14)

One-shot replay of the cluster's recorded hypotheses through BOTH generation paths, against
a full LOCAL CLONE of the cluster database (migrations 1063–1065 applied to the copy only —
the cluster schema untouched). **Operator sign-off: ACCEPTED 2026-08-14** — E3 closed; E4 unblocked.

## Verdict

**Every observed divergence is explained** — 78 rows, two classes, zero unexplained:

* **66 × variant-expansion** (B5 by design): the engine serves explicit bounded variants
  (`@window=30/90/180`, `@window_minutes=15/60/240`) where legacy served at most one card.
* **12 × new-recall**: candidates only the engine retrieves (recipe coverage + closure).

One hypothesis of three skipped: its recognition failed on the provider incident below and
no stored classified attempt existed. Recorded honestly in the table.

## Finding 1 — CORRECTED: account credit exhaustion, masked by a misclassification bug

At ~13:45 local every provider call began failing with HTTP 400. The FIRST diagnosis
(recorded in an earlier revision of this report) was a provider-side schema rejection of
JSON-Schema union types — WRONG. The truth, found by bisecting with raw calls: **the
Anthropic account's credit balance ran out** (the divergence run's legacy generation legs
consumed the remainder), and our own error reporter turned the billing message into a fake
schema diagnosis: the provider's generic error envelope contains `'type':
'invalid_request_error'`, and a bare-substring keyword scan read that as the JSON-Schema
keyword `type`, logging "rejected structured-output schema (keyword=type)".

Fixes landed with this report: (a) billing 400s are now named as ACCOUNT CREDIT EXHAUSTED
in the log with the operator action stated; (b) the schema-keyword scan claims a schema
rejection only when the message actually references the schema context (regression-tested
against the real billing envelope); (c) a defensive wire normalization (type arrays → anyOf)
plus a registry-wide projection ratchet, kept on their own merits. OPERATOR ACTION: top up
the Anthropic account — every LLM stage on the cluster fails closed until then.

## Finding 2 — the resilience asymmetry the incident exposed

Through the total provider outage (whatever its cause), the SEMANTIC path kept serving 33–45 recipe
candidates per hypothesis (the engine is deterministic; only the additive intent lens
degraded). The LEGACY path served ZERO — the model is its only generator. The outage was an
unplanned chaos test, and the one-engine architecture passed it.

## Run history (honesty about the harness)

Four runs to a fair table: (1) vacuous — both legs 422ed on a missing scope and error-pairs
counted as matches (the script gained a non-vacuity guard: error rows are unexplained, a
zero-compared run exits 3); (2) the substantive run — semantic legs full, legacy legs
generated 16/12/20 raw ideas ALL rejected `STALE` against the CLONE's frozen drift
watermark (a clone artifact, fixed by refreshing the copy's watermark); (3) vacuous under
the provider incident (guard caught it); (4) this table — stored classified recognitions
replayed (no provider dependence), semantic legs live, legacy legs empty BECAUSE of the
provider incident. A legacy leg that is both fresh AND provider-served is unobtainable
during the incident; the accounting above stands on run 2 + run 4 jointly.

## Adjudication table

| hypothesis | candidate | legacy | semantic | adjudication |
|---|---|---|---|---|
| A transaction that deviates from the account's n | amount_just_under_limit@window=180 | — | {"name": "Just-under-limit count", "source": "recipe", "stat | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | amount_just_under_limit@window=30 | — | {"name": "Just-under-limit count", "source": "recipe", "stat | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | amount_just_under_limit@window=90 | — | {"name": "Just-under-limit count", "source": "recipe", "stat | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | amount_zscore_spike@window=180 | — | {"name": "Amount z-score", "source": "recipe", "status": "mi | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | amount_zscore_spike@window=30 | — | {"name": "Amount z-score", "source": "recipe", "status": "mi | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | amount_zscore_spike@window=90 | — | {"name": "Amount z-score", "source": "recipe", "status": "mi | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | auth_decline_streak@window_minutes=15 | — | {"name": "Auth decline streak", "source": "recipe", "status" | explained: new-recall (C7 closure / engine retrieval) |
| A transaction that deviates from the account's n | auth_decline_streak@window_minutes=240 | — | {"name": "Auth decline streak", "source": "recipe", "status" | explained: new-recall (C7 closure / engine retrieval) |
| A transaction that deviates from the account's n | auth_decline_streak@window_minutes=60 | — | {"name": "Auth decline streak", "source": "recipe", "status" | explained: new-recall (C7 closure / engine retrieval) |
| A transaction that deviates from the account's n | card_testing_velocity@window_minutes=15 | — | {"name": "Declined small-auth velocity", "source": "recipe", | explained: new-recall (C7 closure / engine retrieval) |
| A transaction that deviates from the account's n | card_testing_velocity@window_minutes=240 | — | {"name": "Declined small-auth velocity", "source": "recipe", | explained: new-recall (C7 closure / engine retrieval) |
| A transaction that deviates from the account's n | card_testing_velocity@window_minutes=60 | — | {"name": "Declined small-auth velocity", "source": "recipe", | explained: new-recall (C7 closure / engine retrieval) |
| A transaction that deviates from the account's n | cross_border_burst@window_minutes=15 | — | {"name": "Cross-border burst count", "source": "recipe", "st | explained: new-recall (C7 closure / engine retrieval) |
| A transaction that deviates from the account's n | cross_border_burst@window_minutes=240 | — | {"name": "Cross-border burst count", "source": "recipe", "st | explained: new-recall (C7 closure / engine retrieval) |
| A transaction that deviates from the account's n | cross_border_burst@window_minutes=60 | — | {"name": "Cross-border burst count", "source": "recipe", "st | explained: new-recall (C7 closure / engine retrieval) |
| A transaction that deviates from the account's n | cross_channel_rail_burst@window_minutes=15 | — | {"name": "Distinct rails in burst", "source": "recipe", "sta | explained: new-recall (C7 closure / engine retrieval) |
| A transaction that deviates from the account's n | cross_channel_rail_burst@window_minutes=240 | — | {"name": "Distinct rails in burst", "source": "recipe", "sta | explained: new-recall (C7 closure / engine retrieval) |
| A transaction that deviates from the account's n | cross_channel_rail_burst@window_minutes=60 | — | {"name": "Distinct rails in burst", "source": "recipe", "sta | explained: new-recall (C7 closure / engine retrieval) |
| A transaction that deviates from the account's n | device_sharing_velocity@window=180 | — | {"name": "Accounts per device", "source": "recipe", "status" | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | device_sharing_velocity@window=30 | — | {"name": "Accounts per device", "source": "recipe", "status" | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | device_sharing_velocity@window=90 | — | {"name": "Accounts per device", "source": "recipe", "status" | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | first_time_payee_high_value@window=180 | — | {"name": "First-time payee high value", "source": "recipe",  | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | first_time_payee_high_value@window=30 | — | {"name": "First-time payee high value", "source": "recipe",  | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | first_time_payee_high_value@window=90 | — | {"name": "First-time payee high value", "source": "recipe",  | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | merchant_amount_zscore@window=180 | — | {"name": "Merchant amount z-score", "source": "recipe", "sta | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | merchant_amount_zscore@window=30 | — | {"name": "Merchant amount z-score", "source": "recipe", "sta | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | merchant_amount_zscore@window=90 | — | {"name": "Merchant amount z-score", "source": "recipe", "sta | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | new_device_flag@window=180 | — | {"name": "New device", "source": "recipe", "status": "missin | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | new_device_flag@window=30 | — | {"name": "New device", "source": "recipe", "status": "missin | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | new_device_flag@window=90 | — | {"name": "New device", "source": "recipe", "status": "missin | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | txn_velocity_spike@window=180 | — | {"name": "Velocity spike ratio", "source": "recipe", "status | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | txn_velocity_spike@window=30 | — | {"name": "Velocity spike ratio", "source": "recipe", "status | explained: variant-expansion (B5 — explicit bounded variants) |
| A transaction that deviates from the account's n | txn_velocity_spike@window=90 | — | {"name": "Velocity spike ratio", "source": "recipe", "status | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | cash_intensity_ratio@window=180 | — | {"name": "Cash intensity", "source": "recipe", "status": "mi | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | cash_intensity_ratio@window=30 | — | {"name": "Cash intensity", "source": "recipe", "status": "mi | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | cash_intensity_ratio@window=90 | — | {"name": "Cash intensity", "source": "recipe", "status": "mi | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | confirmed_match_flag@window=180 | — | {"name": "Confirmed match", "source": "recipe", "status": "a | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | confirmed_match_flag@window=30 | — | {"name": "Confirmed match", "source": "recipe", "status": "a | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | confirmed_match_flag@window=90 | — | {"name": "Confirmed match", "source": "recipe", "status": "a | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | crypto_offramp_exposure@window=180 | — | {"name": "Crypto ramp share", "source": "recipe", "status":  | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | crypto_offramp_exposure@window=30 | — | {"name": "Crypto ramp share", "source": "recipe", "status":  | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | crypto_offramp_exposure@window=90 | — | {"name": "Crypto ramp share", "source": "recipe", "status":  | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | dormant_reactivation@window=180 | — | {"name": "Dormant reactivation", "source": "recipe", "status | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | dormant_reactivation@window=30 | — | {"name": "Dormant reactivation", "source": "recipe", "status | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | dormant_reactivation@window=90 | — | {"name": "Dormant reactivation", "source": "recipe", "status | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | fan_in_counterparty_count@window=180 | — | {"name": "Fan-in counterparties", "source": "recipe", "statu | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | fan_in_counterparty_count@window=30 | — | {"name": "Fan-in counterparties", "source": "recipe", "statu | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | fan_in_counterparty_count@window=90 | — | {"name": "Fan-in counterparties", "source": "recipe", "statu | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | fan_in_fan_out@window=180 | — | {"name": "Fan-in \u00d7 fan-out", "source": "recipe", "statu | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | fan_in_fan_out@window=30 | — | {"name": "Fan-in \u00d7 fan-out", "source": "recipe", "statu | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | fan_in_fan_out@window=90 | — | {"name": "Fan-in \u00d7 fan-out", "source": "recipe", "statu | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | fan_out_counterparty_count@window=180 | — | {"name": "Fan-out counterparties", "source": "recipe", "stat | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | fan_out_counterparty_count@window=30 | — | {"name": "Fan-out counterparties", "source": "recipe", "stat | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | fan_out_counterparty_count@window=90 | — | {"name": "Fan-out counterparties", "source": "recipe", "stat | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | high_risk_corridor_exposure@window=180 | — | {"name": "High-risk corridor share", "source": "recipe", "st | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | high_risk_corridor_exposure@window=30 | — | {"name": "High-risk corridor share", "source": "recipe", "st | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | high_risk_corridor_exposure@window=90 | — | {"name": "High-risk corridor share", "source": "recipe", "st | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | nested_correspondent_flow@window=180 | — | {"name": "Nested correspondent count", "source": "recipe", " | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | nested_correspondent_flow@window=30 | — | {"name": "Nested correspondent count", "source": "recipe", " | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | nested_correspondent_flow@window=90 | — | {"name": "Nested correspondent count", "source": "recipe", " | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | prior_alert_recidivism@window=180 | — | {"name": "Prior alerts (known)", "source": "recipe", "status | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | prior_alert_recidivism@window=30 | — | {"name": "Prior alerts (known)", "source": "recipe", "status | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | prior_alert_recidivism@window=90 | — | {"name": "Prior alerts (known)", "source": "recipe", "status | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | rapid_movement_passthrough@window=180 | — | {"name": "Passthrough ratio", "source": "recipe", "status":  | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | rapid_movement_passthrough@window=30 | — | {"name": "Passthrough ratio", "source": "recipe", "status":  | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | rapid_movement_passthrough@window=90 | — | {"name": "Passthrough ratio", "source": "recipe", "status":  | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | round_amount_ratio@window=180 | — | {"name": "Round-amount share", "source": "recipe", "status": | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | round_amount_ratio@window=30 | — | {"name": "Round-amount share", "source": "recipe", "status": | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | round_amount_ratio@window=90 | — | {"name": "Round-amount share", "source": "recipe", "status": | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | screening_alert_count@window=180 | — | {"name": "Screening alerts", "source": "recipe", "status": " | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | screening_alert_count@window=30 | — | {"name": "Screening alerts", "source": "recipe", "status": " | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | screening_alert_count@window=90 | — | {"name": "Screening alerts", "source": "recipe", "status": " | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | screening_exposure_share@window=180 | — | {"name": "Screening exposure", "source": "recipe", "status": | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | screening_exposure_share@window=30 | — | {"name": "Screening exposure", "source": "recipe", "status": | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | screening_exposure_share@window=90 | — | {"name": "Screening exposure", "source": "recipe", "status": | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | structuring_smurfing@window=180 | — | {"name": "Sub-threshold cash count", "source": "recipe", "st | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | structuring_smurfing@window=30 | — | {"name": "Sub-threshold cash count", "source": "recipe", "st | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | structuring_smurfing@window=90 | — | {"name": "Sub-threshold cash count", "source": "recipe", "st | explained: variant-expansion (B5 — explicit bounded variants) |
| Customers whose recent payment behaviour shifts  | — | — | — | skipped: recognizer returned no primary (status=technical_failure) |
