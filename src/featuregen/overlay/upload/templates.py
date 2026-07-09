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
3. The recipe FAMILIES, authored faithfully from the SME library
   (``docs/superpowers/specs/2026-07-08-banking-feature-template-library.md``): :data:`RETAIL_CHURN_TEMPLATES`
   (the 12 pilot recipes, §PART F) and :data:`CREDIT_RISK_TEMPLATES` (the §B2 deterioration→default funnel,
   §PART G). :data:`ALL_TEMPLATES` is the combined registry future passes extend; grounding is the router,
   so a family surfaces only where its distinctive concepts exist in the catalog.

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
# closest registry concept is used and NOTED on the template). NOTE: the Phase-1 gap fix added dedicated
# concepts for direct_debit, debit_credit_indicator, beneficiary_bank and beneficiary_name, so those
# roles now ground on their OWN concepts; the remaining substitutions are:
#   • entity {customer}         -> customer_id      (Part F table says "customer_identifier"; §3 canonical)
#   • salary tag                -> category_code    (Part F: transactions.type; optional -> degrade)
#   • product_holding           -> product_type     (no "product_holding" concept)
#   • customer_name             -> pii              (a name is PII; read-scoped + consent-gated)
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
        needs=(Need("flow_col", "monetary_flow"),
               Need("direction", "debit_credit_indicator", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (90, 30, 60, 180), "measure": ("ratio", "net")},
        aggregation="inflow_outflow", additivity="non_additive", explain="H",
        use_cases=("retail_churn", "sme_credit", "cashflow"),
        pit=_PIT_TRAILING,
        degrade="no dr/cr indicator -> infer direction from the amount sign (declared derivation, §D.8).",
        stage="3-financial-migration",
        eligibility="single currency — convert to base first.",
        derived=("is_debit := amount_sign(amount) < 0 — declared downstream when no dr/cr column exists.",),
        notes=(_SUB_ENTITY,
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
        needs=(Need("dd_event", "direct_debit"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 180, 365)},
        aggregation="dd_cancellation_rate", additivity="non_additive", explain="H",
        use_cases=("retail_churn", "unbundling"),
        pit=_PIT_TRAILING,
        degrade="SKIP if no direct-debit / mandate data.",
        stage="4-unbundling",
        notes=(_SUB_ENTITY,),
    ),
    # F.12 — external_own_transfer_trend (Stage 3, primacy loss; §A9 derived intermediate + PII)
    Template(
        id="external_own_transfer_trend", family="primacy_outflow",
        intent="Rising transfers of the customer's OWN money to their accounts at OTHER banks — a "
               "top-tier pre-attrition (primacy-loss) signal.",
        needs=(Need("customer_name", "pii"), Need("beneficiary_name", "beneficiary_name"),
               Need("beneficiary_bank", "beneficiary_bank"), Need("flow_col", "monetary_flow"),
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
               "customer_name uses the generic 'pii' concept (no dedicated customer-name concept); "
               "beneficiary_name / beneficiary_bank now ground on their own concepts."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The credit_risk templates — the §B2 DETERIORATION → DEFAULT funnel authored to Part-F depth.
#
# Funnel (§B2): HEALTHY → EARLY STRESS → EMERGING DISTRESS → DELINQUENCY → DEFAULT ⚠ → RECOVERY/LOSS,
# mapped onto IFRS9 staging (Stage 1 performing → 2 SICR → 3 credit-impaired). Two authoring disciplines
# are load-bearing here:
#   • ROUTING — every recipe REQUIRES at least one credit-distinctive concept (limit / ead / dpd /
#     delinquency_bucket / ecl / impairment_stage / collateral_value / bureau_* / trade_line /
#     restructured_flag / sicr_flag / covenant / scheduled_amount), so grounding surfaces the family ONLY
#     where the catalog actually carries credit signals — a churn/deposit catalog yields NOTHING here.
#   • NEAR-LABEL — a recipe that binds a near-label concept (delinquency_bucket ≈ 90+ DPD, impairment_stage
#     stage-3 = credit-impaired, restructured_flag/sicr_flag, or a DPD level / covenant breach that borders
#     the default event) sets near_label=True + a ⚠ eligibility note; the deterioration must be observed
#     STRICTLY pre-default and the 3-part leakage control must FLAG it (there is no data plane to enforce
#     it — see the module docstring). Fair-lending: no recipe binds a protected_attribute (engine-enforced).
#
# The Part-G appendix in docs/…/2026-07-08-banking-feature-template-library.md is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_CREDIT_PIT_STATE = ("point-in-time credit STATE observed as-of: value knowable strictly ≤ as_of; the "
                     "latest snapshot within (as_of − {window}, as_of], never forward. DESIGN-TIME "
                     "declaration — no data plane enforces runtime PIT.")
_NEAR_LABEL_PREFIX = ("⚠ NEAR-LABEL: observe the deterioration signal STRICTLY pre-default (never on/after "
                      "the default event; window ≠ the label window); the 3-part leakage control must "
                      "FLAG it. ")
_EXTERNAL_FCRA = ("EXTERNAL credit-bureau data — FCRA/GDPR regime, heavily lagged/restated (honour "
                  "system_time to avoid restated-data leakage); provenance=external.")
_FAIR_LENDING = "fair-lending: NEVER bind a protected_attribute (engine-enforced); income/geo flagged."

CREDIT_RISK_TEMPLATES: tuple[Template, ...] = (
    # ── Utilisation & exposure (§B2 Stage 0-1 — early stress) ──────────────────────────────────────
    # C.1 — credit_utilisation (level + trend)
    Template(
        id="credit_utilisation", family="utilisation_exposure",
        intent="Credit-limit utilisation — drawn exposure / limit (measure=level) or its trailing OLS "
               "trend (measure=trend); rising utilisation is the classic early-deterioration signal.",
        needs=(Need("limit_col", "limit"), Need("drawn_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "facility_id")),
        params={"window": (90, 60, 30), "measure": ("level", "trend")},
        aggregation="utilisation", additivity="non_additive", explain="H",
        use_cases=("credit_risk", "early_warning", "limit_management"),
        pit=_CREDIT_PIT_STATE,
        degrade="no limit (e.g. a term loan with no ceiling) -> SKIP; use exposure_trend instead.",
        stage="1-early-stress",
        eligibility="bind a monetary_stock drawn balance (never a flow) against its limit; single "
                    "currency — convert to base first. " + _FAIR_LENDING,
        notes=("anchor: 'limit' (credit-distinctive) routes this off a deposit/churn catalog.",
               "OUTPUT additivity is measure-dependent: measure=level is a non-additive ratio, "
               "measure=trend is n/a (a slope) — the default carries the ratio case.",
               "'ead' is an acceptable alternate for the drawn amount where exposure-at-default is "
               "reported instead of a ledger balance."),
    ),
    # C.2 — exposure_trend (raw EAD drift, limit-free)
    Template(
        id="exposure_trend", family="utilisation_exposure",
        intent="OLS slope of exposure-at-default over a trailing window — rising absolute exposure into "
               "stress, independent of any limit (covers term loans + drawing on committed lines).",
        needs=(Need("exposure_col", "ead"), Need("asof", "as_of_date"),
               Need("entity", "facility_id")),
        params={"window": (180, 90, 365), "measure": ("normalized", "slope")},
        aggregation="exposure_trend", additivity="n/a", explain="H",
        use_cases=("credit_risk", "early_warning", "limit_management"),
        pit=_PIT_TRAILING,
        degrade="only a single exposure snapshot (no history) -> SKIP (no trend from one point).",
        stage="1-early-stress",
        eligibility="bind an 'ead' exposure STOCK (semi-additive) — never sum across dates; single "
                    "currency. " + _FAIR_LENDING,
        notes=("anchor: 'ead' (credit-distinctive) routes this off a churn catalog.",
               "'contingent_exposure' (undrawn commitment) is an acceptable alternate where a facility "
               "reports the off-balance-sheet line separately."),
    ),
    # ── Arrears / DPD dynamics (§B2 Stage 3 — delinquency; NEAR-LABEL) ──────────────────────────────
    # C.3 — days_past_due_max
    Template(
        id="days_past_due_max", family="arrears_dpd",
        intent="Worst days-past-due reached in the trailing window: max(dpd) — the headline arrears-"
               "severity signal.",
        needs=(Need("dpd_col", "dpd"), Need("asof", "as_of_date"), Need("entity", "facility_id")),
        params={"window": (90, 60, 30), "measure": ("max", "latest")},
        aggregation="dpd_max", additivity="n/a", explain="H",
        use_cases=("credit_risk", "collections", "early_warning"),
        pit=_CREDIT_PIT_STATE,
        stage="3-delinquency",
        near_label=True,
        eligibility=_NEAR_LABEL_PREFIX + "a max DPD approaching 90+ IS the Basel default backstop. "
                    + _FAIR_LENDING,
        notes=("anchor: 'dpd' (credit-distinctive) routes this off a churn catalog.",
               "borders the default label — the deterioration must be observed strictly pre-default."),
    ),
    # C.4 — delinquency_bucket_dynamics (worst bucket + roll-rate)
    Template(
        id="delinquency_bucket_dynamics", family="arrears_dpd",
        intent="Delinquency-bucket dynamics: worst (highest) bucket reached in the window "
               "(measure=worst_bucket) or forward roll — did the bucket migrate WORSE vs window start "
               "(measure=roll_rate).",
        needs=(Need("bucket_col", "delinquency_bucket"), Need("asof", "as_of_date"),
               Need("entity", "facility_id")),
        params={"window": (90, 60, 30), "measure": ("worst_bucket", "roll_rate")},
        aggregation="delinquency_bucket", additivity="n/a", explain="H",
        use_cases=("credit_risk", "collections", "ifrs9_staging"),
        pit=_CREDIT_PIT_STATE,
        stage="3-delinquency",
        near_label=True,
        eligibility=_NEAR_LABEL_PREFIX + "the 90+ bucket is a default backstop. " + _FAIR_LENDING,
        notes=("anchor: 'delinquency_bucket' (near-label, credit-distinctive).",
               "OUTPUT additivity is measure-dependent: worst_bucket is an ordinal max (n/a); a "
               "portfolio roll_rate is non-additive (a proportion) — the default carries the ordinal case.",
               "borders the default label — observe strictly pre-default."),
    ),
    # ── Repayment behaviour (§B2 Stage 2 — emerging distress) ───────────────────────────────────────
    # C.5 — payment_ratio
    Template(
        id="payment_ratio", family="repayment_behaviour",
        intent="Repayment coverage — sum(repayment flow in window) / drawn balance (measure=to_balance) "
               "or / limit (measure=to_limit); a falling ratio is emerging distress.",
        needs=(Need("payment_col", "monetary_flow"), Need("balance_col", "monetary_stock"),
               Need("limit_col", "limit"), Need("event_ts", "event_timestamp"),
               Need("entity", "facility_id")),
        params={"window": (90, 60, 180), "measure": ("to_balance", "to_limit")},
        aggregation="payment_ratio", additivity="non_additive", explain="H",
        use_cases=("credit_risk", "early_warning", "affordability"),
        pit=_PIT_TRAILING,
        degrade="no limit (a term loan) -> SKIP; use missed_partial_payment_count on the schedule.",
        stage="2-emerging-distress",
        eligibility="single currency — convert to base first. " + _FAIR_LENDING,
        notes=("anchor: 'limit' (credit-distinctive) routes this off a churn catalog.",
               "a ratio — non-additive; compute per facility, never sum."),
    ),
    # C.6 — min_payment_only_streak
    Template(
        id="min_payment_only_streak", family="repayment_behaviour",
        intent="Consecutive billing periods paying only ~the contractual minimum (min ≈ a small % of "
               "limit/balance) — a persistent revolver-in-distress signal.",
        needs=(Need("payment_col", "monetary_flow"), Need("limit_col", "limit"),
               Need("event_ts", "event_timestamp"), Need("entity", "facility_id")),
        params={"window": (180, 90, 365), "min_pct": (3, 5, 2)},
        aggregation="min_only_streak", additivity="additive", explain="H",
        use_cases=("credit_risk", "early_warning"),
        pit=_PIT_TRAILING,
        degrade="no per-period statement minimum -> derive min ≈ {min_pct}% of the period balance "
                "(declared downstream derivation §D.8; probabilistic — FLAG).",
        stage="2-emerging-distress",
        eligibility="single currency. " + _FAIR_LENDING,
        derived=("is_min_only := period_payment ≤ min_due(≈{min_pct}% of balance/limit) — declared "
                 "downstream (no data plane); the streak counts consecutive is_min_only periods.",),
        notes=("anchor: 'limit' (credit-distinctive) routes this off a churn catalog.",
               "a count of periods — additive."),
    ),
    # C.7 — missed_partial_payment_count
    Template(
        id="missed_partial_payment_count", family="repayment_behaviour",
        intent="Count of scheduled installments in the window where the amount PAID fell short of the "
               "amount DUE (missed or partial) — arrears = scheduled − paid.",
        needs=(Need("scheduled_col", "scheduled_amount"), Need("paid_col", "monetary_flow"),
               Need("event_ts", "event_timestamp"), Need("entity", "facility_id")),
        params={"window": (180, 90, 365), "tolerance_pct": (5, 0, 10)},
        aggregation="missed_partial_count", additivity="additive", explain="H",
        use_cases=("credit_risk", "early_warning", "collections"),
        pit=_PIT_TRAILING,
        degrade="no contractual installment schedule (revolving product) -> SKIP; use payment_ratio.",
        stage="2-emerging-distress",
        eligibility="single currency. " + _FAIR_LENDING,
        derived=("is_short := paid < scheduled × (1 − {tolerance_pct}%) — a shortfall vs the "
                 "contractual due, counted per installment date.",),
        notes=("anchor: 'scheduled_amount' — the contractual installment DUE, an installment-lending "
               "concept absent from a deposit/churn catalog, routes this off a churn catalog (it is not "
               "on the §B2 credit-distinctive list but is lending-specific by construction).",
               "a count of shortfall periods — additive."),
    ),
    # ── Exposure & provisioning drift (§B2 Stage 2 — emerging distress; staging is NEAR-LABEL) ──────
    # C.8 — ecl_provision_trend
    Template(
        id="ecl_provision_trend", family="exposure_provisioning",
        intent="Trend in the IFRS9 expected-credit-loss provision over a trailing window — rising ECL is "
               "provisioning drift into distress.",
        needs=(Need("ecl_col", "ecl"), Need("asof", "as_of_date"), Need("entity", "facility_id")),
        params={"window": (180, 90, 365), "measure": ("slope", "pct_change")},
        aggregation="ecl_trend", additivity="n/a", explain="H",
        use_cases=("credit_risk", "ifrs9_staging"),
        pit=_PIT_TRAILING,
        degrade="only a single ECL snapshot (no history) -> SKIP.",
        stage="2-emerging-distress",
        eligibility="bind an 'ecl' provision STOCK (semi-additive) — never sum across dates; single "
                    "currency. " + _FAIR_LENDING,
        notes=("anchor: 'ecl' (credit-distinctive) routes this off a churn catalog.",
               "'provision_amount' is an acceptable alternate for banks reporting a loan-loss provision "
               "rather than an IFRS9 ECL."),
    ),
    # C.9 — stage_migration (IFRS9 impairment stage worsening — NEAR-LABEL)
    Template(
        id="stage_migration", family="exposure_provisioning",
        intent="IFRS9 impairment-stage migration: is the stage at as_of WORSE (higher) than at window "
               "start? measure=worsened_flag / stage_delta.",
        needs=(Need("stage_col", "impairment_stage"), Need("asof", "as_of_date"),
               Need("entity", "facility_id")),
        params={"window": (180, 90, 365), "measure": ("worsened_flag", "stage_delta")},
        aggregation="stage_migration", additivity="n/a", explain="H",
        use_cases=("credit_risk", "ifrs9_staging", "early_warning"),
        pit=_CREDIT_PIT_STATE,
        stage="2-emerging-distress",
        near_label=True,
        eligibility=_NEAR_LABEL_PREFIX + "IFRS9 stage 3 is credit-impaired ≈ the default label. "
                    + _FAIR_LENDING,
        notes=("anchor: 'impairment_stage' (near-label, credit-distinctive).",
               "borders the default label — observe strictly pre-default."),
    ),
    # ── Collateral (§B2 Stage 1 — early stress; non-additive ratio) ─────────────────────────────────
    # C.10 — loan_to_value (LTV / coverage / shortfall)
    Template(
        id="loan_to_value", family="collateral",
        intent="Loan-to-value / collateral coverage — outstanding exposure / collateral_value "
               "(measure=ltv), its inverse (measure=coverage), or the uncovered shortfall "
               "max(exposure − collateral, 0) (measure=shortfall).",
        needs=(Need("exposure_col", "monetary_stock"), Need("collateral_col", "collateral_value"),
               Need("asof", "as_of_date"), Need("entity", "facility_id")),
        params={"window": (90, 180, 365), "measure": ("ltv", "coverage", "shortfall")},
        aggregation="loan_to_value", additivity="non_additive", explain="H",
        use_cases=("credit_risk", "limit_management", "ifrs9_staging"),
        pit=_CREDIT_PIT_STATE,
        degrade="no collateral (unsecured) -> SKIP (LTV undefined).",
        stage="1-early-stress",
        eligibility="apply the collateral haircut / advance_rate before the ratio; single currency. "
                    + _FAIR_LENDING,
        notes=("anchor: 'collateral_value' (credit-distinctive) routes this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: ltv/coverage are non-additive ratios; a "
               "shortfall is a monetary amount — the default carries the ratio case.",
               "'ead' is an acceptable alternate for the exposure numerator."),
    ),
    # ── Bureau / external (§B2 Stage 2 — emerging distress; FCRA external, provenance-flagged) ───────
    # C.11 — bureau_score_delta
    Template(
        id="bureau_score_delta", family="bureau_external",
        intent="Change in the external credit-bureau score over a trailing window — a falling bureau "
               "score is cross-lender deterioration.",
        needs=(Need("score_col", "bureau_score"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={"window": (90, 180, 365), "measure": ("delta", "slope")},
        aggregation="bureau_score_delta", additivity="n/a", explain="H",
        use_cases=("credit_risk", "early_warning"),
        pit=_CREDIT_PIT_STATE,
        degrade="only a single bureau pull (no history) -> SKIP.",
        stage="2-emerging-distress",
        eligibility=_EXTERNAL_FCRA + " a bureau score is a MODEL OUTPUT — leakage-risk when its target "
                    "overlaps the feature target; flag before use. " + _FAIR_LENDING,
        notes=("anchor: 'bureau_score' (external, credit-distinctive) routes this off a churn catalog.",
               "provenance=external (FCRA)."),
    ),
    # C.12 — bureau_inquiry_velocity
    Template(
        id="bureau_inquiry_velocity", family="bureau_external",
        intent="Count of HARD credit-bureau inquiries in the window — rising inquiry velocity is credit-"
               "hungry behaviour (shopping for credit under stress).",
        needs=(Need("inquiry_col", "bureau_inquiry"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 180, 30), "inquiry_kind": ("hard", "all")},
        aggregation="bureau_inquiry_velocity", additivity="additive", explain="H",
        use_cases=("credit_risk", "early_warning"),
        pit=_PIT_TRAILING,
        stage="2-emerging-distress",
        eligibility=_EXTERNAL_FCRA + " " + _FAIR_LENDING,
        notes=("anchor: 'bureau_inquiry' (external, credit-distinctive) routes this off a churn catalog.",
               "a count of inquiry events — additive; provenance=external (FCRA)."),
    ),
    # C.13 — new_trade_line_count
    Template(
        id="new_trade_line_count", family="bureau_external",
        intent="Count of NEW credit-bureau tradelines opened in the window — rising external leverage "
               "(new borrowing elsewhere) ahead of distress.",
        needs=(Need("tradeline_col", "trade_line"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (180, 90, 365)},
        aggregation="new_tradeline_count", additivity="additive", explain="H",
        use_cases=("credit_risk", "early_warning"),
        pit=_PIT_TRAILING,
        stage="2-emerging-distress",
        eligibility=_EXTERNAL_FCRA + " " + _FAIR_LENDING,
        notes=("anchor: 'trade_line' (external, credit-distinctive) routes this off a churn catalog.",
               "a count of newly-opened tradelines — additive; provenance=external (FCRA)."),
    ),
    # ── Forbearance / SICR (§B2 Stage 2-4 — NEAR-LABEL staging triggers) ────────────────────────────
    # C.14 — forbearance_in_window
    Template(
        id="forbearance_in_window", family="forbearance_sicr",
        intent="Did a forbearance / restructure event occur in the window? measure=occurred_flag / count "
               "— concessions granted are a strong pre-default distress marker.",
        needs=(Need("restructured_col", "restructured_flag"), Need("asof", "as_of_date"),
               Need("entity", "facility_id")),
        params={"window": (365, 180, 90), "measure": ("occurred_flag", "count")},
        aggregation="forbearance", additivity="n/a", explain="H",
        use_cases=("credit_risk", "ifrs9_staging", "early_warning"),
        pit=_CREDIT_PIT_STATE,
        stage="4-default-adjacent",
        near_label=True,
        eligibility=_NEAR_LABEL_PREFIX + "forbearance ≈ the default/impaired label (IFRS9 Stage-3 "
                    "trigger). " + _FAIR_LENDING,
        notes=("anchor: 'restructured_flag' (near-label, credit-distinctive).",
               "OUTPUT additivity is measure-dependent: occurred_flag is n/a; a count is additive.",
               "borders the default label — observe strictly pre-default."),
    ),
    # C.15 — sicr_onset
    Template(
        id="sicr_onset", family="forbearance_sicr",
        intent="Did an IFRS9 Significant-Increase-in-Credit-Risk (SICR) trigger fire in the window "
               "(Stage 1 -> 2)? The staging-onset marker.",
        needs=(Need("sicr_col", "sicr_flag"), Need("asof", "as_of_date"),
               Need("entity", "facility_id")),
        params={"window": (180, 90, 365)},
        aggregation="sicr_onset", additivity="n/a", explain="H",
        use_cases=("credit_risk", "ifrs9_staging"),
        pit=_CREDIT_PIT_STATE,
        stage="2-emerging-distress",
        near_label=True,
        eligibility=_NEAR_LABEL_PREFIX + "the SICR trigger borders the default funnel. " + _FAIR_LENDING,
        notes=("anchor: 'sicr_flag' (near-label, credit-distinctive).",
               "borders the default label — observe strictly pre-default."),
    ),
    # ── Affordability (§B2 covenant / DSCR — NEAR-LABEL breach path) ────────────────────────────────
    # C.16 — dscr_covenant_headroom
    Template(
        id="dscr_covenant_headroom", family="affordability",
        intent="Debt-service / covenant headroom — the margin between a covenant's actual and its "
               "threshold (DSCR/ICR/leverage); a shrinking or negative headroom is a breach path.",
        needs=(Need("covenant_col", "covenant"), Need("asof", "as_of_date"),
               Need("entity", "facility_id")),
        params={"window": (90, 180, 365), "measure": ("headroom", "breached_flag", "trend")},
        aggregation="covenant_headroom", additivity="non_additive", explain="H",
        use_cases=("credit_risk", "affordability", "early_warning"),
        pit=_CREDIT_PIT_STATE,
        stage="2-emerging-distress",
        near_label=True,
        eligibility=_NEAR_LABEL_PREFIX + "a covenant breach borders the default/forbearance label; "
                    "income/affordability inputs are SENSITIVE. " + _FAIR_LENDING,
        notes=("anchor: 'covenant' (near-label, credit-distinctive) — DSCR/ICR/leverage headroom.",
               "OUTPUT additivity is measure-dependent: headroom/DSCR is a non-additive ratio; "
               "breached_flag is n/a — the default carries the ratio case.",
               "borders the default label — observe strictly pre-default."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The fraud templates — the §B3 KILL-CHAIN authored to Part-F depth (Phase-3 Pass-2).
#
# Kill-chain (§B3): RECON → ACCESS/TAKEOVER → SETUP/STAGING → CASH-OUT ⚠. Fraud is REAL-TIME: windows are
# MINUTES/HOURS (a ``window_min`` param, NEVER a trailing-days ``window`` — the ``_{window}d`` naming
# convention would mis-label minutes as days), computed on the live PRE-transaction state. Two authoring
# disciplines are load-bearing (mirroring credit_risk):
#   • ROUTING — every recipe REQUIRES at least one crime-distinctive, NON-STRUCTURAL concept (a categorical
#     signal like payment_rail / corridor / mcc, or a pii behavioural like device_fingerprint / geolocation
#     — NOT an entity/as_of concept, which the engine's structural is_grain/is_as_of scoring would bind onto
#     ANY grain/as-of column, cross-surfacing the family). Grounding is the router: the family surfaces ONLY
#     where the catalog carries these fraud signals; a churn catalog with only generic monetary_flow +
#     event_timestamp + customer_id grounds NOTHING here (the locked invariant asserted by the credit +
#     crime routing tests: ALL_TEMPLATES on the churn _CATALOG = EXACTLY the churn lens).
#   • LEAKAGE — a monitoring feature is built from the BEHAVIOUR (velocity, geo-impossibility, structuring),
#     NEVER from the alert outcome. No recipe Needs the fraud_flag leakage anchor (the engine refuses it by
#     construction). Fair-lending: no recipe binds a protected_attribute (engine-enforced); corridor /
#     country_code are national-origin PROXIES — flagged.
# The Part-H appendix in docs/…/2026-07-08-banking-feature-template-library.md is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_FRAUD_PIT_REALTIME = (
    "real-time trailing window (t − {window_min}min, t] computed on the live PRE-transaction state — "
    "values knowable STRICTLY before the authorization/decision point, never after. DESIGN-TIME "
    "declaration: there is NO data plane; a batch trailing-window model cannot honour real-time "
    "settlement-finality timing (§B3 — fraud is real-time).")
_PII_ONLINE_ELIGIBILITY = (
    "⚠ PII: an online identifier (device_fingerprint / precise geolocation) is GDPR personal data — "
    "read-scoped (needs the pii role); consent / purpose / residency REQUIRED.")
_CORRIDOR_PROXY = ("⚠ corridor / country_code are national-origin PROXIES (fair-lending) — proxy-flagged; "
                   "AML-permitted but bias-watched, NEVER a credit input.")
_FRAUD_BEHAVIOUR = "built from transaction BEHAVIOUR, never the fraud outcome (fraud_flag is a leakage anchor)."

FRAUD_TEMPLATES: tuple[Template, ...] = (
    # ── RECON / targeting (§B3 Stage 1) ─────────────────────────────────────────────────────────────
    # F.1 — card_testing_velocity (validating stolen card numbers)
    Template(
        id="card_testing_velocity", family="recon_targeting",
        intent="Card-testing — a burst of many small-value authorizations on one card in a short window "
               "(a fraudster validating stolen card numbers before the real cash-out).",
        needs=(Need("rail", "payment_rail"), Need("card", "card_id"),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp")),
        params={"window_min": (60, 15, 1440), "amount_pctile": (10, 5, 25)},
        aggregation="card_testing_count", additivity="additive", explain="H",
        use_cases=("fraud", "card_fraud", "transaction_monitoring", "financial_crime"),
        pit=_FRAUD_PIT_REALTIME,
        degrade="no card rail / card grain -> SKIP (this is a card-present/CNP pattern).",
        stage="1-recon",
        eligibility=_FRAUD_BEHAVIOUR,
        derived=("is_small := amount ≤ {amount_pctile}th pctile of the card's own auth history — computed "
                 "DOWNSTREAM (no data plane).",),
        notes=("anchor: 'payment_rail' (crime-distinctive, non-structural) routes this off a churn catalog.",
               "a count of small auths — additive."),
    ),
    # F.2 — device_sharing_velocity (synthetic-ID / credential-stuffing ring)
    Template(
        id="device_sharing_velocity", family="recon_targeting",
        intent="Synthetic-ID / credential-stuffing recon — one device_fingerprint transacting across an "
               "abnormal number of distinct customers/accounts in a window (a shared-device ring).",
        needs=(Need("device", "device_fingerprint"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window_min": (1440, 60, 10080)},
        aggregation="device_sharing_velocity", additivity="non_additive", explain="M",
        use_cases=("fraud", "account_takeover", "synthetic_id", "transaction_monitoring", "financial_crime"),
        pit=_FRAUD_PIT_REALTIME,
        degrade="no device_fingerprint -> SKIP.",
        stage="1-recon",
        eligibility=_PII_ONLINE_ELIGIBILITY + " " + _FRAUD_BEHAVIOUR,
        notes=("anchor: 'device_fingerprint' (crime-distinctive, pii, non-structural) routes this off a "
               "churn catalog and needs the pii role.",
               "a distinct-account-per-device velocity — non-additive; compute per device, never sum."),
    ),
    # ── ACCESS / TAKEOVER (§B3 Stage 2) ─────────────────────────────────────────────────────────────
    # F.3 — new_device_flag (novel device for this entity)
    Template(
        id="new_device_flag", family="access_takeover",
        intent="Novel-device / device-change — this entity is transacting from a device_fingerprint not "
               "seen in its trailing history (an account-takeover access marker).",
        needs=(Need("device", "device_fingerprint"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window_min": (43200, 10080, 129600)},
        aggregation="new_device_flag", additivity="n/a", explain="H",
        use_cases=("fraud", "account_takeover", "transaction_monitoring", "financial_crime"),
        pit=_FRAUD_PIT_REALTIME,
        degrade="no device_fingerprint -> SKIP.",
        stage="2-access-takeover",
        eligibility=_PII_ONLINE_ELIGIBILITY + " " + _FRAUD_BEHAVIOUR,
        notes=("anchor: 'device_fingerprint' (crime-distinctive, pii, non-structural).",
               "first-seen device for this entity — a flag; n/a."),
    ),
    # F.4 — geo_velocity_impossible (impossible travel)
    Template(
        id="geo_velocity_impossible", family="access_takeover",
        intent="Impossible travel — two transactions whose geolocations are farther apart than any "
               "physical travel could cover in the elapsed time (a classic account-takeover signal).",
        needs=(Need("geo", "geolocation"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window_min": (720, 60, 1440), "measure": ("impossible_flag", "max_implied_kmh")},
        aggregation="geo_velocity", additivity="n/a", explain="M",
        use_cases=("fraud", "account_takeover", "transaction_monitoring", "financial_crime"),
        pit=_FRAUD_PIT_REALTIME,
        degrade="no geolocation -> SKIP; a coarse country-hop is a weaker fallback (FLAG).",
        stage="2-access-takeover",
        eligibility=_PII_ONLINE_ELIGIBILITY + " " + _FRAUD_BEHAVIOUR,
        derived=("implied_kmh := haversine(geo_i, geo_j) / Δt between consecutive txns — computed "
                 "DOWNSTREAM (no data plane); impossible_flag := implied_kmh > a plausible_max.",),
        notes=("anchor: 'geolocation' (crime-distinctive, pii, non-structural) routes this off a churn "
               "catalog and needs the pii role.",
               "a flag / max implied speed — n/a (not summable)."),
    ),
    # ── SETUP / STAGING (§B3 Stage 3) ───────────────────────────────────────────────────────────────
    # F.5 — first_time_payee_high_value (mule-account staging)
    Template(
        id="first_time_payee_high_value", family="setup_staging",
        intent="A high-value payment to a FIRST-TIME payee (a beneficiary_bank not previously paid) — the "
               "mule-account staging move (payee added, then drained).",
        needs=(Need("rail", "payment_rail"), Need("beneficiary_bank", "beneficiary_bank"),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window_min": (1440, 60, 10080), "amount_pctile": (95, 90, 99)},
        aggregation="first_time_payee_high_value", additivity="n/a", explain="H",
        use_cases=("fraud", "app_scam", "authorised_push_payment", "transaction_monitoring",
                   "financial_crime"),
        pit=_FRAUD_PIT_REALTIME,
        degrade="no payment rail -> SKIP; without beneficiary history the 'first-time' test degrades to "
                "'high-value payment' only (weaker + FLAGGED).",
        stage="3-setup-staging",
        eligibility=_FRAUD_BEHAVIOUR,
        derived=("is_first_time_payee := beneficiary_bank not in the entity's prior-paid set — computed "
                 "DOWNSTREAM (no data plane).",),
        notes=("anchor: 'payment_rail' (crime-distinctive, non-structural) routes this off a churn catalog "
               "(beneficiary_bank ALSO exists on a churn catalog, so it cannot be the sole anchor).",
               "high-value ≈ above the {amount_pctile}th pctile of the entity's own history; a flag — n/a."),
    ),
    # F.6 — merchant_risk_anomaly (high-risk / novel MCC)
    Template(
        id="merchant_risk_anomaly", family="setup_staging",
        intent="Spending anomaly at a high-risk / novel merchant category — an off-pattern MCC or a "
               "first-seen merchant for this entity (card-fraud staging / testing the waters).",
        needs=(Need("mcc", "mcc"), Need("merchant", "merchant_id"),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp")),
        params={"window_min": (1440, 60, 10080), "measure": ("high_risk_mcc_share", "novel_merchant_flag")},
        aggregation="merchant_risk_anomaly", additivity="non_additive", explain="M",
        use_cases=("fraud", "card_fraud", "transaction_monitoring", "financial_crime"),
        pit=_FRAUD_PIT_REALTIME,
        degrade="no mcc -> SKIP; a merchant_id-only novelty flag is a weaker fallback.",
        stage="3-setup-staging",
        eligibility=_FRAUD_BEHAVIOUR,
        notes=("anchor: 'mcc' (crime-distinctive, non-structural) routes this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: high_risk_mcc_share is a non-additive ratio; "
               "novel_merchant_flag is n/a — the default carries the ratio case."),
    ),
    # ── CASH-OUT (§B3 Stage 4) — built from behaviour, NOT the fraud outcome ─────────────────────────
    # F.7 — txn_velocity_spike (the cash-out ramp)
    Template(
        id="txn_velocity_spike", family="cash_out",
        intent="Transaction-velocity spike — count (or amount) of transactions in a short window vs the "
               "entity's own trailing baseline; a sudden ramp is the cash-out signature.",
        needs=(Need("rail", "payment_rail"), Need("card", "card_id"),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp")),
        params={"window_min": (60, 15, 1440), "baseline": ("prior_equal_window", "own_history"),
                "measure": ("count_ratio", "amount_ratio")},
        aggregation="txn_velocity_spike", additivity="non_additive", explain="H",
        use_cases=("fraud", "card_fraud", "account_takeover", "transaction_monitoring", "financial_crime"),
        pit=_FRAUD_PIT_REALTIME,
        degrade="no card rail / card grain -> anchor on customer_id + payment_rail (account-level velocity).",
        stage="4-cash-out",
        eligibility=_FRAUD_BEHAVIOUR,
        notes=("anchor: 'payment_rail' (crime-distinctive, non-structural) routes this off a churn catalog.",
               "a velocity RATIO (recent vs baseline) — non-additive; compute per entity, never sum."),
    ),
    # F.8 — amount_zscore_spike (out-of-pattern high-value drain)
    Template(
        id="amount_zscore_spike", family="cash_out",
        intent="Amount anomaly — z-score of a transaction amount vs the entity's own trailing mean/std; a "
               "large positive z is an out-of-pattern high-value drain.",
        needs=(Need("rail", "payment_rail"), Need("card", "card_id"),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp")),
        params={"window_min": (43200, 10080, 129600)},
        aggregation="amount_zscore", additivity="n/a", explain="M",
        use_cases=("fraud", "card_fraud", "transaction_monitoring", "financial_crime"),
        pit=_FRAUD_PIT_REALTIME,
        degrade="no card rail / card grain -> compute at customer_id grain.",
        stage="4-cash-out",
        eligibility=_FRAUD_BEHAVIOUR,
        derived=("amount_z := (amount − rolling_mean) / rolling_std over the entity's own history — "
                 "computed DOWNSTREAM (no data plane).",),
        notes=("anchor: 'payment_rail' (crime-distinctive, non-structural) routes this off a churn catalog.",
               "a z-score — n/a (not summable)."),
    ),
    # F.9 — cross_channel_rail_anomaly (first use of an unusual rail)
    Template(
        id="cross_channel_rail_anomaly", family="cash_out",
        intent="Cross-channel / cross-rail anomaly — the entity suddenly using a payment_rail (or scheme) "
               "it never uses (e.g. first CHAPS/wire after only card spend) at cash-out.",
        needs=(Need("rail", "payment_rail"), Need("scheme", "scheme", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window_min": (1440, 60, 10080)},
        aggregation="cross_rail_anomaly", additivity="n/a", explain="H",
        use_cases=("fraud", "account_takeover", "transaction_monitoring", "financial_crime"),
        pit=_FRAUD_PIT_REALTIME,
        degrade="no payment rail -> SKIP.",
        stage="4-cash-out",
        eligibility=_FRAUD_BEHAVIOUR,
        notes=("anchor: 'payment_rail' (crime-distinctive, non-structural) routes this off a churn catalog.",
               "a first-seen-rail flag — n/a."),
    ),
    # F.10 — cross_border_burst (rapid offshore movement)
    Template(
        id="cross_border_burst", family="cash_out",
        intent="Cross-border burst — a short-window count of payments into new/high-risk corridors "
               "(rapid offshore movement of the drained funds).",
        needs=(Need("corridor", "corridor"), Need("country", "country_code", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window_min": (1440, 60, 10080)},
        aggregation="cross_border_burst", additivity="additive", explain="H",
        use_cases=("fraud", "aml", "transaction_monitoring", "financial_crime"),
        pit=_FRAUD_PIT_REALTIME,
        degrade="no corridor -> SKIP.",
        stage="4-cash-out",
        eligibility=_CORRIDOR_PROXY + " " + _FRAUD_BEHAVIOUR,
        notes=("anchor: 'corridor' (crime-distinctive, non-structural) routes this off a churn catalog.",
               "a count of cross-border txns in the burst window — additive."),
    ),
    # F.11 — amount_just_under_limit (structuring at authorization)
    Template(
        id="amount_just_under_limit", family="cash_out",
        intent="Just-under-limit structuring at authorization — the share of payments sitting just below a "
               "reporting / step-up / SCA threshold on a rail (deliberately dodging a control).",
        needs=(Need("rail", "payment_rail"), Need("flow_col", "monetary_flow"),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window_min": (10080, 1440, 43200), "band_pct": (5, 2, 10)},
        aggregation="just_under_limit_share", additivity="non_additive", explain="H",
        use_cases=("fraud", "aml", "structuring", "transaction_monitoring", "financial_crime"),
        pit=_FRAUD_PIT_REALTIME,
        degrade="no payment rail (so no per-rail threshold) -> SKIP.",
        stage="4-cash-out",
        eligibility=_FRAUD_BEHAVIOUR,
        derived=("is_just_under := threshold × (1 − {band_pct}%) ≤ amount < threshold, per the rail's "
                 "reporting/SCA limit — computed DOWNSTREAM (no data plane).",),
        notes=("anchor: 'payment_rail' (crime-distinctive, non-structural) — the rail defines the limit.",
               "a share/ratio — non-additive; compute per entity, never sum."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The AML templates — the §B4 LAUNDERING cycle authored to Part-F depth, typology-driven (Phase-3 Pass-2).
#
# Cycle (§B4): PLACEMENT → LAYERING → INTEGRATION. Labels are SARs (suspicion, not proof) — a filed SAR /
# screening hit is NEAR-LABEL. AML windows are trailing DAYS/weeks (typology cadence, a ``window`` param).
# Same two disciplines as fraud/credit:
#   • ROUTING — every recipe REQUIRES a crime-distinctive, NON-STRUCTURAL concept (debit_credit_indicator /
#     iso20022_purpose_code / corridor / nostro_vostro / on_chain_txn / pep_flag / watchlist_hit_flag —
#     NEVER an entity concept like counterparty_id / alert_id / case_id, which the engine's structural
#     is_grain scoring would bind onto any grain column). Grounding is the router; a churn catalog grounds
#     NOTHING here (the locked invariant: ALL_TEMPLATES on the churn _CATALOG = exactly the churn lens).
#   • LEAKAGE / NEAR-LABEL — a screening-exposure or prior-alert recipe BORDERS the label: near_label=True +
#     a ⚠ note (observe strictly BEFORE the alert; the SAR/filing OUTCOME is never an input). PII: pep /
#     sanctions / adverse-media are read-scoped (pii role). Proxy: corridor / country_code are flagged.
# The Part-H appendix in docs/…/2026-07-08-banking-feature-template-library.md is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_AML_PIT_TRAILING = (
    "trailing typology window (as_of − {window}, as_of], observed STRICTLY ≤ as_of; never forward. "
    "DESIGN-TIME declaration — no data plane enforces runtime PIT.")
_AML_NEAR_LABEL_PREFIX = (
    "⚠ NEAR-LABEL: observe the exposure STRICTLY before the alert/label — the screening hit / filed SAR "
    "OUTCOME is NEVER an input (window ≠ the label window); the 3-part leakage control must FLAG it. ")
_SCREENING_PII = ("⚠ PII: pep / sanctions / adverse-media screening data is read-scoped (needs the pii "
                  "role) under an AML lawful basis; residency + purpose gated.")
_AML_BEHAVIOUR = "built from transaction BEHAVIOUR, never a SAR/alert outcome."

AML_TEMPLATES: tuple[Template, ...] = (
    # ── PLACEMENT (dirty money enters) ──────────────────────────────────────────────────────────────
    # A.1 — structuring_smurfing (sub-threshold deposits)
    Template(
        id="structuring_smurfing", family="placement",
        intent="Structuring / smurfing — a count of sub-threshold CREDITS (cash-ins / deposits) that sit "
               "just below a reporting threshold, deliberately fragmenting a larger sum.",
        needs=(Need("direction", "debit_credit_indicator"),
               Need("purpose", "iso20022_purpose_code", optional=True),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (30, 7, 90), "band_pct": (10, 5, 20)},
        aggregation="structuring_count", additivity="additive", explain="H",
        use_cases=("aml", "structuring", "transaction_monitoring", "financial_crime"),
        pit=_AML_PIT_TRAILING,
        degrade="no dr/cr indicator -> infer credit direction from the amount sign (declared derivation "
                "§D.8; FLAG). No cash purpose code -> count all sub-threshold credits (noisier).",
        stage="placement",
        eligibility=_AML_BEHAVIOUR,
        derived=("is_sub_threshold := threshold × (1 − {band_pct}%) ≤ amount < threshold, over credits — "
                 "computed DOWNSTREAM (no data plane).",),
        notes=("anchor: 'debit_credit_indicator' (crime-distinctive, non-structural) routes this off a "
               "churn catalog (the churn fixture deliberately omits dr/cr).",
               "a count of sub-threshold deposits — additive."),
    ),
    # A.2 — cash_intensity_ratio (cash placement)
    Template(
        id="cash_intensity_ratio", family="placement",
        intent="Cash intensity — the share of a customer's inflow value carrying a CASH purpose code "
               "(ATM/branch cash-in) vs total credits; high cash intensity is a placement red flag.",
        needs=(Need("purpose", "iso20022_purpose_code"), Need("flow_col", "monetary_flow"),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (90, 30, 180), "measure": ("value_share", "count_share")},
        aggregation="cash_intensity", additivity="non_additive", explain="H",
        use_cases=("aml", "transaction_monitoring", "financial_crime"),
        pit=_AML_PIT_TRAILING,
        degrade="no purpose code -> derive a cash proxy from channel/category (declared derivation §D.8; "
                "FLAG).",
        stage="placement",
        eligibility=_AML_BEHAVIOUR,
        notes=("anchor: 'iso20022_purpose_code' (crime-distinctive, non-structural) routes this off a "
               "churn catalog.",
               "a share/ratio — non-additive; compute per entity, never sum."),
    ),
    # ── LAYERING (obscure the trail) ────────────────────────────────────────────────────────────────
    # A.3 — rapid_movement_passthrough (in ≈ out, short dwell)
    Template(
        id="rapid_movement_passthrough", family="layering",
        intent="Rapid movement of funds / pass-through — inflow ≈ outflow within a short dwell time (money "
               "in then straight out); a funnel/mule pass-through account.",
        needs=(Need("direction", "debit_credit_indicator"),
               Need("beneficiary_bank", "beneficiary_bank", optional=True),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (7, 1, 30), "measure": ("in_out_ratio", "dwell_hours")},
        aggregation="rapid_movement", additivity="non_additive", explain="H",
        use_cases=("aml", "transaction_monitoring", "financial_crime"),
        pit=_AML_PIT_TRAILING,
        degrade="no dr/cr indicator -> infer direction from amount sign (declared derivation §D.8; FLAG).",
        stage="layering",
        eligibility=_AML_BEHAVIOUR,
        notes=("anchor: 'debit_credit_indicator' (crime-distinctive, non-structural) routes this off a "
               "churn catalog.",
               "an in/out ratio (or dwell time) — non-additive; compute per entity, never sum."),
    ),
    # A.4 — round_amount_ratio (manufactured layering flows)
    Template(
        id="round_amount_ratio", family="layering",
        intent="Round-amount ratio — the share of a customer's payments that are suspiciously round "
               "(whole thousands) vs organic amounts; round numbers signal manufactured layering flows.",
        needs=(Need("purpose", "iso20022_purpose_code"), Need("flow_col", "monetary_flow"),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (90, 30, 180), "round_base": (1000, 100, 500)},
        aggregation="round_amount_ratio", additivity="non_additive", explain="H",
        use_cases=("aml", "transaction_monitoring", "financial_crime"),
        pit=_AML_PIT_TRAILING,
        degrade="",
        stage="layering",
        eligibility=_AML_BEHAVIOUR,
        derived=("is_round := amount mod {round_base} == 0 — computed DOWNSTREAM (no data plane).",),
        notes=("anchor: 'iso20022_purpose_code' (crime-distinctive, non-structural) — the payment-context "
               "anchor that routes this off a churn catalog.",
               "a share/ratio — non-additive; compute per entity, never sum."),
    ),
    # A.5 — fan_in_fan_out (mule ring / smurfing network hub)
    Template(
        id="fan_in_fan_out", family="layering",
        intent="Fan-in / fan-out — an abnormal number of distinct counterparties paying INTO then OUT OF "
               "an account in a window (a mule ring / smurfing network hub).",
        needs=(Need("counterparty", "counterparty_id"), Need("direction", "debit_credit_indicator"),
               Need("beneficiary_name", "beneficiary_name", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (30, 7, 90), "measure": ("fan_in_degree", "fan_out_degree", "fan_ratio")},
        aggregation="fan_in_fan_out", additivity="non_additive", explain="M",
        use_cases=("aml", "transaction_monitoring", "financial_crime"),
        pit=_AML_PIT_TRAILING,
        degrade="no counterparty id -> approximate degree from distinct beneficiary_name (PII; FLAG).",
        stage="layering",
        eligibility=_AML_BEHAVIOUR,
        notes=("anchor: 'debit_credit_indicator' (crime-distinctive, non-structural) routes this off a "
               "churn catalog (counterparty_id is an ENTITY concept — it would structurally bind ANY "
               "grain column, so it cannot be the sole routing anchor).",
               "a distinct-counterparty degree/ratio — non-additive."),
    ),
    # A.6 — high_risk_corridor_exposure (cross-border layering)
    Template(
        id="high_risk_corridor_exposure", family="layering",
        intent="High-risk-corridor exposure — the value (or share) of a customer's cross-border flow into "
               "high-risk / sanctioned corridors over the window.",
        needs=(Need("corridor", "corridor"), Need("country", "country_code", optional=True),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 30, 180), "measure": ("value_share", "amount")},
        aggregation="high_risk_corridor", additivity="non_additive", explain="H",
        use_cases=("aml", "sanctions", "transaction_monitoring", "financial_crime"),
        pit=_AML_PIT_TRAILING,
        degrade="no corridor -> SKIP.",
        stage="layering",
        eligibility=_CORRIDOR_PROXY + " " + _AML_BEHAVIOUR,
        notes=("anchor: 'corridor' (crime-distinctive, non-structural) routes this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: value_share is a non-additive ratio; the raw "
               "'amount' sum is additive — the default carries the ratio case."),
    ),
    # A.7 — nested_correspondent_flow (correspondent-banking visibility gap)
    Template(
        id="nested_correspondent_flow", family="layering",
        intent="Nested-correspondent / nostro-vostro flow — payments cleared through a nested downstream "
               "correspondent (a bank clearing for another bank's clients); a visibility-gap AML typology "
               "(FATF/Wolfsberg).",
        needs=(Need("nostro_vostro", "nostro_vostro"),
               Need("nested_flag", "nested_correspondent_flag", optional=True),
               Need("swift_mt", "swift_message_type", optional=True),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp")),
        params={"window": (90, 30, 180), "measure": ("nested_share", "occurred_flag")},
        aggregation="nested_correspondent", additivity="n/a", explain="M",
        use_cases=("aml", "correspondent_banking", "transaction_monitoring", "financial_crime"),
        pit=_AML_PIT_TRAILING,
        degrade="no nostro/vostro correspondent data -> SKIP.",
        stage="layering",
        eligibility=_AML_BEHAVIOUR,
        notes=("anchor: 'nostro_vostro' (crime-distinctive, non-structural) routes this off a churn "
               "catalog (correspondent-banking data is absent from a retail catalog).",
               "a nested-share / occurred flag — n/a."),
    ),
    # A.8 — crypto_offramp_exposure (fiat↔crypto ramps)
    Template(
        id="crypto_offramp_exposure", family="layering",
        intent="Crypto on/off-ramp exposure — the share of flow crossing into on-chain wallets / "
               "stablecoins (fiat↔crypto ramps), a chain-hop layering route.",
        needs=(Need("on_chain", "on_chain_txn"), Need("wallet", "wallet_address", optional=True),
               Need("stablecoin", "stablecoin", optional=True),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 30, 180), "measure": ("value_share", "count")},
        aggregation="crypto_offramp", additivity="non_additive", explain="M",
        use_cases=("aml", "crypto", "transaction_monitoring", "financial_crime"),
        pit=_AML_PIT_TRAILING,
        degrade="no on-chain / wallet data -> SKIP.",
        stage="layering",
        eligibility="⚠ PII: wallet_address is pseudonymous-but-linkable PERSONAL data (FATF travel-rule) "
                    "— read-scoped (pii role) when a wallet is bound. " + _AML_BEHAVIOUR,
        notes=("anchor: 'on_chain_txn' (crime-distinctive, non-structural) routes this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: value_share is a non-additive ratio; the count "
               "alternate is additive — the default carries the ratio case."),
    ),
    # ── INTEGRATION (clean money returns) + cross-cutting screening ──────────────────────────────────
    # A.9 — dormant_reactivation (parked mule/shell reactivating)
    Template(
        id="dormant_reactivation", family="integration",
        intent="Dormant-then-active reactivation — an account dormant for a long spell then suddenly "
               "receiving large credits (a previously-parked mule/shell reactivating for integration).",
        needs=(Need("direction", "debit_credit_indicator"), Need("flow_col", "monetary_flow"),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (180, 90, 365), "dormancy_days": (90, 60, 180)},
        aggregation="dormant_reactivation", additivity="n/a", explain="H",
        use_cases=("aml", "transaction_monitoring", "financial_crime"),
        pit=_AML_PIT_TRAILING,
        degrade="no dr/cr indicator -> infer credit direction from amount sign (declared derivation §D.8; "
                "FLAG).",
        stage="integration",
        eligibility=_AML_BEHAVIOUR,
        derived=("is_reactivation := no activity for ≥ {dormancy_days}d then a large credit — computed "
                 "DOWNSTREAM (no data plane); dr/cr identifies the inbound credit.",),
        notes=("anchor: 'debit_credit_indicator' (crime-distinctive, non-structural) routes this off a "
               "churn catalog (dormancy alone is generic event/entity — it would cross-surface).",
               "a reactivation flag — n/a."),
    ),
    # A.10 — screening_exposure (PEP / sanctions / adverse-media) — NEAR-LABEL + PII
    Template(
        id="screening_exposure", family="integration",
        intent="PEP / sanctions / adverse-media exposure over the customer and its counterparties — the "
               "share/severity of screened-risky relationships (a KYC/CDD financial-crime marker).",
        needs=(Need("pep", "pep_flag"), Need("sanctions", "sanctions_hit_flag", optional=True),
               Need("adverse_media", "adverse_media_flag", optional=True),
               Need("watchlist", "watchlist_hit_flag", optional=True),
               Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("exposed_flag", "exposure_share")},
        aggregation="screening_exposure", additivity="n/a", explain="H",
        use_cases=("aml", "sanctions", "kyc", "transaction_monitoring", "financial_crime"),
        pit=_AML_PIT_TRAILING,
        degrade="no screening flags at all -> SKIP.",
        stage="integration",
        near_label=True,
        eligibility=_AML_NEAR_LABEL_PREFIX + _SCREENING_PII,
        notes=("anchor: 'pep_flag' (crime-distinctive, pii, non-structural) routes this off a churn "
               "catalog and needs the pii role.",
               "sanctions_hit_flag / adverse_media_flag / watchlist_hit_flag are NEAR-LABEL screening "
               "concepts (optional here); a filed SAR / confirmed hit is the LABEL, never an input.",
               "a flag / share — n/a."),
    ),
    # A.11 — prior_alert_recidivism (repeat-suspicion history) — NEAR-LABEL
    Template(
        id="prior_alert_recidivism", family="integration",
        intent="Prior-alert recidivism — the count/recency of PRIOR monitoring alerts that resulted in a "
               "watchlist hit on this entity (a repeat-suspicion history feature).",
        needs=(Need("watchlist", "watchlist_hit_flag"), Need("alert", "alert_id", optional=True),
               Need("case", "case_id", optional=True), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("prior_alert_count", "days_since_last")},
        aggregation="prior_alert_recidivism", additivity="additive", explain="M",
        use_cases=("aml", "transaction_monitoring", "financial_crime"),
        pit=_AML_PIT_TRAILING,
        degrade="no watchlist/alert history -> SKIP.",
        stage="integration",
        near_label=True,
        eligibility=_AML_NEAR_LABEL_PREFIX + "a prior-alert history BORDERS the label — the SAR/filing "
                    "OUTCOME of any alert is NEVER an input, only the fact/timing of a prior alert.",
        notes=("anchor: 'watchlist_hit_flag' (crime-distinctive, near-label, non-structural) routes this "
               "off a churn catalog (alert_id / case_id are ENTITY concepts — they would structurally "
               "bind ANY grain column, so they are optional, not the routing anchor).",
               "a count of prior alerts — additive; days_since_last is a recency (n/a)."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The collections & recoveries templates — the §B6 DELINQUENCY → RECOVERY journey authored to Part-F
# depth (Phase-3 Pass-3).
#
# Journey (§B6): PRE-DELINQUENCY → EARLY (1-29 DPD) → MID (30-89) → LATE (90+) → RECOVERY / CHARGE-OFF.
# Two authoring disciplines are load-bearing (mirroring credit_risk):
#   • ROUTING — every recipe REQUIRES at least one collections-distinctive, NON-STRUCTURAL concept
#     (delinquency_bucket / dpd / scheduled_amount / cost_to_collect / restructured_flag /
#     recovery_amount / write_off_amount — NOT an entity/as_of concept, which the engine's structural
#     is_grain/is_as_of scoring would bind onto ANY grain/as-of column, cross-surfacing the family).
#     Grounding is the router: the family surfaces ONLY where the catalog carries collections signals; a
#     churn catalog with only generic monetary_stock/flow + as_of + customer_id grounds NOTHING here
#     (the locked invariant: ALL_TEMPLATES on the churn _CATALOG = exactly the churn lens).
#   • NEAR-LABEL — a recipe binding a bucket/DPD roll, a forbearance concession, or (⚠⚠ hardest) a
#     POST-charge-off recovery_amount / write_off_amount BORDERS the cure/recovery/charge-off OUTCOME:
#     near_label=True + a ⚠ note (observe strictly BEFORE the labelled outcome). The recovery/write-off
#     recipes carry an EXTRA hard flag — those amounts ARE ~the recovery label, so a model predicting
#     cure/recovery must NEVER read them as an input (bind only for a downstream post-default LGD study).
# The Part-I appendix in docs/…/2026-07-08-banking-feature-template-library.md is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_COLLECTIONS_PIT_STATE = ("point-in-time collections STATE observed as-of: the latest bucket / DPD / flag "
                          "within (as_of − {window}, as_of], knowable strictly ≤ as_of, never forward. "
                          "DESIGN-TIME declaration — no data plane enforces runtime PIT.")
_COLLECTIONS_NEAR_LABEL_PREFIX = (
    "⚠ NEAR-LABEL: observe the collections signal STRICTLY before the cure / recovery / charge-off "
    "outcome (never on/after it; window ≠ the label window); the 3-part leakage control must FLAG it. ")
_RECOVERY_POST_DEFAULT = (
    "⚠⚠ POST-DEFAULT RECOVERY-OUTCOME: recovery_amount / write_off_amount are booked AFTER charge-off — "
    "a model predicting cure/recovery MUST NOT read them as an INPUT (they ARE ~the recovery label). "
    "Bind ONLY for a downstream LGD / recovery-severity study observed strictly after the default event. ")
_COLLECTIONS_VULNERABILITY = (
    "conduct: a vulnerable-customer (FCA Consumer-Duty) flag drives supportive handling — vulnerability_"
    "flag is special-category (engine-blocked as a feature input); segment on it downstream under a gate.")

COLLECTIONS_TEMPLATES: tuple[Template, ...] = (
    # ── EARLY (1-29 DPD) — promise / arrangement behaviour ──────────────────────────────────────────
    # K.1 — promise_to_pay_adherence
    Template(
        id="promise_to_pay_adherence", family="promise_to_pay",
        intent="Promise-to-pay adherence — share of the scheduled/promised amount actually PAID while "
               "delinquent (payments vs scheduled_amount); a broken promise is a needs-intervention signal.",
        needs=(Need("scheduled_col", "scheduled_amount"), Need("paid_col", "monetary_flow"),
               Need("dpd_col", "dpd", optional=True), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 60, 180), "tolerance_pct": (5, 0, 10)},
        aggregation="ptp_adherence", additivity="non_additive", explain="H",
        use_cases=("collections", "recoveries", "self_cure"),
        pit=_PIT_TRAILING,
        degrade="no delinquency (dpd) context -> compute adherence over ALL scheduled installments "
                "(weaker + FLAGGED — not collections-scoped).",
        stage="early-1-29-dpd",
        eligibility="single currency — convert to base first. " + _COLLECTIONS_VULNERABILITY,
        derived=("kept := paid ≥ scheduled × (1 − {tolerance_pct}%) — a promise-kept test per "
                 "installment, computed DOWNSTREAM (no data plane).",),
        notes=("anchor: 'scheduled_amount' (collections-distinctive — the promised installment DUE, "
               "absent from a churn catalog) routes this off a churn catalog.",
               "concept sub: no dedicated promise_to_pay concept — scheduled_amount is the promised due.",
               "an adherence ratio — non-additive; compute per entity, never sum."),
    ),
    # K.2 — payment_plan_adherence
    Template(
        id="payment_plan_adherence", family="payment_plan",
        intent="Payment-plan adherence — count of consecutive scheduled arrangement installments met on "
               "time (a kept-plan streak); a broken plan flips a self-curer to needs-intervention.",
        needs=(Need("scheduled_col", "scheduled_amount"), Need("paid_col", "monetary_flow"),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (180, 90, 365), "tolerance_pct": (5, 0, 10)},
        aggregation="plan_adherence_streak", additivity="additive", explain="H",
        use_cases=("collections", "recoveries", "self_cure"),
        pit=_PIT_TRAILING,
        degrade="no arrangement schedule -> SKIP (use promise_to_pay_adherence on ad-hoc promises).",
        stage="early-1-29-dpd",
        eligibility="single currency. " + _COLLECTIONS_VULNERABILITY,
        derived=("is_met := paid ≥ scheduled × (1 − {tolerance_pct}%) per arrangement installment — the "
                 "streak counts consecutive met installments; computed DOWNSTREAM (no data plane).",),
        notes=("anchor: 'scheduled_amount' (collections-distinctive — the arrangement installment DUE) "
               "routes this off a churn catalog.",
               "a count of met installments — additive."),
    ),
    # ── MID (30-89 DPD) — roll dynamics + contactability ────────────────────────────────────────────
    # K.3 — cure_reage_dynamics (bucket roll-BACK, NEAR-LABEL)
    Template(
        id="cure_reage_dynamics", family="cure_reage",
        intent="Cure / re-age dynamics — did the delinquency_bucket roll BACK toward current in the "
               "window (a self-cure / re-age)? measure=cure_flag / bucket_improvement.",
        needs=(Need("bucket_col", "delinquency_bucket"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={"window": (90, 60, 180), "measure": ("cure_flag", "bucket_improvement")},
        aggregation="cure_reage", additivity="n/a", explain="H",
        use_cases=("collections", "recoveries", "self_cure"),
        pit=_COLLECTIONS_PIT_STATE,
        stage="mid-30-89-dpd",
        near_label=True,
        eligibility=_COLLECTIONS_NEAR_LABEL_PREFIX + "a cure/re-age IS the collections OUTCOME state — "
                    "observe the roll-back strictly before the labelled cure. " + _COLLECTIONS_VULNERABILITY,
        notes=("anchor: 'delinquency_bucket' (near-label, collections-distinctive) routes this off a "
               "churn catalog.",
               "a cure flag / bucket improvement — n/a (not summable).",
               "borders the cure/roll label — observe strictly pre-outcome."),
    ),
    # K.4 — roll_forward_severity (DPD worsening, NEAR-LABEL)
    Template(
        id="roll_forward_severity", family="roll_rate",
        intent="Roll-forward severity — did days-past-due WORSEN over the window (max(dpd) vs dpd at "
               "window start)? measure=roll_forward_flag / dpd_delta; a forward roll is escalating.",
        needs=(Need("dpd_col", "dpd"), Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (90, 60, 180), "measure": ("roll_forward_flag", "dpd_delta")},
        aggregation="roll_forward", additivity="n/a", explain="H",
        use_cases=("collections", "recoveries", "early_warning"),
        pit=_COLLECTIONS_PIT_STATE,
        stage="mid-30-89-dpd",
        near_label=True,
        eligibility=_COLLECTIONS_NEAR_LABEL_PREFIX + "a DPD rolling to 90+ IS the charge-off backstop. "
                    + _COLLECTIONS_VULNERABILITY,
        notes=("anchor: 'dpd' (collections-distinctive; a max DPD borders the charge-off label) routes "
               "this off a churn catalog.",
               "a forward-roll flag / DPD delta — n/a (not summable).",
               "borders the charge-off label — observe strictly pre-outcome."),
    ),
    # K.5 — right_party_contact_intensity (contactability — no contact-event concept -> substitution)
    Template(
        id="right_party_contact_intensity", family="contactability",
        intent="Right-party-contact intensity — the rate/volume of successful collections contacts while "
               "the account is worked (contactability drives cure); measure=rpc_rate / attempt_count.",
        needs=(Need("cost_col", "cost_to_collect"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 30, 180), "measure": ("rpc_rate", "attempt_count")},
        aggregation="rpc_intensity", additivity="non_additive", explain="M",
        use_cases=("collections", "recoveries", "contactability"),
        pit=_PIT_TRAILING,
        degrade="no contact-event concept in the taxonomy -> approximate contact attempts from "
                "cost_to_collect activity / a channel event downstream (declared derivation §D.8; FLAG).",
        stage="mid-30-89-dpd",
        eligibility="cost_to_collect only exists for delinquent/worked accounts (survivorship — FLAG). "
                    + _COLLECTIONS_VULNERABILITY,
        derived=("contact_attempt := a right-party / attempted-contact event — DERIVED downstream (no "
                 "data plane); rpc_rate := right_party_contacts / attempts.",),
        notes=("anchor: 'cost_to_collect' (collections-distinctive, NOT near-label — an operational "
               "cost, not an outcome) routes this off a churn catalog.",
               "concept sub: the taxonomy has NO contact-event / right-party-contact concept — the "
               "collections cost_to_collect is the distinctive anchor and the contact event is a "
               "declared downstream derivation.",
               "OUTPUT additivity is measure-dependent: rpc_rate is a non-additive ratio; attempt_count "
               "is additive — the default carries the ratio case."),
    ),
    # ── LATE (90+ DPD) — tenure, hardship, cost ─────────────────────────────────────────────────────
    # K.6 — days_in_collection (NEAR-LABEL — the charge-off tail)
    Template(
        id="days_in_collection", family="collection_tenure",
        intent="Days-in-collection — as_of − the date the account first entered a delinquent "
               "delinquency_bucket (how long it has been worked); a long spell lowers cure probability.",
        needs=(Need("bucket_col", "delinquency_bucket"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={"window": (365, 180, 90)},
        aggregation="days_in_collection", additivity="n/a", explain="H",
        use_cases=("collections", "recoveries"),
        pit=_COLLECTIONS_PIT_STATE,
        stage="late-90-plus-dpd",
        near_label=True,
        eligibility=_COLLECTIONS_NEAR_LABEL_PREFIX + "a lengthening collection spell borders the "
                    "charge-off tail. " + _COLLECTIONS_VULNERABILITY,
        notes=("anchor: 'delinquency_bucket' (near-label, collections-distinctive) — the entry into a "
               "non-current bucket marks the collection-start clock; routes this off a churn catalog "
               "(the collection-start date is a DESIGN-TIME declaration over bucket history).",
               "a duration since collection-start — n/a."),
    ),
    # K.7 — hardship_forbearance_in_collection (restructured_flag, NEAR-LABEL)
    Template(
        id="hardship_forbearance_in_collection", family="forbearance",
        intent="Hardship / forbearance-in-collection — did a concession (payment holiday / re-age / "
               "restructure) occur while delinquent? measure=occurred_flag / count.",
        needs=(Need("restructured_col", "restructured_flag"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("occurred_flag", "count")},
        aggregation="hardship_forbearance", additivity="n/a", explain="H",
        use_cases=("collections", "recoveries", "hardship"),
        pit=_COLLECTIONS_PIT_STATE,
        stage="late-90-plus-dpd",
        near_label=True,
        eligibility=_COLLECTIONS_NEAR_LABEL_PREFIX + "a forbearance concession ≈ the impaired/roll label "
                    "(IFRS9 Stage-3 trigger). " + _COLLECTIONS_VULNERABILITY,
        notes=("anchor: 'restructured_flag' (near-label, collections-distinctive) routes this off a "
               "churn catalog.",
               "OUTPUT additivity is measure-dependent: occurred_flag is n/a; a count is additive.",
               "borders the impaired/roll label — observe strictly pre-outcome."),
    ),
    # K.8 — cost_to_collect_ratio (cost efficiency; NOT near-label)
    Template(
        id="cost_to_collect_ratio", family="cost_to_collect",
        intent="Cost-to-collect ratio — collections/workout cost vs the balance-at-risk over the window "
               "(cost efficiency); a high ratio flags an uneconomic-to-work account.",
        needs=(Need("cost_col", "cost_to_collect"), Need("balance_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("to_balance", "absolute")},
        aggregation="cost_to_collect_ratio", additivity="non_additive", explain="H",
        use_cases=("collections", "recoveries", "cost_efficiency"),
        pit=_PIT_TRAILING,
        degrade="no balance-at-risk stock -> report the absolute cost_to_collect (additive) not the ratio.",
        stage="late-90-plus-dpd",
        eligibility="cost_to_collect only exists for delinquent/defaulted accounts (survivorship + "
                    "leakage-risk — FLAG); single currency. " + _COLLECTIONS_VULNERABILITY,
        notes=("anchor: 'cost_to_collect' (collections-distinctive, NOT near-label) routes this off a "
               "churn catalog.",
               "OUTPUT additivity is measure-dependent: to_balance is a non-additive ratio; the absolute "
               "cost is an additive flow — the default carries the ratio case."),
    ),
    # ── RECOVERY / CHARGE-OFF — ⚠⚠ POST-DEFAULT recovery-outcome (NEAR-LABEL, hard flag) ─────────────
    # K.9 — recovery_rate (recovery_amount — POST-charge-off, NEAR-LABEL ⚠⚠)
    Template(
        id="recovery_rate", family="recovery",
        intent="Recovery rate — post-charge-off recovery_amount collected vs the defaulted/charged-off "
               "balance (the LGD complement); measure=to_defaulted_balance / cumulative_amount.",
        needs=(Need("recovery_col", "recovery_amount"), Need("balance_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 720), "measure": ("to_defaulted_balance", "cumulative_amount")},
        aggregation="recovery_rate", additivity="non_additive", explain="H",
        use_cases=("recoveries", "lgd", "workout"),
        pit=_PIT_TRAILING,
        degrade="no charged-off balance base -> report cumulative recovery_amount (additive) not the rate.",
        stage="recovery-charge-off",
        near_label=True,
        eligibility=_RECOVERY_POST_DEFAULT + _COLLECTIONS_NEAR_LABEL_PREFIX + "single currency. "
                    + _COLLECTIONS_VULNERABILITY,
        notes=("anchor: 'recovery_amount' (near-label, POST-default — the LGD numerator) routes this off "
               "a churn catalog.",
               "⚠⚠ a recovery/cure model must NEVER read recovery_amount as an INPUT — it IS ~the "
               "recovery label; bind ONLY for a downstream post-default LGD / severity study observed "
               "strictly after the default event.",
               "OUTPUT additivity is measure-dependent: to_defaulted_balance is a non-additive ratio; "
               "the cumulative amount is an additive flow — the default carries the ratio case."),
    ),
    # K.10 — write_off_severity (write_off_amount — the charge-off IS an outcome, NEAR-LABEL ⚠⚠)
    Template(
        id="write_off_severity", family="charge_off",
        intent="Write-off / charge-off severity — the write_off_amount charged off vs the exposure at "
               "charge-off (loss severity); measure=to_exposure / amount.",
        needs=(Need("write_off_col", "write_off_amount"), Need("exposure_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 720), "measure": ("to_exposure", "amount")},
        aggregation="write_off_severity", additivity="non_additive", explain="H",
        use_cases=("recoveries", "lgd", "workout"),
        pit=_PIT_TRAILING,
        degrade="no exposure base -> report the write_off_amount (additive) not the severity ratio.",
        stage="recovery-charge-off",
        near_label=True,
        eligibility=_RECOVERY_POST_DEFAULT + _COLLECTIONS_NEAR_LABEL_PREFIX + "single currency. "
                    + _COLLECTIONS_VULNERABILITY,
        notes=("anchor: 'write_off_amount' (near-label — the charge-off IS an outcome) routes this off a "
               "churn catalog.",
               "⚠⚠ the charge-off IS the label event — features from write_off_amount leak it; bind ONLY "
               "for a downstream post-charge-off loss study.",
               "OUTPUT additivity is measure-dependent: to_exposure is a non-additive ratio; the raw "
               "write_off_amount is an additive flow — the default carries the ratio case."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The deposit / liquidity / treasury (ALM) templates — the §B7 STABILITY spectrum authored to Part-F
# depth (Phase-3 Pass-3).
#
# Spectrum (§B7): STABLE CORE → RATE-SENSITIVE → SURGE / HOT MONEY → RUNOFF-PRONE → OUTFLOW ⚠. This is
# NOT a customer funnel but a deposit-behaviour spectrum feeding LCR/NSFR, FTP and ALM. The load-bearing
# discipline here is ROUTING-BY-VALUE-ADD: churn ALREADY owns plain balance behaviour (balance_trend /
# balance_volatility / days_below_threshold), so this family deliberately does NOT re-author a balance
# stability/trend feature. Every recipe anchors on an ALM-DISTINCTIVE, NON-STRUCTURAL treasury concept a
# plain balance catalog CANNOT ground (benchmark_rate / ftp_rate / wholesale_funding / maturity_date /
# tenor / hqla / lcr / nsfr / repricing_gap / beta) — that binds only by exact concept match, so a churn
# catalog with monetary_stock + as_of + customer_id grounds NOTHING here (the locked churn=churn-lens
# invariant). A PLAIN balance concentration WOULD cross-surface, so rate_sensitive_concentration weights
# by deposit 'beta' precisely to keep its anchor distinctive. No recipe is near-label (treasury signals
# do not border a customer outcome). The Part-I appendix is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_DEPOSIT_PIT_STATE = ("point-in-time deposit-behaviour STATE observed as-of: the latest balance / rate / "
                      "ratio within (as_of − {window}, as_of], knowable strictly ≤ as_of, never forward. "
                      "DESIGN-TIME declaration — no data plane enforces runtime PIT.")
_DEPOSIT_NOT_A_REHASH = ("NOT a balance re-hash: an ALM/treasury signal a plain balance catalog cannot "
                         "ground (churn already owns balance_trend / balance_volatility / days_below).")
_ALM_SINGLE_CCY = ("single currency — convert to base first; a stock takes the LATEST over time "
                   "(never summed across dates).")

DEPOSITS_TEMPLATES: tuple[Template, ...] = (
    # ── STABLE CORE — sticky funding + liquidity contribution ───────────────────────────────────────
    # T.1 — nmd_stickiness (non-maturity-deposit behavioural life via FTP)
    Template(
        id="nmd_stickiness", family="nmd_stability",
        intent="Non-maturity-deposit stickiness / decay — the assumed behavioural life of a non-maturity "
               "balance priced by its funds-transfer-pricing (ftp_rate) curve; a longer FTP tenor = a "
               "stickier core, a shorter one = decay-prone.",
        needs=(Need("ftp_col", "ftp_rate"), Need("balance_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("ftp_tenor_proxy", "decay_rate")},
        aggregation="nmd_stickiness", additivity="non_additive", explain="M",
        use_cases=("deposit_stability", "alm", "ftp"),
        pit=_DEPOSIT_PIT_STATE,
        degrade="no FTP curve -> derive a decay rate from the balance's own runoff downstream (declared "
                "derivation §D.8; probabilistic — FLAG).",
        stage="stable-core",
        eligibility=_ALM_SINGLE_CCY,
        derived=("behavioural_life := the FTP-implied tenor of the NMD pool — DERIVED downstream from the "
                 "ftp_rate curve (no data plane).",),
        notes=("anchor: 'ftp_rate' (ALM-distinctive — the internal funds-transfer price encodes "
               "behavioural life) routes this off a churn catalog.",
               _DEPOSIT_NOT_A_REHASH,
               "a decay rate / tenor proxy — non-additive."),
    ),
    # T.2 — hqla_eligibility_contribution (HQLA / LCR buffer contribution)
    Template(
        id="hqla_eligibility_contribution", family="lcr_liquidity",
        intent="HQLA / LCR contribution — the High-Quality-Liquid-Asset amount a deposit relationship "
               "backs (or the net cash outflow it drives against the LCR buffer) over the window.",
        needs=(Need("hqla_col", "hqla"), Need("lcr_col", "lcr", optional=True),
               Need("balance_col", "monetary_stock"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={"window": (90, 30, 180), "measure": ("hqla_amount", "net_outflow_contribution")},
        aggregation="hqla_contribution", additivity="semi_additive", explain="H",
        use_cases=("liquidity_risk", "alm", "lcr"),
        pit=_DEPOSIT_PIT_STATE,
        degrade="no HQLA buffer reported -> SKIP.",
        stage="stable-core",
        eligibility=_ALM_SINGLE_CCY,
        notes=("anchor: 'hqla' (ALM-distinctive — the Basel-III LCR buffer stock) routes this off a "
               "churn catalog.",
               _DEPOSIT_NOT_A_REHASH,
               "an HQLA / outflow AMOUNT — semi-additive: sum across the buffer, latest over time (never "
               "sum daily snapshots)."),
    ),
    # T.3 — nsfr_asf_contribution (NSFR available-stable-funding contribution)
    Template(
        id="nsfr_asf_contribution", family="nsfr_funding",
        intent="NSFR available-stable-funding contribution — the structural funding stability a deposit "
               "provides under the Net-Stable-Funding-Ratio (its ASF factor × balance); "
               "measure=nsfr_ratio / asf_amount.",
        needs=(Need("nsfr_col", "nsfr"), Need("balance_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("nsfr_ratio", "asf_amount")},
        aggregation="nsfr_asf", additivity="non_additive", explain="H",
        use_cases=("liquidity_risk", "alm", "nsfr"),
        pit=_DEPOSIT_PIT_STATE,
        degrade="no NSFR / ASF factor -> SKIP.",
        stage="stable-core",
        eligibility=_ALM_SINGLE_CCY,
        notes=("anchor: 'nsfr' (ALM-distinctive — the Basel-III Net Stable Funding Ratio) routes this "
               "off a churn catalog.",
               _DEPOSIT_NOT_A_REHASH,
               "OUTPUT additivity is measure-dependent: nsfr_ratio is a non-additive ratio; asf_amount "
               "is a semi-additive stock — the default carries the ratio case."),
    ),
    # ── RATE-SENSITIVE — deposit beta, LCR outflow weight, repricing gap ────────────────────────────
    # T.4 — deposit_beta (rate sensitivity vs a benchmark reference rate)
    Template(
        id="deposit_beta", family="rate_sensitivity",
        intent="Deposit beta / rate-sensitivity — how much the deposit rate (or balance) responds to a "
               "move in a reference benchmark_rate: Δ(paid rate)/Δ(benchmark) over the window; high beta "
               "= rate-sensitive, run-prone funding.",
        needs=(Need("benchmark_col", "benchmark_rate"), Need("balance_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("rate_beta", "balance_beta")},
        aggregation="deposit_beta", additivity="non_additive", explain="H",
        use_cases=("deposit_stability", "alm", "liquidity_risk", "ftp"),
        pit=_DEPOSIT_PIT_STATE,
        degrade="only a single rate snapshot (no history) -> SKIP (no beta from one point).",
        stage="rate-sensitive",
        eligibility=_ALM_SINGLE_CCY,
        notes=("anchor: 'benchmark_rate' (ALM-distinctive reference rate — SOFR/SONIA/€STR) routes this "
               "off a churn catalog.",
               _DEPOSIT_NOT_A_REHASH,
               "a beta (a ratio) — non-additive; compute per depositor/segment, never sum."),
    ),
    # T.5 — lcr_outflow_weight (modelled 30-day net-cash-outflow rate)
    Template(
        id="lcr_outflow_weight", family="lcr_liquidity",
        intent="LCR outflow weight — the deposit's modelled 30-day net-cash-outflow RATE (the LCR runoff "
               "factor applied to the balance); a higher weight = less LCR-friendly funding.",
        needs=(Need("lcr_col", "lcr"), Need("balance_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (90, 30, 180)},
        aggregation="lcr_outflow_weight", additivity="non_additive", explain="H",
        use_cases=("liquidity_risk", "alm", "lcr"),
        pit=_DEPOSIT_PIT_STATE,
        degrade="no LCR runoff factor -> SKIP.",
        stage="rate-sensitive",
        eligibility=_ALM_SINGLE_CCY,
        notes=("anchor: 'lcr' (ALM-distinctive — the Basel-III Liquidity Coverage Ratio) routes this off "
               "a churn catalog.",
               _DEPOSIT_NOT_A_REHASH,
               "an outflow-weight RATIO — non-additive; compute per depositor/segment, never sum."),
    ),
    # T.6 — repricing_gap_exposure (IRRBB repricing/maturity gap)
    Template(
        id="repricing_gap_exposure", family="irrbb_repricing",
        intent="Repricing-gap exposure — the net IRRBB repricing/maturity gap (assets less liabilities "
               "repricing in a time bucket) the deposit book carries; a large signed gap is rate-risk "
               "exposure.",
        needs=(Need("gap_col", "repricing_gap"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={"window": (90, 180, 365), "measure": ("gap_level", "gap_trend")},
        aggregation="repricing_gap", additivity="non_additive", explain="H",
        use_cases=("liquidity_risk", "alm", "irrbb"),
        pit=_DEPOSIT_PIT_STATE,
        degrade="no repricing-bucket gap reported -> SKIP.",
        stage="rate-sensitive",
        eligibility="the gap NETS within a snapshot (assets − liabilities) — never sum across dates.",
        notes=("anchor: 'repricing_gap' (ALM-distinctive — the IRRBB gap) routes this off a churn "
               "catalog.",
               _DEPOSIT_NOT_A_REHASH,
               "a signed gap that nets within a snapshot — non-additive; never summed across dates."),
    ),
    # ── SURGE / HOT MONEY — non-core funding share + concentration ───────────────────────────────────
    # T.7 — hot_money_share (non-core wholesale funding share)
    Template(
        id="hot_money_share", family="hot_money",
        intent="Hot-money / surge share — the share of the funding base that is non-core wholesale/market "
               "funding (wholesale_funding) vs sticky retail deposits; high hot-money share = surge/run "
               "risk.",
        needs=(Need("wholesale_col", "wholesale_funding"), Need("balance_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (90, 180, 365), "measure": ("value_share", "surge_flag")},
        aggregation="hot_money_share", additivity="non_additive", explain="H",
        use_cases=("deposit_stability", "alm", "liquidity_risk"),
        pit=_DEPOSIT_PIT_STATE,
        degrade="no wholesale-funding split -> SKIP (a plain balance can't separate core vs hot money).",
        stage="surge-hot-money",
        eligibility=_ALM_SINGLE_CCY,
        notes=("anchor: 'wholesale_funding' (ALM-distinctive — non-core funding, a run-off-risk stock) "
               "routes this off a churn catalog.",
               _DEPOSIT_NOT_A_REHASH,
               "OUTPUT additivity is measure-dependent: value_share is a non-additive ratio; surge_flag "
               "is n/a — the default carries the ratio case."),
    ),
    # T.8 — rate_sensitive_concentration (funding concentration WEIGHTED by deposit beta)
    Template(
        id="rate_sensitive_concentration", family="funding_concentration",
        intent="Rate-sensitive funding concentration — how concentrated the funding base is in high-beta "
               "/ hot depositors (an HHI of balance weighted by deposit beta); a book concentrated in "
               "rate-sensitive money is run-prone.",
        needs=(Need("beta_col", "beta"), Need("balance_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("beta_weighted_hhi", "top_depositor_share")},
        aggregation="rate_sensitive_concentration", additivity="non_additive", explain="M",
        use_cases=("deposit_stability", "alm", "liquidity_risk", "concentration_risk"),
        pit=_DEPOSIT_PIT_STATE,
        degrade="no per-depositor deposit beta -> approximate with a plain balance HHI (weaker; FLAG — "
                "loses the rate-sensitivity weighting).",
        stage="surge-hot-money",
        eligibility=_ALM_SINGLE_CCY,
        notes=("anchor: 'beta' (ALM-distinctive deposit beta) routes this off a churn catalog — a PLAIN "
               "balance concentration WOULD cross-surface (monetary_stock + customer_id exist on churn), "
               "so the beta weighting is load-bearing for routing, not just economics.",
               _DEPOSIT_NOT_A_REHASH,
               "a concentration index (HHI / top-share) — non-additive."),
    ),
    # ── RUNOFF-PRONE — maturity laddering + early-break behaviour ────────────────────────────────────
    # T.9 — maturity_ladder_runoff (term deposits maturing in a horizon bucket)
    Template(
        id="maturity_ladder_runoff", family="runoff_ladder",
        intent="Runoff / maturity laddering — the balance (or share) of term deposits maturing inside a "
               "horizon bucket keyed on maturity_date; a lumpy near-dated ladder is refinancing/runoff "
               "risk.",
        needs=(Need("maturity_col", "maturity_date"), Need("balance_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"horizon_days": (30, 90, 365), "measure": ("runoff_share", "runoff_amount")},
        aggregation="maturity_runoff", additivity="non_additive", explain="H",
        use_cases=("deposit_stability", "alm", "liquidity_risk"),
        pit=_DEPOSIT_PIT_STATE,
        degrade="no maturity_date (a non-maturity deposit) -> SKIP; use nmd_stickiness for NMDs.",
        stage="runoff-prone",
        eligibility=_ALM_SINGLE_CCY,
        notes=("anchor: 'maturity_date' (ALM-distinctive — the contractual runoff clock) routes this off "
               "a churn catalog.",
               _DEPOSIT_NOT_A_REHASH,
               "OUTPUT additivity is measure-dependent: runoff_amount is a semi-additive stock (latest "
               "over time); runoff_share is a non-additive ratio — the default carries the ratio case."),
    ),
    # T.10 — early_withdrawal_break (term deposits broken before their contractual term)
    Template(
        id="early_withdrawal_break", family="break_behaviour",
        intent="Early-withdrawal / break behaviour — the rate at which term deposits are broken BEFORE "
               "their contractual term (tenor) elapses; measure=break_rate / break_count. Early breaks "
               "shorten effective funding life.",
        needs=(Need("tenor_col", "tenor"), Need("balance_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("break_rate", "break_count")},
        aggregation="early_break", additivity="non_additive", explain="H",
        use_cases=("deposit_stability", "alm", "liquidity_risk"),
        pit=_DEPOSIT_PIT_STATE,
        degrade="no term (tenor) -> SKIP (a non-maturity deposit cannot be 'broken' early).",
        stage="runoff-prone",
        eligibility="single currency; a notice-period deposit substitutes its notice term for 'tenor'.",
        derived=("is_early_break := a withdrawal before the contractual term elapses — a break EVENT "
                 "DERIVED downstream (no data plane); break_rate := early breaks / active term deposits.",),
        notes=("anchor: 'tenor' (ALM-distinctive — the contractual term) routes this off a churn catalog.",
               "concept sub: no dedicated notice_period concept — a notice-period deposit substitutes its "
               "notice term for 'tenor'.",
               _DEPOSIT_NOT_A_REHASH,
               "OUTPUT additivity is measure-dependent: break_rate is a non-additive ratio; break_count "
               "is additive — the default carries the ratio case."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The payments-as-a-business templates — the §B14 payments beyond cards, authored to Part-F depth
# (Phase-3 Pass-3).
#
# Coverage (§B14): rail/scheme throughput + mix, interchange/MDR economics, settlement quality
# (authorisation / chargeback / returns / timing), corridor/cross-border mix, and payment-purpose mix.
# The load-bearing discipline is ROUTING: every recipe REQUIRES a payments-distinctive, NON-STRUCTURAL
# concept (payment_rail / scheme / interchange / merchant_discount_rate / settlement_status /
# settlement_cycle / direct_debit / corridor / iso20022_purpose_code — that binds only by exact concept
# match). Grounding is the router; a churn catalog (which even carries beneficiary_bank but NO
# payment_rail / dr-cr / rail signals) grounds NOTHING here — the locked churn=churn-lens invariant.
# These payments recipes DO also ground on the fraud/AML crime catalog (which carries payment_rail /
# scheme / corridor / dr-cr) — that is expected overlap and breaks no crime test (those assert per-family
# grounding, never that ALL_TEMPLATES on the crime catalog is only fraud+AML). Additivity: economics
# amounts (interchange / value) additive, rates non-additive, mix/diversity n/a. No recipe is near-label
# (a payments-throughput/economics signal does not border a customer outcome). Part-I is the doc source.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_PAYMENTS_CORRIDOR_PROXY = (
    "⚠ corridor / country_code are national-origin PROXIES (fair-lending) — proxy-flagged; payments/AML-"
    "permitted but bias-watched, NEVER a credit input.")

PAYMENTS_TEMPLATES: tuple[Template, ...] = (
    # ── THROUGHPUT & MIX — volume/value by rail, rail/scheme diversity, purpose mix ─────────────────
    # Y.1 — rail_volume_value
    Template(
        id="rail_volume_value", family="rail_mix",
        intent="Payment volume / value by rail — count (or summed value) of payments on a given "
               "payment_rail (FPS/BACS/CHAPS/SEPA/ACH/card) over the window; the base throughput signal.",
        needs=(Need("rail", "payment_rail"), Need("flow_col", "monetary_flow"),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (90, 30, 180), "measure": ("value", "count")},
        aggregation="rail_volume", additivity="additive", explain="H",
        use_cases=("payments", "payments_ops", "merchant_analytics"),
        pit=_PIT_TRAILING,
        degrade="no payment_rail -> SKIP (this is a rail-segmented throughput signal).",
        stage="throughput",
        eligibility="single currency for the value measure — convert to base first.",
        notes=("anchor: 'payment_rail' (payments-distinctive) routes this off a churn catalog.",
               "a count / summed value — additive across entities and time."),
    ),
    # Y.2 — rail_scheme_diversity
    Template(
        id="rail_scheme_diversity", family="rail_mix",
        intent="Rail / scheme mix & diversity — the number of distinct payment_rails (and card schemes) a "
               "customer uses and how concentrated the mix is (HHI); measure=distinct_count / hhi.",
        needs=(Need("rail", "payment_rail"), Need("scheme", "scheme", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (90, 180, 365), "measure": ("distinct_count", "hhi")},
        aggregation="rail_scheme_diversity", additivity="n/a", explain="H",
        use_cases=("payments", "merchant_analytics", "segmentation"),
        pit=_PIT_TRAILING,
        degrade="no scheme tag -> compute rail-only diversity (still valid; note the narrower scope).",
        stage="mix",
        notes=("anchor: 'payment_rail' (payments-distinctive) routes this off a churn catalog.",
               "a mix / diversity index (distinct-count or HHI) — n/a (not summable)."),
    ),
    # Y.3 — purpose_code_diversity
    Template(
        id="purpose_code_diversity", family="purpose_mix",
        intent="Payment-purpose mix & diversity — the distinct ISO-20022 purpose codes (SALA/SUPP/…) a "
               "customer's payments carry and their concentration (HHI); a structured payments-context "
               "signal for analytics/segmentation.",
        needs=(Need("purpose_col", "iso20022_purpose_code"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 180, 365), "measure": ("distinct_count", "hhi")},
        aggregation="purpose_diversity", additivity="n/a", explain="H",
        use_cases=("payments", "merchant_analytics", "segmentation"),
        pit=_PIT_TRAILING,
        degrade="no iso20022_purpose_code -> SKIP.",
        stage="mix",
        notes=("anchor: 'iso20022_purpose_code' (payments-distinctive structured purpose) routes this off "
               "a churn catalog.",
               "a mix / diversity index — n/a (not summable)."),
    ),
    # ── ECONOMICS — interchange revenue + merchant discount rate ─────────────────────────────────────
    # Y.4 — interchange_revenue
    Template(
        id="interchange_revenue", family="economics",
        intent="Interchange economics — issuer interchange revenue earned on a customer's card "
               "transactions over the window (an additive economics flow).",
        needs=(Need("interchange_col", "interchange"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 30, 180), "measure": ("sum", "avg_per_txn")},
        aggregation="interchange_revenue", additivity="additive", explain="H",
        use_cases=("payments", "interchange_optimisation", "merchant_analytics"),
        pit=_PIT_TRAILING,
        degrade="no interchange flow -> SKIP.",
        stage="economics",
        eligibility="single currency — convert to base first.",
        notes=("anchor: 'interchange' (payments-distinctive economics flow) routes this off a churn "
               "catalog.",
               "OUTPUT additivity is measure-dependent: the interchange SUM is an additive flow; "
               "avg_per_txn is n/a — the default carries the additive sum."),
    ),
    # Y.5 — merchant_discount_economics
    Template(
        id="merchant_discount_economics", family="economics",
        intent="Merchant-discount economics — the effective merchant_discount_rate (MDR, the acquiring "
               "fee %) a merchant is charged, and its trend over the window; the acquiring-margin signal.",
        needs=(Need("mdr_col", "merchant_discount_rate"), Need("flow_col", "monetary_flow", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (90, 180, 365), "measure": ("level", "trend")},
        aggregation="mdr_economics", additivity="non_additive", explain="H",
        use_cases=("payments", "merchant_analytics", "interchange_optimisation"),
        pit=_PIT_TRAILING,
        degrade="no MDR reported -> SKIP.",
        stage="economics",
        notes=("anchor: 'merchant_discount_rate' (payments-distinctive) routes this off a churn catalog.",
               "an MDR RATE (or its trend) — non-additive; never sum or naively average across notionals."),
    ),
    # ── SETTLEMENT QUALITY — authorisation / chargeback / returns / timing ──────────────────────────
    # Y.6 — authorisation_decline_rate
    Template(
        id="authorisation_decline_rate", family="settlement_quality",
        intent="Authorisation / decline rate — the share of payment attempts that settle vs are "
               "declined/failed, read from settlement_status (pending/settled/failed/partial); a rising "
               "decline rate is a friction / risk signal.",
        needs=(Need("status_col", "settlement_status"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 30, 180), "measure": ("decline_rate", "approval_rate")},
        aggregation="auth_decline_rate", additivity="non_additive", explain="H",
        use_cases=("payments", "payments_ops", "merchant_analytics"),
        pit=_PIT_TRAILING,
        degrade="no settlement_status -> SKIP.",
        stage="settlement",
        notes=("anchor: 'settlement_status' (payments-distinctive) routes this off a churn catalog.",
               "an approval / decline RATE — non-additive; compute per entity, never sum."),
    ),
    # Y.7 — chargeback_dispute_rate (no chargeback concept -> scheme anchor + substitution)
    Template(
        id="chargeback_dispute_rate", family="settlement_quality",
        intent="Chargeback / dispute rate — the share of a merchant's (or cardholder's) card "
               "transactions that are charged back or disputed under the card scheme's rules over the "
               "window.",
        needs=(Need("scheme", "scheme"), Need("flow_col", "monetary_flow", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (180, 90, 365), "measure": ("count_rate", "value_rate")},
        aggregation="chargeback_rate", additivity="non_additive", explain="H",
        use_cases=("payments", "merchant_analytics", "fraud"),
        pit=_PIT_TRAILING,
        degrade="no card scheme -> SKIP (chargebacks are a card-scheme construct).",
        stage="settlement",
        derived=("is_chargeback := a scheme dispute / chargeback on the transaction — DERIVED downstream "
                 "(no data plane); the taxonomy has no dedicated chargeback concept.",),
        notes=("anchor: 'scheme' (payments-distinctive — card chargebacks are scheme-governed) routes "
               "this off a churn catalog.",
               "concept sub: no dedicated chargeback concept — the dispute/chargeback event is a "
               "declared downstream derivation scoped by the card scheme.",
               "a chargeback RATE — non-additive; compute per entity, never sum."),
    ),
    # Y.8 — return_payment_rate (direct-debit returns / standing-order failures)
    Template(
        id="return_payment_rate", family="settlement_quality",
        intent="Return / failed-payment rate — the share of direct-debit collections (or standing "
               "orders) that are RETURNED unpaid (NSF / mandate cancelled / no funds) over the window; a "
               "rising return rate is credit + operational stress.",
        needs=(Need("dd_col", "direct_debit"), Need("so_col", "standing_order", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (90, 180, 365), "measure": ("return_rate", "return_count")},
        aggregation="return_payment_rate", additivity="non_additive", explain="H",
        use_cases=("payments", "payments_ops", "collections"),
        pit=_PIT_TRAILING,
        degrade="no direct-debit / standing-order data -> SKIP.",
        stage="settlement",
        notes=("anchor: 'direct_debit' (payments-distinctive mandate + its return events) routes this off "
               "a churn catalog.",
               "OUTPUT additivity is measure-dependent: return_rate is a non-additive ratio; "
               "return_count is additive — the default carries the ratio case."),
    ),
    # Y.9 — settlement_lag (timing vs the T+n settlement_cycle convention)
    Template(
        id="settlement_lag", family="settlement_timing",
        intent="Settlement timing / lag — the mean settlement lag vs the rail's settlement_cycle "
               "convention (T+0/T+1/T+2) and the share settling late; PIT-critical (a fail is not "
               "knowable until T+n).",
        needs=(Need("cycle_col", "settlement_cycle"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 30, 180), "measure": ("mean_lag_days", "late_share")},
        aggregation="settlement_lag", additivity="n/a", explain="H",
        use_cases=("payments", "payments_ops"),
        pit=("trailing window (as_of − {window}, as_of]; PIT-CRITICAL — a settlement outcome is not "
             "KNOWABLE until T+n (settlement_cycle), so honour system_time. DESIGN-TIME declaration — "
             "no data plane enforces it."),
        degrade="no settlement_cycle convention -> SKIP.",
        stage="settlement",
        notes=("anchor: 'settlement_cycle' (payments-distinctive T+n convention) routes this off a churn "
               "catalog.",
               "OUTPUT additivity is measure-dependent: mean_lag_days is n/a (a duration); late_share is "
               "a non-additive ratio — the default carries the duration case."),
    ),
    # ── CROSS-BORDER — corridor mix + cross-border share (PROXY) ─────────────────────────────────────
    # Y.10 — corridor_cross_border_share
    Template(
        id="corridor_cross_border_share", family="corridor_mix",
        intent="Corridor mix / cross-border share — the share of a customer's payment value flowing "
               "through cross-border corridors (and the mix across them) over the window.",
        needs=(Need("corridor", "corridor"), Need("country", "country_code", optional=True),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 180, 365), "measure": ("cross_border_share", "corridor_hhi")},
        aggregation="corridor_cross_border", additivity="non_additive", explain="H",
        use_cases=("payments", "cross_border", "merchant_analytics"),
        pit=_PIT_TRAILING,
        degrade="no corridor -> SKIP.",
        stage="cross-border",
        eligibility=_PAYMENTS_CORRIDOR_PROXY,
        notes=("anchor: 'corridor' (payments-distinctive, PROXY) routes this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: cross_border_share / corridor_hhi are "
               "non-additive ratios — compute per entity, never sum."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The markets / trading templates — the §B8 risk families + the COUNTERPARTY-RISK funnel authored to
# Part-F depth (Phase-3 Pass-4, breadth).
#
# Positions/instruments, NOT customers. Two authoring disciplines are load-bearing (mirroring credit):
#   • ROUTING — every recipe REQUIRES a markets-distinctive, NON-STRUCTURAL concept (var / expected_shortfall
#     / pv01 / dv01 / implied_volatility / notional / expected_exposure / potential_future_exposure /
#     margin / limit / benchmark_rate / price / watchlist_hit_flag — NOT an entity concept like
#     instrument_id / book_id / netting_set_id / counterparty_id, which the engine's structural is_grain
#     scoring would bind onto ANY grain column, cross-surfacing the family). Grounding is the router; a
#     churn catalog with only generic monetary_stock/flow + as_of + customer_id grounds NOTHING here
#     (the locked invariant: ALL_TEMPLATES on the churn _CATALOG = exactly the churn lens).
#   • NON-ADDITIVE RISK + NEAR-LABEL — a VaR/ES/greek/PFE is a QUANTILE/greek: non-additive (sub-additive
#     with diversification), NEVER summed across books/netting sets; a notional is semi-additive (gross-
#     additive across positions, netted within a netting set); counts are additive. The counterparty-risk
#     funnel mirrors credit (HEALTHY → MARGIN PRESSURE → DISPUTE → CLOSE-OUT ⚠); a counterparty watchlist
#     hit BORDERS the close-out/default tail -> near_label=True + a ⚠ note (observe strictly pre-close-out;
#     no recipe ever Needs the default_flag/outcome_label leakage anchor — the engine refuses them).
# Markets data is MNPI / Chinese-wall aware (high model-risk tier for VaR/XVA). The Part-J appendix in
# docs/…/2026-07-08-banking-feature-template-library.md is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_MARKETS_PIT_STATE = ("point-in-time market / counterparty-risk STATE observed as-of: the latest metric "
                      "within (as_of − {window}, as_of], knowable strictly ≤ as_of, never forward. "
                      "DESIGN-TIME declaration — no data plane enforces runtime PIT.")
_MARKETS_NEAR_LABEL_PREFIX = ("⚠ NEAR-LABEL: observe the deterioration signal STRICTLY before the "
                              "counterparty close-out / default (never on/after it; window ≠ the label "
                              "window); the 3-part leakage control must FLAG it. ")
_MARKETS_MNPI = ("markets data is MNPI / Chinese-wall aware (high model-risk tier for VaR/XVA models) — "
                 "read-scoped by desk / information barrier where applicable.")

MARKETS_TEMPLATES: tuple[Template, ...] = (
    # ── Market-risk measures (§B8 point-in-time risk families) ──────────────────────────────────────
    # B8.1 — position_var_risk (VaR / ES level & trend)
    Template(
        id="position_var_risk", family="market_risk_measures",
        intent="Value-at-risk / expected-shortfall level or trailing trend for a trading book — the "
               "headline market-risk measure; a rising VaR/ES is escalating market risk.",
        needs=(Need("var_col", "var"), Need("es_col", "expected_shortfall", optional=True),
               Need("asof", "as_of_date"), Need("entity", "book_id")),
        params={"window": (90, 60, 30), "measure": ("level", "trend")},
        aggregation="var_risk", additivity="non_additive", explain="M",
        use_cases=("market_risk", "trading_risk", "frtb"),
        pit=_MARKETS_PIT_STATE,
        degrade="only a single VaR snapshot (no history) -> report the level only (no trend).",
        stage="risk-measures",
        eligibility="a VaR/ES is a QUANTILE — non-additive (sub-additive with diversification); never "
                    "sum across books/netting sets. " + _MARKETS_MNPI,
        notes=("anchor: 'var' (markets-distinctive, non-structural) routes this off a churn catalog.",
               "OUTPUT additivity is non_additive: a VaR quantile (or its slope) is never summed across "
               "books; 'expected_shortfall' is the FRTB twin (an acceptable alternate)."),
    ),
    # B8.2 — greek_sensitivity_exposure (PV01/DV01/vega)
    Template(
        id="greek_sensitivity_exposure", family="sensitivities_greeks",
        intent="Sensitivity / greek exposure (PV01 / DV01 / vega on implied-vol) for a book — the "
               "point-in-time market-sensitivity level or its trailing trend.",
        needs=(Need("pv01_col", "pv01"), Need("dv01_col", "dv01", optional=True),
               Need("vol_col", "implied_volatility", optional=True),
               Need("asof", "as_of_date"), Need("entity", "book_id")),
        params={"window": (90, 60, 30), "greek": ("pv01", "dv01", "vega"), "measure": ("level", "trend")},
        aggregation="greek_exposure", additivity="non_additive", explain="H",
        use_cases=("market_risk", "trading_risk", "irrbb"),
        pit=_MARKETS_PIT_STATE,
        degrade="only a single greek snapshot (no history) -> report the level only (no trend).",
        stage="risk-measures",
        eligibility="a greek is position-additive only WITHIN one risk factor — non-additive across "
                    "curves/tenors/underlyings; never sum naively. " + _MARKETS_MNPI,
        notes=("anchor: 'pv01' (markets-distinctive sensitivity, non-structural) routes this off a churn "
               "catalog.",
               "'dv01' is the dollar-sensitivity twin; 'implied_volatility' anchors a vega/vol-risk "
               "measure — non-additive across strikes/expiries."),
    ),
    # B8.3 — notional_netting_exposure (gross vs net notional by netting set)
    Template(
        id="notional_netting_exposure", family="notional_exposure",
        intent="Gross vs net notional exposure by netting set — signed notional netted within an ISDA "
               "netting set (measure=net_notional) or the gross sum across positions "
               "(measure=gross_notional).",
        needs=(Need("notional_col", "notional"), Need("direction", "position_direction", optional=True),
               Need("asof", "as_of_date"), Need("entity", "netting_set_id")),
        params={"window": (90, 180, 365), "measure": ("gross_notional", "net_notional")},
        aggregation="notional_exposure", additivity="semi_additive", explain="H",
        use_cases=("market_risk", "counterparty_risk", "exposure_management"),
        pit=_MARKETS_PIT_STATE,
        degrade="no position_direction -> gross notional only (cannot net long vs short; FLAG).",
        stage="exposure",
        eligibility="notional is SEMI-ADDITIVE: gross-additive across positions, NETTED within a netting "
                    "set, latest over snapshots — never sum a notional across dates. " + _MARKETS_MNPI,
        derived=("signed_notional := notional × sign(position_direction), netted within the "
                 "netting_set_id for measure=net_notional — computed DOWNSTREAM (no data plane).",),
        notes=("anchor: 'notional' (markets-distinctive, non-structural) routes this off a churn catalog "
               "(netting_set_id is an ENTITY concept — it would structurally bind any grain column, so it "
               "cannot be the sole anchor).",
               "semi_additive — gross-additive across positions, latest over time."),
    ),
    # ── Counterparty-risk funnel (§B8 — mirrors credit: MARGIN PRESSURE → DISPUTE → CLOSE-OUT ⚠) ─────
    # B8.4 — counterparty_exposure_trend (EPE / PFE)
    Template(
        id="counterparty_exposure_trend", family="counterparty_exposure",
        intent="Counterparty credit-exposure profile trend — expected (positive) exposure EPE or "
               "potential future exposure PFE over a trailing window; a rising profile is counterparty "
               "margin pressure.",
        needs=(Need("epe_col", "expected_exposure"),
               Need("pfe_col", "potential_future_exposure", optional=True),
               Need("asof", "as_of_date"), Need("entity", "netting_set_id")),
        params={"window": (180, 90, 365), "measure": ("epe_trend", "epe_level", "pfe_level")},
        aggregation="counterparty_exposure", additivity="non_additive", explain="M",
        use_cases=("counterparty_risk", "xva", "market_risk"),
        pit=_MARKETS_PIT_STATE,
        degrade="only a single exposure snapshot (no history) -> report the level only (no trend).",
        stage="1-margin-pressure",
        eligibility="EPE aggregates across netting sets SUB-additively and PFE is a QUANTILE — "
                    "non-additive; never sum EE/PFE naively across netting sets (like var). "
                    + _MARKETS_MNPI,
        notes=("anchor: 'expected_exposure' (markets-distinctive EPE, non-structural) routes this off a "
               "churn catalog.",
               "OUTPUT additivity is measure-dependent: a raw EPE level is a semi-additive exposure "
               "stock, a PFE level is a non-additive quantile, a trend is n/a — the default carries the "
               "non-additive case (never sum a counterparty quantile across netting sets)."),
    ),
    # B8.5 — margin_call_intensity (margin / collateral call intensity)
    Template(
        id="margin_call_intensity", family="margin_collateral",
        intent="Margin / collateral call intensity — the rate (or count) of variation/initial-margin "
               "calls on a netting set over the window, or the posted-margin level; rising calls are "
               "margin pressure into a dispute.",
        needs=(Need("margin_col", "margin"), Need("event_ts", "event_timestamp", optional=True),
               Need("asof", "as_of_date"), Need("entity", "netting_set_id")),
        params={"window": (90, 60, 30), "measure": ("call_intensity", "call_count", "im_level")},
        aggregation="margin_call_intensity", additivity="non_additive", explain="H",
        use_cases=("counterparty_risk", "margin", "market_risk"),
        pit=_MARKETS_PIT_STATE,
        degrade="no margin-call event stream -> report the posted-margin (im) level only.",
        stage="1-margin-pressure",
        eligibility="posted margin is a semi-additive collateral STOCK (sum across counterparties, "
                    "latest over time). " + _MARKETS_MNPI,
        notes=("anchor: 'margin' (markets-distinctive collateral stock, non-structural) routes this off "
               "a churn catalog.",
               "OUTPUT additivity is measure-dependent: call_intensity is a non-additive rate; a "
               "call_count is additive; an im_level is a semi-additive stock — the default carries the "
               "rate case."),
    ),
    # B8.6 — trading_limit_utilisation (limit-utilisation on trading limits)
    Template(
        id="trading_limit_utilisation", family="trading_limits",
        intent="Trading-limit utilisation — used exposure (notional) against a trading limit "
               "(measure=utilisation), the headroom, or proximity to a breach; rising utilisation is a "
               "limit-management early warning.",
        needs=(Need("limit_col", "limit"), Need("used_col", "notional", optional=True),
               Need("asof", "as_of_date"), Need("entity", "book_id")),
        params={"window": (90, 60, 30), "measure": ("utilisation", "headroom", "breach_proximity")},
        aggregation="limit_utilisation", additivity="non_additive", explain="H",
        use_cases=("market_risk", "trading_risk", "limit_management"),
        pit=_MARKETS_PIT_STATE,
        degrade="no used-exposure numerator (notional) -> report the limit level only (utilisation "
                "undefined; FLAG).",
        stage="risk-measures",
        eligibility="a utilisation ratio is non-additive; trading limits NEST (sub-limits under a "
                    "master) — never naively sum nested limits. " + _MARKETS_MNPI,
        notes=("anchor: 'limit' (markets/credit-distinctive ceiling, non-structural) routes this off a "
               "churn catalog.",
               "a utilisation ratio — non-additive; compute per book/limit, never sum."),
    ),
    # B8.7 — book_desk_concentration (concentration by book/desk)
    Template(
        id="book_desk_concentration", family="concentration",
        intent="Concentration of exposure by book / desk — an HHI (or top-share) of notional exposure "
               "across books/desks; a book concentrated in one risk is fragile.",
        needs=(Need("notional_col", "notional"), Need("desk", "desk_id", optional=True),
               Need("asof", "as_of_date"), Need("entity", "book_id")),
        params={"window": (90, 180, 365), "measure": ("book_hhi", "top_book_share")},
        aggregation="book_concentration", additivity="non_additive", explain="M",
        use_cases=("market_risk", "concentration_risk"),
        pit=_MARKETS_PIT_STATE,
        degrade="no desk breakdown -> compute book-level concentration only.",
        stage="risk-measures",
        eligibility=_MARKETS_MNPI,
        notes=("anchor: 'notional' (markets-distinctive exposure, non-structural) routes this off a "
               "churn catalog.",
               "a concentration index (HHI / top-share) — non-additive."),
    ),
    # B8.8 — benchmark_basis_dislocation (benchmark / tracking dislocation)
    Template(
        id="benchmark_basis_dislocation", family="benchmark_tracking",
        intent="Benchmark / basis dislocation — the spread of an instrument price or funding vs its "
               "reference benchmark_rate (SOFR/SONIA/€STR) and its trailing trend; a widening basis is "
               "market dislocation / tracking risk.",
        needs=(Need("benchmark_col", "benchmark_rate"), Need("price_col", "price", optional=True),
               Need("asof", "as_of_date"), Need("entity", "book_id")),
        params={"window": (90, 60, 180), "measure": ("basis_level", "basis_trend")},
        aggregation="benchmark_basis", additivity="non_additive", explain="M",
        use_cases=("market_risk", "trading_risk", "basis_risk"),
        pit=_MARKETS_PIT_STATE,
        degrade="only a single observation (no history) -> report the basis level only (no trend).",
        stage="risk-measures",
        eligibility=_MARKETS_MNPI,
        notes=("anchor: 'benchmark_rate' (markets-distinctive reference rate, non-structural) routes "
               "this off a churn catalog — distinct from the asset-management 'benchmark' INDEX and the "
               "deposit deposit_beta use of the same rate.",
               "a basis / spread vs the reference rate — non-additive."),
    ),
    # B8.9 — counterparty_deterioration_ewi (counterparty-deterioration early-warning — NEAR-LABEL)
    Template(
        id="counterparty_deterioration_ewi", family="counterparty_deterioration",
        intent="Counterparty-deterioration early-warning — a credit-watchlist (or adverse-media) hit on "
               "a trading counterparty and its recency; watchlisting borders the close-out / default "
               "tail of the counterparty-risk funnel.",
        needs=(Need("watchlist_col", "watchlist_hit_flag"),
               Need("adverse_media", "adverse_media_flag", optional=True),
               Need("asof", "as_of_date"), Need("entity", "counterparty_id")),
        params={"window": (365, 180, 90), "measure": ("watchlisted_flag", "days_since_watchlist")},
        aggregation="counterparty_deterioration", additivity="n/a", explain="H",
        use_cases=("counterparty_risk", "early_warning", "credit_risk"),
        pit=_MARKETS_PIT_STATE,
        degrade="no watchlist signal -> SKIP.",
        stage="3-close-out-adjacent",
        near_label=True,
        eligibility=_MARKETS_NEAR_LABEL_PREFIX + "a counterparty watchlist hit borders the close-out / "
                    "default label (the counterparty-risk funnel mirrors credit); adverse_media is pii "
                    "(read-scoped). " + _MARKETS_MNPI,
        notes=("anchor: 'watchlist_hit_flag' (near-label, markets-distinctive, non-structural) routes "
               "this off a churn catalog.",
               "borders the counterparty close-out/default outcome — observe strictly pre-close-out.",
               "a watchlist flag / recency — n/a."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The custody & securities-services templates — the §B10 SETTLEMENT-FAIL funnel authored to Part-F depth
# (Phase-3 Pass-4, breadth).
#
# Operational / asset-servicing; institutional; less PII. Funnel (§B10): TRADE BOOKED → MATCHING
# (unmatched) → PRE-SETTLEMENT (inventory/cash shortfall) → SETTLEMENT DATE → FAIL ⚠ → FAIL-AGING → BUY-IN.
# Two authoring disciplines are load-bearing:
#   • ROUTING — every recipe REQUIRES a custody-distinctive, NON-STRUCTURAL concept (settlement_status /
#     settlement_cycle / corporate_action / securities_loan / nav / custody_holding — NOT an entity
#     concept like account_id / instrument_id, which the engine's structural is_grain scoring would bind
#     onto any grain column). Grounding is the router; a churn catalog grounds NOTHING here (the locked
#     invariant: ALL_TEMPLATES on the churn _CATALOG = exactly the churn lens).
#   • SAFETY BY CONSTRUCTION — a settlement-fail-PREDICTION recipe is built from PRE-fail signals
#     (settlement_status pending/failed HISTORY, settlement_cycle T+n length, corporate_action
#     complexity), NEVER the ``settlement_fail`` outcome (a leakage anchor the engine refuses). A trailing
#     fail RATE and (harder) a POST-fail fail-ageing signal BORDER the fail outcome -> near_label=True + a
#     ⚠ note (observe strictly pre-outcome / on prior instructions). PIT-CRITICAL: a fail is not KNOWABLE
#     until T+n (settlement_cycle) — honour system_time. The Part-J appendix in
#     docs/…/2026-07-08-banking-feature-template-library.md is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_CUSTODY_PIT_STATE = ("point-in-time custody / settlement STATE observed as-of: the latest status / "
                      "holding within (as_of − {window}, as_of], knowable strictly ≤ as_of, never "
                      "forward. PIT-CRITICAL: a settlement outcome is not KNOWABLE until T+n "
                      "(settlement_cycle) — honour system_time. DESIGN-TIME declaration — no data plane "
                      "enforces runtime PIT.")
_CUSTODY_NEAR_LABEL_PREFIX = ("⚠ NEAR-LABEL: build the signal from PRE-fail observations STRICTLY before "
                              "the predicted instruction's settlement outcome (never the settlement_fail "
                              "label itself — the engine refuses it; window ≠ the label window); the "
                              "3-part leakage control must FLAG it. ")
_CUSTODY_PREFAIL = ("built from PRE-fail signals (settlement_status pending/failed history, "
                    "settlement_cycle length), NEVER the settlement_fail outcome (a leakage anchor the "
                    "engine refuses by construction).")

CUSTODY_TEMPLATES: tuple[Template, ...] = (
    # ── PRE-SETTLEMENT — matching + inventory aging (pre-fail signals) ───────────────────────────────
    # B10.1 — matching_break_rate (unmatched / mismatched at the matching stage)
    Template(
        id="matching_break_rate", family="matching",
        intent="Matching-break rate — the trailing share of instructions that were UNMATCHED / "
               "mismatched at the matching stage (read from settlement_status), an early pre-settlement "
               "break signal.",
        needs=(Need("status_col", "settlement_status"), Need("event_ts", "event_timestamp"),
               Need("entity", "account_id")),
        params={"window": (90, 30, 180), "measure": ("break_rate", "break_count")},
        aggregation="matching_break_rate", additivity="non_additive", explain="H",
        use_cases=("settlement_risk", "custody", "securities_services"),
        pit=_CUSTODY_PIT_STATE,
        degrade="no settlement_status (matching state) -> SKIP.",
        stage="matching",
        eligibility=_CUSTODY_PREFAIL,
        notes=("anchor: 'settlement_status' (custody-distinctive, non-structural) routes this off a "
               "churn catalog.",
               "concept sub: the taxonomy has no dedicated matching_status concept — settlement_status "
               "carries the unmatched/mismatched value.",
               "OUTPUT additivity is measure-dependent: break_rate is a non-additive ratio; a "
               "break_count is additive — the default carries the rate case."),
    ),
    # B10.2 — pre_settlement_aging (pending instructions aging vs the T+n settlement_cycle)
    Template(
        id="pre_settlement_aging", family="pre_settlement",
        intent="Pre-settlement aging — how long unsettled/pending instructions have aged against their "
               "T+n settlement_cycle convention (a pre-fail inventory/cash-shortfall signal); a "
               "lengthening pending age is fail risk BEFORE the settlement date.",
        needs=(Need("cycle_col", "settlement_cycle"),
               Need("status_col", "settlement_status", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "account_id")),
        params={"window": (30, 90, 180), "measure": ("mean_pending_age", "overdue_share")},
        aggregation="pre_settlement_aging", additivity="n/a", explain="H",
        use_cases=("settlement_risk", "custody", "securities_services"),
        pit=_CUSTODY_PIT_STATE,
        degrade="no settlement_cycle convention -> SKIP (cannot age against T+n).",
        stage="pre-settlement",
        eligibility=_CUSTODY_PREFAIL,
        notes=("anchor: 'settlement_cycle' (custody-distinctive T+n convention, non-structural) routes "
               "this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: mean_pending_age is a duration (n/a); an "
               "overdue_share is a non-additive ratio — the default carries the duration case."),
    ),
    # ── SETTLEMENT DATE → FAIL ⚠ — the fail rate (NEAR-LABEL; pre-fail history, never settlement_fail) ─
    # B10.3 — settlement_fail_rate (the headline safety recipe)
    Template(
        id="settlement_fail_rate", family="settlement_fail",
        intent="Settlement-fail rate — the trailing share of an account's / counterparty's instructions "
               "that reached a FAILED settlement_status vs settled, built from historical status (a "
               "pre-fail predictor for a NEW instruction); NEVER the settlement_fail label itself.",
        needs=(Need("status_col", "settlement_status"),
               Need("cycle_col", "settlement_cycle", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "account_id")),
        params={"window": (90, 180, 365), "measure": ("fail_rate", "fail_count")},
        aggregation="settlement_fail_rate", additivity="non_additive", explain="H",
        use_cases=("settlement_risk", "custody", "securities_services"),
        pit=_CUSTODY_PIT_STATE,
        degrade="no settlement_status history -> SKIP (cannot compute a fail rate).",
        stage="fail",
        near_label=True,
        eligibility=_CUSTODY_NEAR_LABEL_PREFIX + _CUSTODY_PREFAIL,
        notes=("anchor: 'settlement_status' (custody-distinctive; the historical status distribution, "
               "NOT the settlement_fail label) routes this off a churn catalog.",
               "the settlement_fail outcome is NEVER an input — the engine refuses the leakage anchor; "
               "the fail RATE is a pre-observation over PRIOR instructions.",
               "OUTPUT additivity is measure-dependent: fail_rate is a non-additive ratio; a fail_count "
               "is additive — the default carries the rate case."),
    ),
    # B10.4 — fail_ageing_buckets (POST-fail aging on the fail→buy-in tail — NEAR-LABEL)
    Template(
        id="fail_ageing_buckets", family="fail_ageing",
        intent="Fail-ageing buckets — how long already-FAILED instructions have been aging "
               "(measure=aged_fail_share in buckets, or mean fail_age_days), a POST-fail asset-servicing "
               "signal on the fail → buy-in tail.",
        needs=(Need("status_col", "settlement_status"),
               Need("cycle_col", "settlement_cycle", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "account_id")),
        params={"window": (90, 180, 30), "measure": ("aged_fail_share", "mean_fail_age_days")},
        aggregation="fail_ageing", additivity="non_additive", explain="H",
        use_cases=("settlement_risk", "custody", "securities_services"),
        pit=_CUSTODY_PIT_STATE,
        degrade="no settlement_status fail history -> SKIP.",
        stage="fail-ageing",
        near_label=True,
        eligibility=_CUSTODY_NEAR_LABEL_PREFIX + "fail-ageing is a POST-fail signal (the fail → buy-in "
                    "tail) — for a fail-PREDICTION model observe it on PRIOR/other instructions, never "
                    "the target instruction's own post-fail age. " + _CUSTODY_PREFAIL,
        notes=("anchor: 'settlement_status' (custody-distinctive; the failed-status aging, NOT the "
               "settlement_fail label) routes this off a churn catalog.",
               "borders/reads the fail outcome (POST-fail) — like a collections post-charge-off signal; "
               "observe strictly on pre-label observations for a prediction model.",
               "OUTPUT additivity is measure-dependent: aged_fail_share is a non-additive ratio; "
               "mean_fail_age_days is a duration (n/a) — the default carries the ratio case."),
    ),
    # ── ASSET-SERVICING — corporate actions, securities lending, NAV, custody holdings ───────────────
    # B10.5 — corporate_action_complexity (volume / complexity)
    Template(
        id="corporate_action_complexity", family="corporate_actions",
        intent="Corporate-action volume / complexity — the count of corporate-action events an account "
               "must service (measure=ca_volume) or a complexity / elective-deadline-proximity score "
               "(measure=complexity_score); elective/complex CAs drive asset-servicing risk.",
        needs=(Need("ca_col", "corporate_action"), Need("pay_date", "pay_date", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "account_id")),
        params={"window": (90, 180, 365), "measure": ("ca_volume", "complexity_score")},
        aggregation="corporate_action_complexity", additivity="additive", explain="H",
        use_cases=("custody", "securities_services", "corporate_actions"),
        pit=_CUSTODY_PIT_STATE,
        degrade="no corporate_action data -> SKIP.",
        stage="asset-servicing",
        eligibility="entitlement is fixed at record_date, priced at ex_date, paid at pay_date — honour "
                    "the corporate-action PIT (system_time for restated terms).",
        notes=("anchor: 'corporate_action' (custody-distinctive, non-structural) routes this off a churn "
               "catalog.",
               "OUTPUT additivity is measure-dependent: ca_volume is an additive count; a "
               "complexity_score is non-additive — the default carries the additive count."),
    ),
    # B10.6 — sec_lending_utilisation (securities-lending utilisation / specials)
    Template(
        id="sec_lending_utilisation", family="securities_lending",
        intent="Securities-lending utilisation — on-loan securities (securities_loan) vs the lendable "
               "custody inventory (measure=utilisation), or the on-loan amount / specials demand; high "
               "utilisation signals recall risk.",
        needs=(Need("loan_col", "securities_loan"), Need("holding_col", "custody_holding", optional=True),
               Need("asof", "as_of_date"), Need("entity", "instrument_id")),
        params={"window": (90, 180, 365), "measure": ("utilisation", "on_loan_amount")},
        aggregation="sec_lending_utilisation", additivity="non_additive", explain="H",
        use_cases=("securities_services", "securities_lending", "custody"),
        pit=_CUSTODY_PIT_STATE,
        degrade="no lendable-inventory base (custody_holding) -> report the on_loan_amount "
                "(semi-additive stock) only.",
        stage="asset-servicing",
        eligibility="single currency; a securities-loan position is a semi-additive STOCK (latest over "
                    "time).",
        notes=("anchor: 'securities_loan' (custody-distinctive SFT position, non-structural) routes this "
               "off a churn catalog.",
               "OUTPUT additivity is measure-dependent: utilisation is a non-additive ratio; the "
               "on_loan_amount is a semi-additive stock — the default carries the ratio case."),
    ),
    # B10.7 — nav_strike_timeliness (fund-admin NAV striking — PIT on record/pay dates)
    Template(
        id="nav_strike_timeliness", family="fund_admin_nav",
        intent="NAV-strike timeliness / exception rate — whether fund NAVs are struck on time and clean "
               "(measure=exception_rate / late_share), read against the corporate-action record/pay PIT; "
               "a rising NAV-exception rate is a fund-admin quality signal.",
        needs=(Need("nav_col", "nav"), Need("record_date", "record_date", optional=True),
               Need("pay_date", "pay_date", optional=True), Need("event_ts", "event_timestamp"),
               Need("entity", "account_id")),
        params={"window": (90, 30, 180), "measure": ("exception_rate", "late_share")},
        aggregation="nav_strike_timeliness", additivity="non_additive", explain="H",
        use_cases=("securities_services", "fund_administration", "custody"),
        pit=_CUSTODY_PIT_STATE,
        degrade="no nav strike history -> SKIP.",
        stage="fund-admin",
        eligibility="honour the corporate-action PIT (entitlement fixed at record_date, priced at "
                    "ex_date, paid at pay_date) so a NAV is struck on knowable data.",
        notes=("anchor: 'nav' (custody/fund-admin-distinctive price, non-structural) routes this off a "
               "churn catalog.",
               "a NAV-exception / late-strike RATE — non-additive; compute per fund/account, never sum."),
    ),
    # B10.8 — custody_holding_dynamics (custody-holding turnover / concentration)
    Template(
        id="custody_holding_dynamics", family="custody_holdings",
        intent="Custody-holding dynamics — assets-under-custody holding level / trend "
               "(measure=holding_trend), turnover (traded value / holdings) or concentration (HHI across "
               "holdings); the custody-book stability signal.",
        needs=(Need("holding_col", "custody_holding"), Need("instrument", "instrument_id", optional=True),
               Need("asof", "as_of_date"), Need("entity", "account_id")),
        params={"window": (90, 180, 365), "measure": ("holding_trend", "turnover", "concentration_hhi")},
        aggregation="custody_holding_dynamics", additivity="semi_additive", explain="M",
        use_cases=("custody", "securities_services"),
        pit=_CUSTODY_PIT_STATE,
        degrade="no instrument breakdown -> account-level holding level/trend only (no concentration).",
        stage="asset-servicing",
        eligibility="single currency; a custody holding is a semi-additive STOCK (sum across accounts, "
                    "latest over time — never sum across dates).",
        notes=("anchor: 'custody_holding' (custody-distinctive AUC stock, non-structural) routes this "
               "off a churn catalog.",
               "OUTPUT additivity is measure-dependent: a holding level/trend is a semi-additive stock; "
               "turnover / concentration_hhi are non-additive — the default carries the semi-additive "
               "holding case."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The asset-management (buy-side) templates — the §B12 REDEMPTION funnel + mandate compliance authored
# to Part-F depth (Phase-3 Pass-4, breadth).
#
# Funds/mandates, driven by RELATIVE PERFORMANCE + LIQUIDITY. Funnel (§B12, mirrors churn): INVESTED →
# DISENGAGEMENT → REDEMPTION-RISK (underperformance, partial redemptions) → REDEMPTION NOTICE ⚠ → REDEEMED.
# Two authoring disciplines are load-bearing:
#   • ROUTING — every recipe REQUIRES an asset-management-distinctive, NON-STRUCTURAL concept (fund_flow /
#     benchmark / tracking_error / expense_ratio / mandate / nav — NOT an entity concept like fund /
#     share_class, which the engine's structural is_grain scoring would bind onto any grain column).
#     Grounding is the router; a churn catalog grounds NOTHING here (the locked invariant).
#   • SAFETY BY CONSTRUCTION — a redemption recipe is built from fund_flow / performance / tracking-error
#     PRE-signals, NEVER the ``redeemed`` outcome (a leakage anchor the engine refuses). The NEAR-LABEL tail
#     is mandate compliance: a mandate-breach headroom and a tracking-error-limit proximity BORDER the
#     mandate/IMA-breach label -> near_label=True + a ⚠ note (observe strictly pre-breach). Distinguish
#     'mandate' (the INVESTMENT mandate / IMA) from a PAYMENT mandate (direct_debit / standing_order), and
#     'benchmark' (a performance INDEX) from 'benchmark_rate' (a reference INTEREST rate). The Part-J
#     appendix in docs/…/2026-07-08-banking-feature-template-library.md is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_AM_PIT_STATE = ("point-in-time asset-management STATE observed as-of: the latest performance / ratio "
                 "within (as_of − {window}, as_of], knowable strictly ≤ as_of, never forward. "
                 "DESIGN-TIME declaration — no data plane enforces runtime PIT.")
_AM_PIT_TRAILING = ("trailing window (as_of − {window}, as_of], values knowable strictly ≤ as_of; never "
                    "forward. DESIGN-TIME declaration — no data plane enforces runtime PIT.")
_AM_NEAR_LABEL_PREFIX = ("⚠ NEAR-LABEL: observe the pre-breach signal STRICTLY before the "
                         "mandate / tracking-error breach label (never on/after it; window ≠ the label "
                         "window); the 3-part leakage control must FLAG it. ")
_AM_NOT_REDEEMED = ("built from fund_flow / performance / tracking-error PRE-signals, NEVER the "
                    "'redeemed' outcome (a leakage anchor the engine refuses by construction).")

ASSET_MGMT_TEMPLATES: tuple[Template, ...] = (
    # ── Investor-flow / redemption funnel (§B12 — mirrors churn) ─────────────────────────────────────
    # B12.1 — net_fund_flow_trend (net flow & redemption pressure — from fund_flow, NEVER redeemed)
    Template(
        id="net_fund_flow_trend", family="redemption_flow",
        intent="Net fund-flow trend & redemption pressure — cumulative net flow (subscriptions − "
               "redemptions), its trailing trend, or a redemption-pressure ratio (gross redemptions / "
               "AUM); sustained net outflows are the buy-side attrition signal. Built from fund_flow, "
               "NEVER 'redeemed'.",
        needs=(Need("flow_col", "fund_flow"), Need("event_ts", "event_timestamp"),
               Need("entity", "fund")),
        params={"window": (90, 180, 365),
                "measure": ("cumulative_net_flow", "net_flow_trend", "redemption_pressure")},
        aggregation="net_fund_flow", additivity="additive", explain="H",
        use_cases=("redemption_risk", "fund_flows", "asset_management"),
        pit=_AM_PIT_TRAILING,
        degrade="only a single flow snapshot (no history) -> report the cumulative net flow (no trend).",
        stage="2-disengagement",
        eligibility="single currency — convert to base first. " + _AM_NOT_REDEEMED,
        notes=("anchor: 'fund_flow' (asset-management-distinctive net-flow, non-structural) routes this "
               "off a churn catalog.",
               "OUTPUT additivity is measure-dependent: the cumulative net flow is an additive flow; a "
               "trend is n/a; a redemption_pressure ratio is non-additive — the default carries the "
               "additive flow (the safe pre-redemption signal, NOT the 'redeemed' label)."),
    ),
    # B12.2 — performance_vs_benchmark (relative performance dispersion drives outflows)
    Template(
        id="performance_vs_benchmark", family="relative_performance",
        intent="Relative performance vs benchmark — active return / return dispersion of a fund vs its "
               "benchmark index (measure=relative_return / return_dispersion / underperformance_flag); "
               "underperformance vs benchmark drives outflows.",
        needs=(Need("benchmark_col", "benchmark"), Need("te_col", "tracking_error", optional=True),
               Need("nav_col", "nav", optional=True), Need("asof", "as_of_date"),
               Need("entity", "fund")),
        params={"window": (365, 180, 90),
                "measure": ("relative_return", "return_dispersion", "underperformance_flag")},
        aggregation="relative_performance", additivity="non_additive", explain="H",
        use_cases=("redemption_risk", "fund_performance", "asset_management"),
        pit=_AM_PIT_STATE,
        degrade="no benchmark series -> SKIP (relative performance undefined).",
        stage="3-redemption-risk",
        eligibility=_AM_NOT_REDEEMED,
        notes=("anchor: 'benchmark' (asset-management-distinctive INDEX — NOT the markets benchmark_rate "
               "reference rate — non-structural) routes this off a churn catalog.",
               "a relative return / dispersion — non-additive; compute per fund/share-class, never sum."),
    ),
    # B12.3 — share_class_flow_mix (flow mix across share classes / distribution)
    Template(
        id="share_class_flow_mix", family="distribution_mix",
        intent="Share-class flow mix — how a fund's net flows split across share classes / distribution "
               "(institutional vs retail vs platform) and how concentrated the mix is "
               "(measure=institutional_flow_share / flow_hhi); a flighty distribution mix is run risk.",
        needs=(Need("flow_col", "fund_flow"), Need("share_class", "share_class"),
               Need("event_ts", "event_timestamp")),
        params={"window": (90, 180, 365), "measure": ("institutional_flow_share", "flow_hhi")},
        aggregation="share_class_flow_mix", additivity="non_additive", explain="M",
        use_cases=("redemption_risk", "distribution", "asset_management"),
        pit=_AM_PIT_TRAILING,
        degrade="no share-class breakdown -> fund-level net flow only (no mix).",
        stage="2-disengagement",
        eligibility="single currency. " + _AM_NOT_REDEEMED,
        notes=("anchor: 'fund_flow' (asset-management-distinctive flow, non-structural) routes this off a "
               "churn catalog (share_class is an ENTITY concept — it would structurally bind any grain "
               "column, so it cannot be the sole anchor).",
               "a flow mix / concentration (share / HHI) — non-additive; the underlying flows are "
               "additive."),
    ),
    # B12.4 — redemption_liquidity_coverage (open-ended-fund run-risk mismatch — from fund_flow)
    Template(
        id="redemption_liquidity_coverage", family="liquidity_coverage",
        intent="Redemption liquidity coverage — liquid assets vs trailing / expected redemptions "
               "(measure=coverage_ratio), or redemption velocity; a coverage mismatch is open-ended-fund "
               "run risk. Built from fund_flow, NEVER 'redeemed'.",
        needs=(Need("flow_col", "fund_flow"), Need("liquid_col", "monetary_stock", optional=True),
               Need("asof", "as_of_date"), Need("event_ts", "event_timestamp", optional=True),
               Need("entity", "fund")),
        params={"window": (90, 30, 180), "measure": ("coverage_ratio", "redemption_velocity")},
        aggregation="redemption_liquidity_coverage", additivity="non_additive", explain="M",
        use_cases=("redemption_risk", "fund_liquidity", "asset_management"),
        pit=_AM_PIT_STATE,
        degrade="no liquid-asset base -> report the redemption velocity only (no coverage ratio).",
        stage="3-redemption-risk",
        eligibility="single currency. " + _AM_NOT_REDEEMED,
        notes=("anchor: 'fund_flow' (asset-management-distinctive redemption flow, non-structural) routes "
               "this off a churn catalog.",
               "a coverage ratio / velocity — non-additive; the underlying redemption flow is additive."),
    ),
    # B12.5 — aum_stability (AUM level / trend / volatility — nav)
    Template(
        id="aum_stability", family="aum_stability",
        intent="AUM stability — fund assets-under-management (NAV × units, or a fund AUM stock) level / "
               "trend / volatility over the window (measure=aum_level / aum_trend / aum_volatility); an "
               "unstable AUM base is redemption/liquidity risk.",
        needs=(Need("nav_col", "nav"), Need("aum_col", "monetary_stock", optional=True),
               Need("asof", "as_of_date"), Need("entity", "fund")),
        params={"window": (365, 180, 90), "measure": ("aum_level", "aum_trend", "aum_volatility")},
        aggregation="aum_stability", additivity="semi_additive", explain="M",
        use_cases=("redemption_risk", "aum_stability", "asset_management"),
        pit=_AM_PIT_STATE,
        degrade="only a single NAV/AUM snapshot (no history) -> report the AUM level (no trend/vol).",
        stage="1-invested",
        eligibility="single currency; a fund AUM is a semi-additive STOCK (sum across funds, latest over "
                    "time — never sum across dates). " + _AM_NOT_REDEEMED,
        notes=("anchor: 'nav' (asset-management-distinctive net asset value, non-structural) routes this "
               "off a churn catalog.",
               "OUTPUT additivity is measure-dependent: a fund AUM level is a semi-additive stock; a "
               "trend is n/a; volatility is non-additive — the default carries the semi-additive AUM "
               "stock."),
    ),
    # ── Mandate / portfolio compliance (§B12 — NEAR-LABEL breach paths) ──────────────────────────────
    # B12.6 — tracking_error_breach_proximity (active-risk vs the TE limit — NEAR-LABEL)
    Template(
        id="tracking_error_breach_proximity", family="tracking_error",
        intent="Tracking-error breach proximity — the active-risk (tracking_error) level and its "
               "proximity to the mandate's tracking-error limit (measure=te_level / breach_proximity / "
               "breach_flag); a rising TE toward its cap borders a mandate breach.",
        needs=(Need("te_col", "tracking_error"), Need("asof", "as_of_date"), Need("entity", "fund")),
        params={"window": (365, 180, 90), "measure": ("te_level", "breach_proximity", "breach_flag")},
        aggregation="tracking_error", additivity="non_additive", explain="H",
        use_cases=("mandate_compliance", "tracking_error", "asset_management"),
        pit=_AM_PIT_STATE,
        stage="3-redemption-risk",
        near_label=True,
        eligibility=_AM_NEAR_LABEL_PREFIX + "a tracking-error-limit breach borders the mandate / IMA "
                    "breach label. " + _AM_NOT_REDEEMED,
        notes=("anchor: 'tracking_error' (asset-management-distinctive active risk, non-structural) "
               "routes this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: te_level / breach_proximity are non-additive; "
               "breach_flag is n/a — the default carries the non-additive case.",
               "borders the tracking-error/mandate breach label — observe strictly pre-breach."),
    ),
    # B12.7 — mandate_breach_proximity (pre-breach headroom to an IMA limit — NEAR-LABEL)
    Template(
        id="mandate_breach_proximity", family="mandate_compliance",
        intent="Mandate-breach proximity — the headroom to a fund's investment-mandate (IMA) limit "
               "(sector/issuer/rating/concentration) and its trend (measure=headroom / breach_proximity "
               "/ breached_flag); a shrinking headroom is a pre-breach compliance signal.",
        needs=(Need("mandate_col", "mandate"), Need("asof", "as_of_date"), Need("entity", "fund")),
        params={"window": (90, 180, 365), "measure": ("headroom", "breach_proximity", "breached_flag")},
        aggregation="mandate_headroom", additivity="non_additive", explain="H",
        use_cases=("mandate_compliance", "portfolio_risk", "asset_management"),
        pit=_AM_PIT_STATE,
        stage="mandate-compliance",
        near_label=True,
        eligibility=_AM_NEAR_LABEL_PREFIX + "a shrinking headroom borders the mandate-breach label. "
                    + _AM_NOT_REDEEMED,
        notes=("anchor: 'mandate' (asset-management-distinctive IMA — the INVESTMENT mandate, NOT a "
               "PAYMENT mandate like direct_debit/standing_order — non-structural) routes this off a "
               "churn catalog.",
               "OUTPUT additivity is measure-dependent: headroom / breach_proximity are non-additive; "
               "breached_flag is n/a — the default carries the non-additive case.",
               "borders the mandate-breach label — observe strictly pre-breach."),
    ),
    # B12.8 — expense_ratio_competitiveness (fee competitiveness vs peers)
    Template(
        id="expense_ratio_competitiveness", family="fee_competitiveness",
        intent="Expense-ratio competitiveness — the fund's expense ratio (TER/OCF) level, its trend, or "
               "its gap vs a peer group (measure=ter_level / ter_trend / ter_vs_peer); an uncompetitive "
               "TER drives redemptions.",
        needs=(Need("ter_col", "expense_ratio"), Need("peer", "peer_group", optional=True),
               Need("asof", "as_of_date"), Need("entity", "fund")),
        params={"window": (365, 180, 90), "measure": ("ter_level", "ter_trend", "ter_vs_peer")},
        aggregation="expense_ratio", additivity="non_additive", explain="H",
        use_cases=("redemption_risk", "pricing", "asset_management"),
        pit=_AM_PIT_STATE,
        degrade="no peer group -> report the TER level / trend only (no peer gap).",
        stage="2-disengagement",
        eligibility=_AM_NOT_REDEEMED,
        notes=("anchor: 'expense_ratio' (asset-management-distinctive TER/OCF, non-structural) routes "
               "this off a churn catalog.",
               "a TER ratio (or its trend / peer-gap) — non-additive; never sum across funds."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The full template REGISTRY — every family, in author order. Future template passes EXTEND this tuple;
# gate1 grounds ALL_TEMPLATES so a family surfaces only where its distinctive concepts exist in the
# catalog (grounding is the router). RETAIL_CHURN_TEMPLATES stays a standalone name because gate1 + the
# pilot tests still import it directly.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
ALL_TEMPLATES: tuple[Template, ...] = (
    RETAIL_CHURN_TEMPLATES + CREDIT_RISK_TEMPLATES + FRAUD_TEMPLATES + AML_TEMPLATES
    + COLLECTIONS_TEMPLATES + DEPOSITS_TEMPLATES + PAYMENTS_TEMPLATES
    + MARKETS_TEMPLATES + CUSTODY_TEMPLATES + ASSET_MGMT_TEMPLATES)


def _validate_family(templates: tuple[Template, ...], label: str, seen_ids: set[str]) -> None:
    """Per-need concept-existence + param-shape checks for one family, accumulating each id into
    ``seen_ids`` so the caller can enforce id-uniqueness at whatever scope it passes the set."""
    for t in templates:
        if t.id in seen_ids:
            raise ValueError(f"duplicate template id {t.id!r} (checking {label})")
        seen_ids.add(t.id)
        for need in t.needs:
            if need.concept not in CONCEPT_REGISTRY:
                raise ValueError(
                    f"template {t.id!r} need {need.role!r} references unknown concept {need.concept!r}")
        for key, allowed in t.params.items():
            if not isinstance(allowed, tuple) or not allowed:
                raise ValueError(f"template {t.id!r} param {key!r} must be a non-empty tuple")


def _validate_registry() -> None:
    """Fail fast at import if a template drifts from the concept registry / schema invariants.

    Two passes: (1) each family on its own (kept intact — gate1 + the family tests import them directly);
    (2) the combined ALL_TEMPLATES registry with a GLOBAL id-uniqueness check — no two templates in ANY
    family may share an id — plus the same per-need concept-existence + param-shape checks for every
    family."""
    _validate_family(RETAIL_CHURN_TEMPLATES, "RETAIL_CHURN_TEMPLATES", set())
    _validate_family(CREDIT_RISK_TEMPLATES, "CREDIT_RISK_TEMPLATES", set())
    _validate_family(FRAUD_TEMPLATES, "FRAUD_TEMPLATES", set())
    _validate_family(AML_TEMPLATES, "AML_TEMPLATES", set())
    _validate_family(COLLECTIONS_TEMPLATES, "COLLECTIONS_TEMPLATES", set())
    _validate_family(DEPOSITS_TEMPLATES, "DEPOSITS_TEMPLATES", set())
    _validate_family(PAYMENTS_TEMPLATES, "PAYMENTS_TEMPLATES", set())
    _validate_family(MARKETS_TEMPLATES, "MARKETS_TEMPLATES", set())
    _validate_family(CUSTODY_TEMPLATES, "CUSTODY_TEMPLATES", set())
    _validate_family(ASSET_MGMT_TEMPLATES, "ASSET_MGMT_TEMPLATES", set())
    _validate_family(ALL_TEMPLATES, "ALL_TEMPLATES", set())


_validate_registry()
