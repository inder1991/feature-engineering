"""Parametric feature-template engine + the ``retail_churn`` recipe set (build task B2).

Three things live here, standalone (NOT wired into the considered-set / generation flow — that is B4):

1. A parametric **template model** (:class:`Need`, :class:`Template`, :class:`GroundedFeature`) — the
   "cookbook" schema. A template is a *scaffold, not a cage* (domain-intelligence spec §5): it seeds
   generation with an expert-curated, safe-by-construction pattern; the LLM still composes/extends beyond
   it and un-templated requests still work.
2. A deterministic **grounding engine** (:func:`ground_template`, :func:`ground_all`) — NO LLM. It binds a
   template's abstract ``needs`` to a catalog's concept-tagged ``graph_node`` columns (read-scoped by
   sensitivity like the rest of the overlay) and yields a safe candidate :class:`GroundedFeature`, or
   degrades / skips when a required need can't ground.
3. :data:`RETAIL_CHURN_TEMPLATES` — the 12 pilot recipes authored faithfully from the SME library,
   ``docs/superpowers/specs/2026-07-08-banking-feature-template-library.md`` §PART F.

**Safety by construction.** Grounding refuses, structurally, to bind any column that is a *leakage
anchor* (a target / target-defining column — §3.10/§3.7 of the taxonomy) or a *protected_attribute* /
*special_category* column (fair-lending / GDPR). This holds even when a template is mis-authored to
*need* such a concept, and even when a column would be picked *structurally* (via ``is_grain`` /
``is_as_of``) rather than by concept — see :func:`_safe_to_bind`.

**Point-in-time (PIT) is a DESIGN-TIME declaration.** Every template bakes in a trailing-window PIT rule
``(as_of − window, as_of]`` and carries it onto the :class:`GroundedFeature`. This platform has **no data
plane** — it cannot read fact rows, so it CANNOT enforce PIT (or currency conversion, or "balance has
history") at runtime. The declaration travels with the candidate so a downstream executor can honour it;
the honest limit is that grounding asserts intent, not enforcement.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY, concept
from featuregen.overlay.upload.read_scope import allowed_sensitivities

# Concept-level sensitivities that a feature input may NEVER carry (a hard eligibility block, Part D.4).
# Distinct from the column-STORED sensitivity (pii/restricted) that read_scope filters on: these are
# behavioural classes declared by the column's *concept* in CONCEPT_REGISTRY.
_BLOCKED_SENSITIVITIES = frozenset({"protected_attribute", "special_category"})


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The template model
# ──────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Need:
    """One binding slot of a template — a required (or optional) concept the grounding engine must find a
    column for. ``concept`` is a NAME that must exist in ``CONCEPT_REGISTRY`` (validated at import)."""
    role: str            # binding slot, e.g. "stock_col", "asof", "entity", "flow_col", "event_ts"
    concept: str         # required concept NAME (must exist in CONCEPT_REGISTRY)
    optional: bool = False


@dataclass(frozen=True, slots=True)
class Template:
    """A parametric, safe-by-construction feature recipe. The first four extra fields beyond the
    core schema (``stage``…``notes``) carry SME authoring metadata (funnel stage, eligibility note,
    near-label flag, declared downstream derivations, concept-substitution notes); all default so the
    core positional schema is unchanged."""
    id: str                          # e.g. "balance_trend"
    family: str                      # e.g. "balance_stock"
    intent: str                      # one-line business meaning
    needs: tuple[Need, ...]          # the grounding contract
    params: dict[str, tuple]         # {"window": (90, 60, 30)} — allowed values, first = default
    aggregation: str                 # base aggregation label ("trend" -> bound feature carries "trend_90d")
    additivity: str                  # the OUTPUT's additivity ("n/a"|"additive"|"semi_additive"|"non_additive")
    explain: str                     # "H"|"M"|"L"
    use_cases: tuple[str, ...]
    pit: str                         # human-readable trailing-window PIT rule — baked in (design-time)
    degrade: str = ""                # what to do if a need is unmet (skip / fall back)
    # ── authored extensions (optional; defaults keep the core constructor intact) ──
    stage: str = ""                  # attrition-funnel stage this template sits on (Part F / B1)
    eligibility: str = ""            # sensitivity / regulatory note (e.g. income sensitive; PII consent)
    near_label: bool = False         # borders the outcome label -> the 3-part leakage control must flag it
    derived: tuple[str, ...] = ()    # declared DOWNSTREAM derivations (no data plane runs them here, §D.8)
    notes: tuple[str, ...] = ()      # authoring notes (e.g. concept substitutions vs Part F)


@dataclass(frozen=True, slots=True)
class GroundedFeature:
    """A template bound to concrete catalog columns — a safe candidate ready for B4 to consider. The
    trailing-window ``pit`` and ``near_label`` flag are DESIGN-TIME declarations that travel with the
    candidate; nothing here enforces them at runtime (no data plane)."""
    template_id: str
    name: str                                     # e.g. "balance_trend_90d" (id + params)
    aggregation: str                              # e.g. "trend_90d"
    grain_table: str | None
    as_of_column: str | None
    derives_pairs: tuple[tuple[str, str], ...]    # (catalog_source, object_ref) bound columns
    params: dict
    # safety metadata baked in at grounding (declarations, not runtime-enforced):
    pit: str = ""
    additivity: str = ""
    explain: str = ""
    near_label: bool = False
    eligibility: str = ""
    notes: tuple[str, ...] = ()                   # substitutions + declared derivations + unmet-optionals


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The grounding engine (deterministic — NO LLM)
# ──────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class _Col:
    """A read-scoped ``graph_node`` column row, loaded once per grounding call."""
    catalog_source: str
    object_ref: str
    table: str
    column: str
    data_type: str | None
    is_grain: bool
    is_as_of: bool
    concept: str | None
    entity: str | None
    additivity: str | None
    sensitivity: str | None
    currency: str | None


def _load_columns(conn, catalog_source: str, roles: Iterable[str]) -> list[_Col]:
    """Every ``kind='column'`` node for the source the caller may see. READ-SCOPED exactly like the rest
    of the overlay: an untagged (NULL sensitivity) column is always visible; a pii/restricted column is
    visible only when the caller's roles grant that class (``allowed_sensitivities``). This is a HARD
    filter — a restricted column the caller can't see simply isn't a grounding candidate."""
    rows = conn.execute(
        "SELECT catalog_source, object_ref, table_name, column_name, data_type, is_grain, is_as_of, "
        "       concept, entity, additivity, sensitivity, currency "
        "FROM graph_node "
        "WHERE kind = 'column' AND catalog_source = %s "
        "  AND (sensitivity IS NULL OR sensitivity = ANY(%s)) "
        "ORDER BY table_name, column_name",
        (catalog_source, allowed_sensitivities(roles))).fetchall()
    return [_Col(*r) for r in rows]


def _safe_to_bind(col: _Col) -> bool:
    """Safety by construction — a column is NEVER a valid feature input if its concept is a leakage
    anchor (a target / target-defining column) or a protected_attribute / special_category. Applied to
    EVERY candidate, so it holds both for a concept-matched pick and for a structural (is_grain/is_as_of)
    pick, and even if a template is mis-authored to *need* such a concept."""
    if col.concept is None:
        return True                              # untagged column — no known behavioural danger
    c = concept(col.concept)
    if c is None:
        return True                              # unknown concept string — nothing dangerous is asserted
    if c.leakage_anchor:
        return False                             # §3.10/§3.7 — reading the target = leakage
    if c.sensitivity in _BLOCKED_SENSITIVITIES:
        return False                             # ECOA/fair-lending + GDPR special category — hard block
    return True


def _match(cols: Sequence[_Col], need: Need) -> _Col | None:
    """Pick the best SAFE column for a need, or None. A column scores by how well it fits:
    exact concept match is strongest; for an as-of need an ``is_as_of`` column also qualifies; for an
    identifier/entity need an ``is_grain`` (then entity-tagged) column also qualifies. A column with no
    positive signal is not a match. Ties break on column name for determinism. Unsafe columns
    (:func:`_safe_to_bind`) are skipped BEFORE scoring — so a structural is_grain/is_as_of pick that lands
    on a target / protected column is refused, not silently bound."""
    c = concept(need.concept)                    # exists (validated at import), but guard anyway
    want_as_of = bool(c and c.pit_role == "as_of")
    want_entity = bool(c and c.entity_link)
    best: _Col | None = None
    best_score = 0
    for col in cols:                             # already ordered by (table, column) from SQL
        if not _safe_to_bind(col):
            continue
        score = 0
        if col.concept == need.concept:
            score -= 4                           # exact concept match — the intended binding
        if want_as_of and col.is_as_of:
            score -= 2                           # a declared as-of column fits an as-of need
        if want_entity and col.is_grain:
            score -= 2                           # the grain column fits the entity need
        if want_entity and col.entity:
            score -= 1                           # an entity-tagged column is a weaker entity fit
        if score < 0 and score < best_score:
            best, best_score = col, score
    return best


def _bind_params(template: Template, overrides: dict | None) -> dict:
    """Resolve params: default = first allowed value; an override must be in the allowed tuple; an
    unknown param key is rejected (a caller error, not a grounding degrade)."""
    overrides = overrides or {}
    unknown = set(overrides) - set(template.params)
    if unknown:
        raise ValueError(f"unknown param(s) {sorted(unknown)} for template {template.id!r}")
    bound: dict = {}
    for key, allowed in template.params.items():
        value = overrides.get(key, allowed[0])
        if value not in allowed:
            raise ValueError(
                f"param {key}={value!r} not allowed for template {template.id!r} (allowed: {allowed})")
        bound[key] = value
    return bound


def _feature_name(template: Template, bound: dict) -> str:
    window = bound.get("window")
    return f"{template.id}_{window}d" if window is not None else template.id


def _aggregation_label(template: Template, bound: dict) -> str:
    window = bound.get("window")
    return f"{template.aggregation}_{window}d" if window is not None else template.aggregation


def _is_entity_concept(concept_name: str | None) -> bool:
    if not concept_name:
        return False
    c = concept(concept_name)
    return bool(c and c.entity_link)


def _is_as_of_concept(concept_name: str | None) -> bool:
    if not concept_name:
        return False
    c = concept(concept_name)
    return bool(c and c.pit_role == "as_of")


def ground_template(conn, template: Template, *, catalog_source: str,
                    roles: Iterable[str] = (), params: dict | None = None) -> GroundedFeature | None:
    """Bind ``template`` to ``catalog_source``'s concept-tagged columns, or return None if a REQUIRED
    need can't ground (the caller then degrades / skips — see the template's ``degrade``).

    Steps: (1) resolve params; (2) for each need, find a safe column (:func:`_match`) — an optional need
    may be absent (recorded as a declared-downstream/degrade note); a required need that is unmet
    ungrounds the whole template; (3) refuse, by construction, any leakage-anchor / protected column;
    (4) bake the trailing-window PIT rule + additivity + near-label flag into the result.

    Note the honest limits: PIT, single-currency, and "the stock has time history" are DECLARED here
    (there is no data plane to verify fact rows) — a downstream executor must honour them.
    """
    bound = _bind_params(template, params)
    cols = _load_columns(conn, catalog_source, roles)

    bindings: dict[str, _Col] = {}
    notes = list(template.notes) + list(template.derived)
    for need in template.needs:
        col = _match(cols, need)
        if col is None:
            if need.optional:
                notes.append(
                    f"optional need '{need.role}' ({need.concept}) unmet -> "
                    f"{template.degrade or 'declared downstream derivation (§D.8)'}")
                continue
            return None                           # ungroundable required need -> caller degrades/skips
        bindings[need.role] = col

    # Provenance: the (catalog_source, object_ref) of each bound column, deduped, in needs order.
    derives: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    entity_col: _Col | None = None
    as_of_col: _Col | None = None
    for need in template.needs:
        col = bindings.get(need.role)
        if col is None:
            continue
        pair = (col.catalog_source, col.object_ref)
        if pair not in seen:
            seen.add(pair)
            derives.append(pair)
        if entity_col is None and _is_entity_concept(need.concept):
            entity_col = col
        if as_of_col is None and (_is_as_of_concept(need.concept) or col.is_as_of):
            as_of_col = col

    if entity_col is not None:
        grain_table: str | None = entity_col.table
    elif bindings:
        grain_table = next(iter(bindings.values())).table   # no entity bound -> first bound column's table
    else:
        grain_table = None

    return GroundedFeature(
        template_id=template.id,
        name=_feature_name(template, bound),
        aggregation=_aggregation_label(template, bound),
        grain_table=grain_table,
        as_of_column=as_of_col.column if as_of_col else None,
        derives_pairs=tuple(derives),
        params=bound,
        pit=template.pit,
        additivity=template.additivity,
        explain=template.explain,
        near_label=template.near_label,
        eligibility=template.eligibility,
        notes=tuple(notes),
    )


def ground_all(conn, templates: Iterable[Template], *, catalog_source: str,
               roles: Iterable[str] = (), use_case: str | None = None) -> list[GroundedFeature]:
    """Ground every template that can ground (default params), skipping the ungroundable. When
    ``use_case`` is given, only templates whose ``use_cases`` include it are considered."""
    out: list[GroundedFeature] = []
    for template in templates:
        if use_case is not None and use_case not in template.use_cases:
            continue
        grounded = ground_template(conn, template, catalog_source=catalog_source, roles=roles)
        if grounded is not None:
            out.append(grounded)
    return out


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The 12 retail_churn templates — authored from Part F (§F.1–§F.12) of the SME library.
#
# Concept substitutions (the taxonomy §3 registry has no dedicated concept for a few Part-F roles — the
# closest registry concept is used and NOTED on the template, per the B2 brief):
#   • entity {customer}         -> customer_id      (Part F table says "customer_identifier"; §3 canonical)
#   • salary tag                -> category_code    (Part F: transactions.type; optional -> degrade)
#   • dr/cr direction           -> transaction_type (optional -> degrade to amount-sign, §D.8)
#   • direct-debit mandate event-> transaction_type (no "direct_debit" concept; skip if absent)
#   • product_holding           -> product_type     (no "product_holding" concept)
#   • beneficiary_bank          -> counterparty_id  (the receiving bank as a counterparty)
#   • customer_name / beneficiary_name -> pii        (a name is PII; read-scoped + consent-gated)
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_SUB_ENTITY = "concept sub: entity uses 'customer_id' (Part F: customer_identifier)"
_PIT_TRAILING = ("trailing window (as_of − {window}, as_of], values knowable strictly ≤ as_of; "
                 "never forward. DESIGN-TIME declaration — no data plane enforces runtime PIT.")

RETAIL_CHURN_TEMPLATES: tuple[Template, ...] = (
    # F.1 — balance_trend (Stage 3, headline drain signal)
    Template(
        id="balance_trend", family="balance_stock",
        intent="OLS slope of a customer's balance vs time over a trailing window — the core deposit-drain "
               "/ attrition signal.",
        needs=(Need("stock_col", "monetary_stock"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={"window": (90, 60, 30), "measure": ("normalized", "slope")},
        aggregation="trend", additivity="n/a", explain="H",
        use_cases=("retail_churn", "deposit_attrition", "early_warning"),
        pit=_PIT_TRAILING,
        degrade="only a current balance with no time history -> SKIP (no trend from one point); the "
                "history requirement is declared, not verifiable here (no data plane).",
        stage="3-financial-migration",
        eligibility="bind a monetary_stock (never a flow); single currency — convert to base first.",
        notes=(_SUB_ENTITY,
               "'measure=normalized' divides the slope by window-mean balance (scale-free)."),
    ),
    # F.2 — dormancy_days (baseline recency, NEAR-LABEL)
    Template(
        id="dormancy_days", family="recency",
        intent="Days since the customer's last activity: as_of − max(event_ts).",
        needs=(Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"event_filter": ("any",)},
        aggregation="recency", additivity="n/a", explain="H",
        use_cases=("retail_churn", "engagement", "collections"),
        pit="last event strictly ≤ as_of. DESIGN-TIME declaration — no data plane enforces it.",
        degrade="",
        stage="baseline",
        near_label=True,
        eligibility="⚠ NEAR-LABEL: if churn is defined as 'no activity in N days' this ≈ the label. The "
                    "3-part leakage control must FLAG it (confirm pre-as_of only, and window ≠ label window).",
        notes=(_SUB_ENTITY, "borders the churn label — see the attrition-funnel leakage trap (Part D.9)."),
    ),
    # F.3 — txn_frequency_trend (Stage 2, engagement decay)
    Template(
        id="txn_frequency_trend", family="activity_trend",
        intent="Is transaction activity decaying? count(events in recent half) / count(prior half); <1 "
               "means declining.",
        needs=(Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (90, 60, 180), "measure": ("halves_ratio", "slope")},
        aggregation="frequency_trend", additivity="n/a", explain="H",
        use_cases=("retail_churn", "cross_sell", "engagement"),
        pit=_PIT_TRAILING,
        stage="2-disengagement",
        notes=(_SUB_ENTITY,),
    ),
    # F.4 — inflow_outflow_ratio (Stage 3, net draining?)
    Template(
        id="inflow_outflow_ratio", family="cashflow_ratio",
        intent="Debits vs credits in a window — is the account net-draining? measure=net -> credits−debits.",
        needs=(Need("flow_col", "monetary_flow"), Need("direction", "transaction_type", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (90, 30, 60, 180), "measure": ("ratio", "net")},
        aggregation="inflow_outflow", additivity="non_additive", explain="H",
        use_cases=("retail_churn", "sme_credit", "cashflow"),
        pit=_PIT_TRAILING,
        degrade="no dr/cr flag -> infer direction from the amount sign (declared derivation, §D.8).",
        stage="3-financial-migration",
        eligibility="single currency — convert to base first.",
        derived=("is_debit := amount_sign(amount) < 0 — declared downstream when no dr/cr column exists.",),
        notes=(_SUB_ENTITY,
               "concept sub: direction uses 'transaction_type' (a dr/cr code); optional -> degrade.",
               "OUTPUT additivity is measure-dependent: measure=net is additive, measure=ratio is "
               "non-additive (default carries the ratio case)."),
    ),
    # F.5 — days_below_threshold (Stage 3, near-empty)
    Template(
        id="days_below_threshold", family="balance_stock",
        intent="Count of distinct days the balance sat under a floor in the trailing window.",
        needs=(Need("stock_col", "monetary_stock"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={"window": (90, 60, 30), "threshold_pct": (10, 5, 25)},
        aggregation="days_below", additivity="additive", explain="H",
        use_cases=("retail_churn", "overdraft_propensity", "hardship"),
        pit=_PIT_TRAILING,
        stage="3-financial-migration",
        eligibility="bind a monetary_stock; single currency.",
        notes=(_SUB_ENTITY,
               "threshold_pct is a percentile of the customer's OWN history; an absolute threshold is "
               "also permitted downstream."),
    ),
    # F.6 — salary_signal (Stage 3, salary cessation / irregularity)
    Template(
        id="salary_signal", family="income_signal",
        intent="Over salary-tagged credits: cessation_flag (salary stopped when previously regular) / "
               "gap_std (irregularity) / latest_gap (days since last salary).",
        needs=(Need("flow_col", "monetary_flow"), Need("salary_tag", "category_code", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (90, 60, 180), "measure": ("cessation_flag", "gap_std", "latest_gap")},
        aggregation="salary_signal", additivity="n/a", explain="H",
        use_cases=("retail_churn", "primacy_loss", "affordability"),
        pit=_PIT_TRAILING,
        degrade="no salary tag -> derive from recurring same-amount ~monthly credits (declared "
                "derivation §D.8; probabilistic — FLAG).",
        stage="3-financial-migration",
        eligibility="income is SENSITIVE — churn-permitted but flagged.",
        derived=("is_salary := recurring same-amount ~monthly credit — declared downstream when no "
                 "salary tag exists (probabilistic).",),
        notes=(_SUB_ENTITY, "concept sub: salary tag uses 'category_code' (transactions.type)."),
    ),
    # F.7 — product_breadth / product_attrition (Stage 4, unbundling)
    Template(
        id="product_breadth", family="product_holding",
        intent="breadth = count(distinct products held at as_of); measure=attrition = "
               "breadth(as_of) − breadth(as_of−window) (the unbundling signal).",
        needs=(Need("product", "product_type"), Need("open_close", "effective_date"),
               Need("entity", "customer_id")),
        params={"window": (90, 180, 365), "measure": ("breadth", "attrition")},
        aggregation="product_breadth", additivity="additive", explain="H",
        use_cases=("retail_churn", "cross_sell", "share_of_wallet"),
        pit="products with open ≤ as_of < close. DESIGN-TIME declaration — no data plane enforces it.",
        degrade="no product-holding data -> SKIP.",
        stage="4-unbundling",
        notes=(_SUB_ENTITY,
               "concept sub: product_holding uses 'product_type' (no product_holding concept in §3)."),
    ),
    # F.8 — tenure_days (context)
    Template(
        id="tenure_days", family="tenure",
        intent="Age of the relationship: as_of − signup/origination date.",
        needs=(Need("origination", "effective_date"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={},
        aggregation="tenure", additivity="n/a", explain="H",
        use_cases=("retail_churn", "credit_seasoning", "pricing"),
        pit="origination ≤ as_of. DESIGN-TIME declaration — no data plane enforces it.",
        stage="context",
        notes=(_SUB_ENTITY,
               "Part F grounds signup on 'effective_date'; 'origination_date' is an acceptable alternate."),
    ),
    # F.9 — balance_volatility (Stage 3, instability)
    Template(
        id="balance_volatility", family="balance_stock",
        intent="Coefficient of variation of balance in the window: std(stock)/mean(stock).",
        needs=(Need("stock_col", "monetary_stock"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={"window": (90, 60, 30)},
        aggregation="balance_volatility", additivity="n/a", explain="H",
        use_cases=("retail_churn", "cashflow_risk", "sme_credit"),
        pit=_PIT_TRAILING,
        stage="3-financial-migration",
        eligibility="bind a monetary_stock; single currency.",
        notes=(_SUB_ENTITY,),
    ),
    # F.10 — rfm_composite (baseline workhorse)
    Template(
        id="rfm_composite", family="rfm",
        intent="Classic RFM: percentile-binned blend of recency_days, txn_frequency(window) and "
               "monetary_sum(window). Components stay inspectable.",
        needs=(Need("event_ts", "event_timestamp"), Need("flow_col", "monetary_flow"),
               Need("entity", "customer_id")),
        params={"window": (90, 180, 365)},
        aggregation="rfm", additivity="n/a", explain="H",
        use_cases=("retail_churn", "cross_sell", "segmentation", "clv"),
        pit=_PIT_TRAILING,
        stage="baseline",
        eligibility="single currency for the monetary component.",
        notes=(_SUB_ENTITY,),
    ),
    # F.11 — dd_cancellation_rate (Stage 4, sticky commitments leaving)
    Template(
        id="dd_cancellation_rate", family="unbundling",
        intent="count(DD mandates cancelled in window) / count(DDs active at window start) — utilities / "
               "mortgage direct debits leaving is sticky 'furniture' departing.",
        needs=(Need("dd_event", "transaction_type"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 180, 365)},
        aggregation="dd_cancellation_rate", additivity="non_additive", explain="H",
        use_cases=("retail_churn", "unbundling"),
        pit=_PIT_TRAILING,
        degrade="SKIP if no direct-debit / mandate data.",
        stage="4-unbundling",
        notes=(_SUB_ENTITY,
               "concept sub: DD mandate events use 'transaction_type' (no direct_debit concept in §3) — "
               "skip if absent."),
    ),
    # F.12 — external_own_transfer_trend (Stage 3, primacy loss; §A9 derived intermediate + PII)
    Template(
        id="external_own_transfer_trend", family="primacy_outflow",
        intent="Rising transfers of the customer's OWN money to their accounts at OTHER banks — a "
               "top-tier pre-attrition (primacy-loss) signal.",
        needs=(Need("customer_name", "pii"), Need("beneficiary_name", "pii"),
               Need("beneficiary_bank", "counterparty_id"), Need("flow_col", "monetary_flow"),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (90, 180), "baseline": ("prior_equal_window",),
                "measure": ("amount", "count"), "match_method": ("token", "exact", "fuzzy"),
                "match_threshold": (0.9, 0.8, 0.95)},
        aggregation="own_transfer_trend", additivity="n/a", explain="M",
        use_cases=("retail_churn", "deposit_attrition", "primacy_loss", "wealth_outflow"),
        pit=_PIT_TRAILING,
        degrade="no name to match (or no beneficiary bank) -> degrade to 'external_outflow_growth' (ALL "
                "external outflows; weaker + FLAGGED — noisier).",
        stage="3-financial-migration",
        eligibility="⚠ PII entity-resolution on customer_name + beneficiary_name — consent / purpose / "
                    "residency REQUIRED; read-scoped (needs the pii role). Match is probabilistic "
                    "(false-pos same name, false-neg initials/joint accounts) — explain: M.",
        derived=("is_own_external_transfer := name_match(customer.name, beneficiary_name) ≥ "
                 "{match_threshold} AND beneficiary_bank ≠ home_bank — computed DOWNSTREAM (no data "
                 "plane here); declare method + threshold (§A9/§D.8).",),
        notes=(_SUB_ENTITY,
               "concept sub: names use 'pii'; beneficiary_bank uses 'counterparty_id'."),
    ),
)


def _validate_registry() -> None:
    """Fail fast at import if a template drifts from the concept registry / schema invariants."""
    seen_ids: set[str] = set()
    for t in RETAIL_CHURN_TEMPLATES:
        if t.id in seen_ids:
            raise ValueError(f"duplicate template id {t.id!r} in RETAIL_CHURN_TEMPLATES")
        seen_ids.add(t.id)
        for need in t.needs:
            if need.concept not in CONCEPT_REGISTRY:
                raise ValueError(
                    f"template {t.id!r} need {need.role!r} references unknown concept {need.concept!r}")
        for key, allowed in t.params.items():
            if not isinstance(allowed, tuple) or not allowed:
                raise ValueError(f"template {t.id!r} param {key!r} must be a non-empty tuple")


_validate_registry()
