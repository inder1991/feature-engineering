# Four-Objective Minimum Discovery Coverage

Status: proposed for independent banking-SME and product-owner approval.

This delivery closes deterministic discovery gaps for four active objectives. It proves only that a
confirmed scope retrieves at least one reviewed predictor recipe and that the recipe can be evaluated
honestly by catalog grounding. It does not prove formula authoring, point-in-time correctness,
predictive usefulness, planner compilation, or external execution.

| Objective | Authored anchor | Rationale | Known limitation |
|---|---|---|---|
| `credit.monitoring.obligor` | `obligor_facility_count` | Facility breadth is a bounded obligor exposure signal. | Needs governed history/event time for an executable window. |
| `fraud.merchant_fraud` | `merchant_mcc_diversity` | Merchant category breadth is a behavior signal, not a fraud label. | No high-risk-MCC set or transaction-value filter is asserted. |
| `treasury_alm.deposit_runoff_forecasting` | `contractual_deposit_maturity_profile` | Contractual maturities are a direct runoff-model input. | Requires a future-horizon temporal policy and scenario semantics. |
| `treasury_alm.net_interest_margin` | `lagged_net_interest_flow` | Lagged interest income and expense are observed NIM predictors. | Physical expense sign convention is not governed yet. |

Required independent approvals before release:

- Banking/ALM SME for runoff and NIM semantics.
- Credit-risk SME for obligor exposure semantics.
- Fraud SME for merchant-fraud relevance.
- Product owner for the `minimum_discovery_coverage` claim.

The machine gate is `python -m featuregen.overlay.upload.taxonomy.coverage_cli`. Its passing result is
necessary but is not evidence that the independent review above occurred.
