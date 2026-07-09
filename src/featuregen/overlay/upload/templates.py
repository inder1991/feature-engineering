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
# The full template REGISTRY — every family, in author order. Future template passes (fraud, AML,
# collections, …) EXTEND this tuple; gate1 grounds ALL_TEMPLATES so a family surfaces only where its
# distinctive concepts exist in the catalog (grounding is the router). RETAIL_CHURN_TEMPLATES stays a
# standalone name because gate1 + the pilot tests still import it directly.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
ALL_TEMPLATES: tuple[Template, ...] = RETAIL_CHURN_TEMPLATES + CREDIT_RISK_TEMPLATES


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

    Two passes: (1) the churn pilot on its own (kept intact — gate1 + tests import it directly); (2) the
    combined ALL_TEMPLATES registry with a GLOBAL id-uniqueness check — no two templates in ANY family may
    share an id — plus the same per-need concept-existence + param-shape checks for every family."""
    _validate_family(RETAIL_CHURN_TEMPLATES, "RETAIL_CHURN_TEMPLATES", set())
    _validate_family(ALL_TEMPLATES, "ALL_TEMPLATES", set())


_validate_registry()
