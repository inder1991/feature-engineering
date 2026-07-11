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

from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
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
    # ── 3B.1 cross-catalog binding metadata (optional; need_metadata derives the unset ones) ──
    allowed_source_grains: tuple[str, ...] = ()   # acceptable source grains; () = unconstrained
    join_role: JoinRole | None = None             # explicit override; None -> derived (NEVER tuple position)
    temporal_role: TemporalRole | None = None     # explicit override; None -> derived from concept.pit_role


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
    # ── 3B.1 explicit source anchor (optional; needed only when >1 distinct entity-linked need) ──
    source_entity: str | None = None            # the recipe's source grain entity (derived when unambiguous)
    source_entity_need_role: str | None = None   # which need carries the source key


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
        source_entity_need_role="entity",   # 3B.1: customer is the source grain (beneficiary is related)
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
        source_entity_need_role="entity",   # 3B.1: customer is the source grain (counterparty/beneficiary related)
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
        source_entity_need_role="entity",   # 3B.1: customer is the source grain (wallet is related)
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
        source_entity_need_role="entity",   # 3B.1: customer is the source grain (alert/case are related)
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
        source_entity_need_role="entity",   # 3B.1: book is the source grain (desk is related)
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
        source_entity_need_role="entity",   # 3B.1: account is the source grain (instrument is related)
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
# The insurance / bancassurance templates — the §B9 LAPSE funnel + CLAIMS-FRAUD journey authored to
# Part-F depth (Phase-3 Pass-5, breadth).
#
# Two journeys. Lapse/persistency funnel (mirrors churn): ACTIVE → DISENGAGEMENT → ARREARS → SURRENDER
# REQUEST ⚠ → LAPSED. Claims-fraud journey: INCEPTION → CLAIM EVENT → FILED → INVESTIGATION → SETTLE/DENY.
# Two authoring disciplines are load-bearing (mirroring credit):
#   • ROUTING — every recipe REQUIRES an insurance-distinctive, NON-STRUCTURAL concept (premium /
#     surrender_value / claim_reserve / sum_assured / reinsurance_recoverable / mortality_morbidity — NOT
#     an entity concept like policy_id / claim_id / customer_id, which the engine's structural is_grain
#     scoring would bind onto ANY grain column, cross-surfacing the family). Grounding is the router; a
#     churn catalog with only generic monetary_stock/flow + as_of + customer_id grounds NOTHING here (the
#     locked invariant: ALL_TEMPLATES on the churn _CATALOG = exactly the churn lens).
#   • SAFETY BY CONSTRUCTION — a LAPSE / SURRENDER-prediction recipe is built from PRE-lapse signals
#     (premium-payment irregularity, missed-premium streak, surrender-value trend), NEVER the ``lapsed`` /
#     ``surrendered`` outcome (leakage anchors the engine refuses by construction). A claims-fraud typology
#     is built from claim BEHAVIOUR (early-claim, over-servicing), NEVER ``fraud_flag`` — and because it
#     BORDERS the SIU/confirmed-fraud label it is near_label=True + a ⚠ note. Sensitivity: mortality_
#     morbidity is health-ADJACENT (the actuarial RATE is bindable; an individual's health STATUS is a
#     special_category column the engine blocks). The Part-K appendix in
#     docs/…/2026-07-08-banking-feature-template-library.md is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_INSURANCE_PIT_STATE = ("point-in-time policy STATE observed as-of: the latest reserve / value / rate "
                        "within (as_of − {window}, as_of], knowable strictly ≤ as_of, never forward. "
                        "DESIGN-TIME declaration — no data plane enforces runtime PIT.")
_INSURANCE_SINGLE_CCY = ("single currency — convert to base first; a reserve / sum-assured / recoverable "
                         "is a STOCK (latest over time, never summed across dates).")
_PREMIUM_WRITTEN_EARNED = ("premiums are additive but mind the WRITTEN-vs-EARNED trap (never sum written "
                           "AND earned for one period — UPR bridges them; see the 'premium' concept note).")
_LAPSE_PRELAPSE = ("built from PRE-lapse signals (premium-payment irregularity, missed-premium streak, "
                   "surrender-value trend), NEVER the 'lapsed' / 'surrendered' outcome (leakage anchors "
                   "the engine refuses by construction).")
_CLAIMS_NEAR_LABEL_PREFIX = ("⚠ NEAR-LABEL: build the claims-fraud typology from claim BEHAVIOUR observed "
                             "STRICTLY before the SIU / confirmed-fraud (repudiation) label (never the "
                             "fraud_flag outcome — the engine refuses it; window ≠ the label window); the "
                             "3-part leakage control must FLAG it. ")

INSURANCE_TEMPLATES: tuple[Template, ...] = (
    # ── LAPSE / PERSISTENCY funnel (mirrors churn — PRE-lapse, never 'lapsed'/'surrendered') ──────────
    # B9.1 — premium_payment_irregularity (premium regularity — the disengagement signal)
    Template(
        id="premium_payment_irregularity", family="persistency",
        intent="Premium-payment regularity / irregularity — the inter-payment gap std (or a latest-gap) "
               "over premium credits; a lengthening / erratic premium cadence is a pre-lapse "
               "disengagement signal. Built from premium behaviour, NEVER the 'lapsed' outcome.",
        needs=(Need("premium_col", "premium"), Need("event_ts", "event_timestamp"),
               Need("entity", "policy_id")),
        params={"window": (365, 180, 90), "measure": ("gap_std", "latest_gap", "regularity")},
        aggregation="premium_regularity", additivity="n/a", explain="H",
        use_cases=("lapse_risk", "persistency", "insurance"),
        pit=_PIT_TRAILING,
        degrade="only a single premium (no cadence history) -> SKIP (no regularity from one point).",
        stage="2-disengagement",
        eligibility=_LAPSE_PRELAPSE,
        notes=("anchor: 'premium' (insurance-distinctive, non-structural) routes this off a churn catalog "
               "(policy_id is an ENTITY concept — it would structurally bind any grain column, so it "
               "cannot be the sole anchor).",
               "a gap std / latest gap — n/a (not summable)."),
    ),
    # B9.2 — missed_premium_streak (arrears — consecutive short/missed premiums)
    Template(
        id="missed_premium_streak", family="persistency",
        intent="Missed-premium / arrears streak — consecutive billing periods where the premium PAID fell "
               "short of the premium DUE (missed or partial); a persistent arrears streak precedes lapse. "
               "Built from premium arrears, NEVER the 'lapsed' outcome.",
        needs=(Need("premium_col", "premium"), Need("scheduled_col", "scheduled_amount", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "policy_id")),
        params={"window": (365, 180, 90), "tolerance_pct": (5, 0, 10)},
        aggregation="missed_premium_streak", additivity="additive", explain="H",
        use_cases=("lapse_risk", "persistency", "insurance"),
        pit=_PIT_TRAILING,
        degrade="no contractual premium schedule -> derive the due premium from the recurring premium "
                "cadence (declared derivation §D.8; probabilistic — FLAG).",
        stage="3-arrears",
        eligibility=_INSURANCE_SINGLE_CCY + " " + _LAPSE_PRELAPSE,
        derived=("is_short := premium_paid < premium_due × (1 − {tolerance_pct}%) — a shortfall per "
                 "billing period; the streak counts consecutive short periods (computed DOWNSTREAM).",),
        notes=("anchor: 'premium' (insurance-distinctive, non-structural) routes this off a churn catalog.",
               "concept sub: the premium DUE uses 'scheduled_amount' (no dedicated premium-due concept).",
               "a count of consecutive missed/short periods — additive."),
    ),
    # B9.3 — surrender_value_trajectory (surrender pressure — value trend + surrender-value-to-premium)
    Template(
        id="surrender_value_trajectory", family="surrender_pressure",
        intent="Surrender-value trajectory & surrender pressure — the cash-surrender-value trend and its "
               "ratio to cumulative premiums paid (the incentive to surrender); a rising surrender value "
               "with disengagement is surrender pressure. Built PRE-surrender, NEVER 'surrendered'.",
        needs=(Need("surrender_col", "surrender_value"), Need("premium_col", "premium", optional=True),
               Need("asof", "as_of_date"), Need("entity", "policy_id")),
        params={"window": (365, 180, 90), "measure": ("surrender_ratio", "value_trend", "surrender_pressure")},
        aggregation="surrender_trajectory", additivity="non_additive", explain="H",
        use_cases=("lapse_risk", "surrender", "persistency", "insurance"),
        pit=_INSURANCE_PIT_STATE,
        degrade="no premium history for the ratio -> report the surrender-value trend only (FLAG the "
                "narrower scope).",
        stage="3-arrears",
        eligibility=_INSURANCE_SINGLE_CCY + " " + _LAPSE_PRELAPSE,
        notes=("anchor: 'surrender_value' (insurance-distinctive stock, non-structural) routes this off a "
               "churn catalog.",
               "OUTPUT additivity is measure-dependent: surrender_ratio / surrender_pressure are "
               "non-additive ratios; value_trend is n/a; the raw surrender_value is a semi-additive stock "
               "— the default carries the ratio case."),
    ),
    # B9.4 — policy_loan_utilisation (financial-stress signal — loan against the policy)
    Template(
        id="policy_loan_utilisation", family="surrender_pressure",
        intent="Policy-loan utilisation — the outstanding policy loan drawn against the policy's cash / "
               "surrender value (loan ÷ surrender_value); high utilisation is a policyholder-liquidity-"
               "stress + pre-lapse signal.",
        needs=(Need("surrender_col", "surrender_value"), Need("loan_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "policy_id")),
        params={"window": (365, 180, 90), "measure": ("utilisation", "loan_trend")},
        aggregation="policy_loan_utilisation", additivity="non_additive", explain="H",
        use_cases=("lapse_risk", "insurance", "hardship"),
        pit=_INSURANCE_PIT_STATE,
        degrade="no policy-loan balance -> SKIP (no loan to size against the surrender value).",
        stage="3-arrears",
        eligibility=_INSURANCE_SINGLE_CCY + " " + _LAPSE_PRELAPSE,
        notes=("anchor: 'surrender_value' (insurance-distinctive — the loan's collateral base) routes "
               "this off a churn catalog.",
               "concept sub: no dedicated policy_loan concept — the loan balance uses 'monetary_stock', "
               "sized against the surrender_value.",
               "OUTPUT additivity is measure-dependent: utilisation is a non-additive ratio; loan_trend "
               "is n/a — the default carries the ratio case."),
    ),
    # ── CLAIMS journey — frequency/severity + the claims-fraud typology (behaviour, near-label) ───────
    # B9.5 — claims_frequency_severity (claim behaviour over the window)
    Template(
        id="claims_frequency_severity", family="claims",
        intent="Claims frequency / severity — the count of claims (frequency) and the incurred claim "
               "reserve per claim (severity), or a loss ratio vs premiums earned, over a trailing window; "
               "the core claims-cost signal.",
        needs=(Need("reserve_col", "claim_reserve"), Need("premium_col", "premium", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "policy_id")),
        params={"window": (365, 180, 720), "measure": ("frequency", "severity", "loss_ratio")},
        aggregation="claims_frequency_severity", additivity="additive", explain="H",
        use_cases=("claims", "insurance", "pricing"),
        pit=_PIT_TRAILING,
        degrade="no premium base for the loss ratio -> report frequency + severity only.",
        stage="claims",
        eligibility=_INSURANCE_SINGLE_CCY + " " + _PREMIUM_WRITTEN_EARNED,
        notes=("anchor: 'claim_reserve' (insurance-distinctive, non-structural) routes this off a churn "
               "catalog.",
               "OUTPUT additivity is measure-dependent: frequency is an additive count; severity via "
               "claim_reserve is a semi-additive STOCK; a loss_ratio is non-additive — the default "
               "carries the additive count."),
    ),
    # B9.6 — claims_fraud_typology (early-claim / over-servicing — BEHAVIOUR, NEAR-LABEL ⚠)
    Template(
        id="claims_fraud_typology", family="claims_fraud",
        intent="Claims-fraud typology — early-claim (a claim soon after inception), claim-amount anomaly "
               "or over-servicing frequency, read from claim BEHAVIOUR (measure=early_claim_flag / "
               "over_servicing_score / claim_amount_zscore); a red-flag typology that BORDERS the SIU "
               "label. Built from behaviour, NEVER 'fraud_flag'.",
        needs=(Need("reserve_col", "claim_reserve"), Need("inception", "effective_date"),
               Need("event_ts", "event_timestamp"), Need("entity", "policy_id")),
        params={"window": (720, 365, 180),
                "measure": ("early_claim_flag", "over_servicing_score", "claim_amount_zscore")},
        aggregation="claims_fraud_typology", additivity="n/a", explain="M",
        use_cases=("claims", "claims_fraud", "insurance", "financial_crime"),
        pit=_PIT_TRAILING,
        degrade="no policy-inception date -> the early-claim measure degrades to a claim-amount anomaly "
                "only (weaker + FLAGGED).",
        stage="claims-investigation",
        near_label=True,
        eligibility=_CLAIMS_NEAR_LABEL_PREFIX + "a claims-fraud typology built over prior-claim / adverse "
                    "behaviour borders the confirmed-fraud/repudiation label. " + _INSURANCE_SINGLE_CCY,
        derived=("is_early_claim := claim_event within a short spell after effective_date (inception) — "
                 "computed DOWNSTREAM (no data plane); the SIU/fraud_flag OUTCOME is NEVER an input.",),
        notes=("anchor: 'claim_reserve' (insurance-distinctive claim behaviour, non-structural) routes "
               "this off a churn catalog.",
               "borders the SIU/confirmed-fraud label — observe strictly pre-label; fraud_flag is a "
               "leakage anchor the engine refuses.",
               "a flag / score / z-score — n/a (not summable)."),
    ),
    # ── REINSURANCE / UNDERWRITING / BANCASSURANCE ───────────────────────────────────────────────────
    # B9.7 — reinsurance_recoverable_concentration (ceded-reserve recoverable concentration)
    Template(
        id="reinsurance_recoverable_concentration", family="reinsurance",
        intent="Reinsurance-recoverable concentration — how concentrated the amount recoverable from "
               "reinsurers on ceded reserves is (an HHI across reinsurers), or the recoverable ÷ gross "
               "reserve share; a concentrated recoverable is reinsurer counterparty risk.",
        needs=(Need("recoverable_col", "reinsurance_recoverable"),
               Need("reserve_col", "claim_reserve", optional=True),
               Need("asof", "as_of_date"), Need("entity", "policy_id")),
        params={"window": (365, 180, 90), "measure": ("concentration_hhi", "recoverable_share", "recoverable_amount")},
        aggregation="reinsurance_recoverable", additivity="non_additive", explain="M",
        use_cases=("reinsurance", "insurance", "counterparty_risk"),
        pit=_INSURANCE_PIT_STATE,
        degrade="no gross-reserve base for the share -> report the recoverable concentration HHI only.",
        stage="reinsurance",
        eligibility=_INSURANCE_SINGLE_CCY,
        notes=("anchor: 'reinsurance_recoverable' (insurance-distinctive stock, non-structural) routes "
               "this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: concentration_hhi / recoverable_share are "
               "non-additive; the raw recoverable_amount is a semi-additive STOCK (an ESTIMATED "
               "reinsurance asset) — the default carries the non-additive case."),
    ),
    # B9.8 — sum_assured_adequacy (underinsurance vs a needs proxy)
    Template(
        id="sum_assured_adequacy", family="underwriting",
        intent="Sum-assured adequacy / underinsurance — the sum assured (face amount) vs a needs / income "
               "proxy (measure=adequacy_ratio = sum_assured ÷ income), an underinsurance flag, or the raw "
               "sum-assured exposure; a low ratio flags underinsurance (and cross-sell headroom).",
        needs=(Need("sum_assured_col", "sum_assured"), Need("income_col", "monetary_flow", optional=True),
               Need("asof", "as_of_date"), Need("entity", "policy_id")),
        params={"window": (365, 180, 90), "measure": ("adequacy_ratio", "underinsurance_flag", "sum_assured_amount")},
        aggregation="sum_assured_adequacy", additivity="non_additive", explain="H",
        use_cases=("underwriting", "insurance", "bancassurance"),
        pit=_INSURANCE_PIT_STATE,
        degrade="no income / needs proxy -> report the raw sum-assured exposure (a semi-additive stock).",
        stage="underwriting",
        eligibility=_INSURANCE_SINGLE_CCY + " income is SENSITIVE — flagged.",
        notes=("anchor: 'sum_assured' (insurance-distinctive stock, non-structural) routes this off a "
               "churn catalog.",
               "concept sub: no dedicated income concept — the needs proxy uses 'monetary_flow' (a salary "
               "credit).",
               "OUTPUT additivity is measure-dependent: adequacy_ratio is a non-additive ratio; "
               "underinsurance_flag is n/a; the raw sum_assured is a semi-additive STOCK — the default "
               "carries the ratio case."),
    ),
    # B9.9 — bancassurance_cross_hold (insurance held alongside banking — share of wallet)
    Template(
        id="bancassurance_cross_hold", family="bancassurance",
        intent="Bancassurance cross-hold — the count of premium-paying insurance policies a customer "
               "holds alongside their banking products (measure=policy_count / cross_hold_flag / "
               "premium_share); a bancassurance share-of-wallet + stickiness signal.",
        needs=(Need("premium_col", "premium"), Need("product", "product_type", optional=True),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("policy_count", "cross_hold_flag", "premium_share")},
        aggregation="bancassurance_cross_hold", additivity="additive", explain="H",
        use_cases=("bancassurance", "insurance", "cross_sell", "share_of_wallet"),
        pit=_INSURANCE_PIT_STATE,
        degrade="no banking product-holding data -> report the insurance policy_count only (no cross-hold "
                "ratio).",
        stage="bancassurance",
        eligibility=_INSURANCE_SINGLE_CCY,
        notes=("anchor: 'premium' (insurance-distinctive — a premium-paying policy held) routes this off "
               "a churn catalog (customer_id is an ENTITY concept — not the sole anchor).",
               "concept sub: no product_holding concept — the banking side uses 'product_type'.",
               "OUTPUT additivity is measure-dependent: policy_count is an additive count; "
               "cross_hold_flag is n/a; premium_share is a non-additive ratio — the default carries the "
               "additive count."),
    ),
    # B9.10 — mortality_morbidity_loading (underwriting risk-loading — health-ADJACENT sensitivity)
    Template(
        id="mortality_morbidity_loading", family="underwriting",
        intent="Mortality / morbidity loading — the actuarial mortality/morbidity RATE (from a table) "
               "applied to a policy, its level or an underwriting loading factor vs the standard table; "
               "an underwriting risk signal.",
        needs=(Need("rate_col", "mortality_morbidity"), Need("asof", "as_of_date"),
               Need("entity", "policy_id")),
        params={"window": (365, 180, 90), "measure": ("rate_level", "loading_factor")},
        aggregation="mortality_morbidity_loading", additivity="non_additive", explain="M",
        use_cases=("underwriting", "insurance", "pricing"),
        pit=_INSURANCE_PIT_STATE,
        degrade="no mortality/morbidity assumption reported -> SKIP.",
        stage="underwriting",
        eligibility="⚠ HEALTH-ADJACENT: mortality_morbidity is the actuarial RATE (bindable), but an "
                    "individual's health STATUS is special_category (GDPR) — the engine BLOCKS binding a "
                    "special_category column; consent / purpose eligibility applies to the underlying "
                    "medical data.",
        notes=("anchor: 'mortality_morbidity' (insurance-distinctive rate, non-structural) routes this "
               "off a churn catalog.",
               "a mortality/morbidity RATE (or its loading) — non-additive; never sum across policies.",
               "do NOT anchor on a special_category health-status column — the engine refuses it."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The Islamic-banking templates — the §B13 conventional funnels reframed + the SHARIA-COMPLIANCE overlay
# authored to Part-F depth (Phase-3 Pass-5, breadth).
#
# Most B1–B7 funnels APPLY, reframed: PROFIT-rate, not interest. The distinctive layer is Sharia
# compliance — a HARD eligibility gate (ratified by the Sharia board), plus product-specific behavioural
# signals (Murabaha installments, Mudaraba/Musharaka profit-sharing, Sukuk, Takaful). Two disciplines:
#   • ROUTING — every recipe REQUIRES an Islamic-distinctive, NON-STRUCTURAL concept (profit_rate /
#     profit_share_ratio / purification_amount / prohibited_activity_exposure / sukuk / takaful_
#     contribution — NOT an entity concept like customer_id, which is structural). Grounding is the
#     router; a churn catalog grounds NOTHING here (the locked invariant). 'profit_rate' is DELIBERATELY
#     not is_a monetary_rate (a Sharia + modelling distinction), so it binds only by exact concept match.
#   • COMPLIANCE / NEAR-LABEL — a prohibited-activity exposure crossing the 5%/33% Sharia screen BORDERS
#     the compliance-breach determination (the sharia_compliant_flag) -> near_label=True + a ⚠ note. No
#     recipe models profit as guaranteed interest (riba). The Part-K appendix in
#     docs/…/2026-07-08-banking-feature-template-library.md is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_ISLAMIC_PIT_STATE = ("point-in-time Islamic-account STATE observed as-of: the latest profit-rate / "
                      "ratio / exposure within (as_of − {window}, as_of], knowable strictly ≤ as_of, "
                      "never forward. DESIGN-TIME declaration — no data plane enforces runtime PIT.")
_PROFIT_NOT_INTEREST = ("Sharia: 'profit_rate' is a PROFIT rate (Murabaha mark-up / Mudaraba expected "
                        "profit), NOT interest (riba) — never model it as a guaranteed conventional rate.")
_SHARIA_GATE = ("Sharia compliance is a HARD eligibility gate (ratified by the Sharia board) — non-"
                "compliance BLOCKS the product, not merely flags it.")
_ISLAMIC_NEAR_LABEL_PREFIX = ("⚠ NEAR-LABEL: a prohibited-activity exposure crossing the 5%/33% Sharia "
                              "screen BORDERS the compliance-breach determination (the sharia_compliant_"
                              "flag) — observe the exposure STRICTLY before the breach is declared; the "
                              "3-part leakage control must FLAG it. ")

ISLAMIC_TEMPLATES: tuple[Template, ...] = (
    # ── SHARIA-COMPLIANCE overlay — profit-rate, profit-sharing, purification, prohibited-activity ────
    # B13.1 — profit_rate_exposure (Murabaha mark-up / expected profit vs a benchmark)
    Template(
        id="profit_rate_exposure", family="sharia_profit_rate",
        intent="Profit-rate exposure — the Islamic PROFIT rate (Murabaha mark-up / Mudaraba expected "
               "profit) level, its spread vs a reference benchmark, or its trend (measure=rate_level / "
               "benchmark_spread / trend); the Islamic analogue of a rate-exposure feature — NOT interest.",
        needs=(Need("profit_col", "profit_rate"), Need("benchmark_col", "benchmark_rate", optional=True),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("rate_level", "benchmark_spread", "trend")},
        aggregation="profit_rate_exposure", additivity="non_additive", explain="H",
        use_cases=("sharia_compliance", "islamic_banking", "pricing"),
        pit=_ISLAMIC_PIT_STATE,
        degrade="no benchmark series -> report the profit-rate level / trend only (no spread).",
        stage="sharia-compliance",
        eligibility=_PROFIT_NOT_INTEREST + " " + _SHARIA_GATE,
        notes=("anchor: 'profit_rate' (Islamic-distinctive, non-structural — deliberately NOT is_a "
               "monetary_rate) routes this off a churn catalog.",
               "a profit RATE (or spread / trend) — non-additive; never sum or naively average across "
               "notionals."),
    ),
    # B13.2 — profit_sharing_split_behaviour (Mudaraba/Musharaka PSR + partner-performance volatility)
    Template(
        id="profit_sharing_split_behaviour", family="sharia_profit_sharing",
        intent="Profit-sharing (Mudaraba/Musharaka) split behaviour — the pre-agreed profit-sharing ratio "
               "(PSR) level and its realised-profit volatility (partner performance); measure=psr_level / "
               "psr_volatility. A distinctive Islamic partnership-performance signal (not a guaranteed "
               "return).",
        needs=(Need("psr_col", "profit_share_ratio"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("psr_level", "psr_volatility")},
        aggregation="profit_sharing_split", additivity="non_additive", explain="M",
        use_cases=("sharia_compliance", "islamic_banking"),
        pit=_ISLAMIC_PIT_STATE,
        degrade="only a single PSR snapshot (no history) -> report the PSR level only (no volatility).",
        stage="sharia-compliance",
        eligibility=_PROFIT_NOT_INTEREST + " " + _SHARIA_GATE,
        notes=("anchor: 'profit_share_ratio' (Islamic-distinctive, non-structural) routes this off a "
               "churn catalog.",
               "a PSR ratio / its volatility — non-additive (a pre-agreed split, not an aggregation)."),
    ),
    # B13.3 — purification_ratio (non-compliant income to purify — a Sharia flow)
    Template(
        id="purification_ratio", family="sharia_purification",
        intent="Income-purification ratio — the non-compliant income to be purified (donated to charity) "
               "vs total income (measure=purification_ratio), or the raw purification amount; a rising "
               "purification ratio flags creeping Sharia non-compliance in the income mix.",
        needs=(Need("purification_col", "purification_amount"),
               Need("income_col", "monetary_flow", optional=True),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("purification_ratio", "purification_amount")},
        aggregation="purification_ratio", additivity="non_additive", explain="H",
        use_cases=("sharia_compliance", "islamic_banking"),
        pit=_PIT_TRAILING,
        degrade="no total-income base -> report the raw purification amount (an additive flow) not the "
                "ratio.",
        stage="sharia-compliance",
        eligibility=_SHARIA_GATE,
        notes=("anchor: 'purification_amount' (Islamic-distinctive flow, non-structural) routes this off "
               "a churn catalog.",
               "OUTPUT additivity is measure-dependent: purification_ratio is a non-additive ratio; the "
               "raw purification_amount is an additive flow — the default carries the ratio case."),
    ),
    # B13.4 — prohibited_activity_exposure_share (haram-sector screen — NEAR-LABEL to the compliance flag)
    Template(
        id="prohibited_activity_exposure_share", family="sharia_screening",
        intent="Prohibited-activity exposure share — the share of exposure in Sharia-prohibited sectors "
               "(alcohol / gambling / conventional finance) vs total exposure (measure=exposure_share), a "
               "screen-breach flag (5%/33% thresholds), or the raw exposure; a Sharia-screening signal.",
        needs=(Need("prohibited_col", "prohibited_activity_exposure"),
               Need("total_col", "monetary_stock", optional=True),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("exposure_share", "breach_flag", "exposure_amount")},
        aggregation="prohibited_activity_exposure", additivity="non_additive", explain="H",
        use_cases=("sharia_compliance", "islamic_banking", "screening"),
        pit=_ISLAMIC_PIT_STATE,
        degrade="no total-exposure base -> report the raw prohibited-activity exposure (a semi-additive "
                "stock) not the share.",
        stage="sharia-compliance",
        near_label=True,
        eligibility=_ISLAMIC_NEAR_LABEL_PREFIX + _SHARIA_GATE,
        notes=("anchor: 'prohibited_activity_exposure' (Islamic-distinctive screening stock, "
               "non-structural) routes this off a churn catalog.",
               "borders the sharia-compliance-breach determination — observe strictly pre-breach.",
               "OUTPUT additivity is measure-dependent: exposure_share is a non-additive ratio; "
               "breach_flag is n/a; the raw exposure is a semi-additive STOCK — the default carries the "
               "ratio case."),
    ),
    # B13.5 — sukuk_concentration (Sharia-compliant certificate holdings — NOT a conventional bond)
    Template(
        id="sukuk_concentration", family="sukuk",
        intent="Sukuk holding / concentration — how concentrated a customer's Sukuk (Sharia-compliant "
               "asset-backed certificate) holdings are (an HHI across issuers), the holding share, or the "
               "on-book amount; a Sharia-compliant-instrument concentration signal.",
        needs=(Need("sukuk_col", "sukuk"), Need("holding_col", "monetary_stock", optional=True),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("concentration_hhi", "holding_share", "holding_amount")},
        aggregation="sukuk_concentration", additivity="non_additive", explain="M",
        use_cases=("sharia_compliance", "islamic_banking", "concentration_risk"),
        pit=_ISLAMIC_PIT_STATE,
        degrade="no per-issuer holding values -> report the distinct-issuer count only (weaker; FLAG).",
        stage="sharia-compliance",
        eligibility=_SHARIA_GATE,
        notes=("anchor: 'sukuk' (Islamic-distinctive instrument classification, non-structural) routes "
               "this off a churn catalog — a Sukuk is asset-backed, NOT a conventional interest-bearing "
               "bond.",
               "OUTPUT additivity is measure-dependent: concentration_hhi / holding_share are "
               "non-additive; the raw holding_amount is a semi-additive STOCK — the default carries the "
               "non-additive case."),
    ),
    # B13.6 — takaful_contribution_behaviour (the Islamic insurance analogue of premium)
    Template(
        id="takaful_contribution_behaviour", family="takaful",
        intent="Takaful contribution behaviour — the cumulative Takaful contribution (tabarru' — a "
               "cooperative donation, the Islamic analogue of a premium), its regularity, or a payment "
               "gap; an erratic contribution cadence is a Takaful pre-lapse signal (like B9).",
        needs=(Need("contribution_col", "takaful_contribution"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("cumulative_contribution", "contribution_regularity", "payment_gap")},
        aggregation="takaful_contribution", additivity="additive", explain="H",
        use_cases=("sharia_compliance", "islamic_banking", "lapse_risk"),
        pit=_PIT_TRAILING,
        degrade="only a single contribution (no cadence history) -> report the cumulative contribution "
                "only (no regularity).",
        stage="sharia-compliance",
        eligibility=_SHARIA_GATE + " a Takaful contribution is NOT interest/premium (a tabarru' "
                    "donation).",
        notes=("anchor: 'takaful_contribution' (Islamic-distinctive flow, non-structural) routes this "
               "off a churn catalog.",
               "OUTPUT additivity is measure-dependent: the cumulative contribution is an additive flow; "
               "contribution_regularity / payment_gap are n/a — the default carries the additive flow."),
    ),
    # ── CONVENTIONAL funnels reframed — deposit beta + Murabaha installment behaviour (profit_rate) ───
    # B13.7 — islamic_deposit_beta (profit-rate sensitivity — deposit attrition reframed)
    Template(
        id="islamic_deposit_beta", family="sharia_profit_rate",
        intent="Islamic deposit beta — how much a Sharia deposit's paid PROFIT rate (or its balance) "
               "responds to a move in the profit-rate environment (Δ paid profit / Δ profit_rate) over "
               "the window; the Islamic-deposit rate-sensitivity signal — profit_rate, not interest.",
        needs=(Need("profit_col", "profit_rate"), Need("balance_col", "monetary_stock"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("rate_beta", "balance_beta")},
        aggregation="islamic_deposit_beta", additivity="non_additive", explain="H",
        use_cases=("sharia_compliance", "islamic_banking", "deposit_stability", "alm"),
        pit=_ISLAMIC_PIT_STATE,
        degrade="only a single rate snapshot (no history) -> SKIP (no beta from one point).",
        stage="deposit-attrition",
        eligibility=_PROFIT_NOT_INTEREST + " " + _SHARIA_GATE + " single currency.",
        notes=("anchor: 'profit_rate' (Islamic-distinctive, non-structural) routes this off a churn "
               "catalog — the profit-rate analogue of the deposits 'deposit_beta' (which uses "
               "benchmark_rate).",
               "a beta (a ratio) — non-additive; compute per depositor/segment, never sum."),
    ),
    # B13.8 — murabaha_installment_behaviour (Murabaha cost-plus installment behaviour — credit reframed)
    Template(
        id="murabaha_installment_behaviour", family="murabaha",
        intent="Murabaha installment behaviour — over a cost-plus (Murabaha, disclosed profit_rate) "
               "financing, the count of scheduled installments where the amount PAID fell short of the "
               "amount DUE, or the payment ratio; the Islamic analogue of the credit-B2 repayment signal.",
        needs=(Need("profit_col", "profit_rate"), Need("scheduled_col", "scheduled_amount", optional=True),
               Need("paid_col", "monetary_flow"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "tolerance_pct": (5, 0, 10),
                "measure": ("missed_installment_count", "payment_ratio")},
        aggregation="murabaha_installment", additivity="additive", explain="H",
        use_cases=("sharia_compliance", "islamic_banking", "credit_risk"),
        pit=_PIT_TRAILING,
        degrade="no contractual installment schedule -> derive the due from the recurring installment "
                "cadence (declared derivation §D.8; probabilistic — FLAG).",
        stage="installment-behaviour",
        eligibility=_PROFIT_NOT_INTEREST + " " + _SHARIA_GATE + " single currency.",
        derived=("is_short := paid < scheduled × (1 − {tolerance_pct}%) per installment — a shortfall vs "
                 "the contractual due, counted per date (computed DOWNSTREAM).",),
        notes=("anchor: 'profit_rate' (Islamic-distinctive — a Murabaha discloses a profit mark-up, not "
               "interest) routes this off a churn catalog.",
               "concept sub: the installment DUE uses 'scheduled_amount' (no dedicated Murabaha concept).",
               "OUTPUT additivity is measure-dependent: missed_installment_count is an additive count; "
               "payment_ratio is non-additive — the default carries the additive count."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The ESG / sustainable-finance templates — the §B11 SCORING + TRANSITION-RISK journey authored to
# Part-F depth (Phase-3 Pass-5, breadth).
#
# ESG data is often EXTERNAL (ratings vendors / emissions disclosures) + heavily ESTIMATED and RESTATED;
# an esg_score is itself a model output. Two authoring disciplines are load-bearing:
#   • ROUTING — every recipe REQUIRES an ESG-distinctive, NON-STRUCTURAL concept (scope_1/2/3_emissions /
#     financed_emissions / carbon_intensity / taxonomy_alignment / transition_alignment / physical_hazard_
#     score / emissions_data_quality / sll_kpi — NOT an entity concept like counterparty_id, which is
#     structural). Grounding is the router; a churn catalog grounds NOTHING here (the locked invariant).
#   • ADDITIVITY GUARD (the load-bearing correctness rule) — GHG scopes are additive WITHIN a scope, but a
#     naive scope 1+2+3 total DOUBLE-COUNTS across the value chain (one firm's Scope 1 is another's Scope
#     3), and Scope 3 is NOT summable across a PORTFOLIO (cross-entity double-count); financed_emissions is
#     PCAF-ATTRIBUTED (additive across the book — attribution avoids the double-count); carbon_intensity is
#     a ratio → non_additive. Each recipe picks additivity honestly and annotates the trap in notes. No
#     recipe is near-label (an ESG/climate signal does not border a customer outcome). geographic is
#     CLIMATE-legitimate here (physical hazard), NOT a fair-lending credit proxy. The Part-K appendix in
#     docs/…/2026-07-08-banking-feature-template-library.md is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_ESG_PIT_STATE = ("point-in-time ESG / climate STATE observed as-of: the latest emissions / alignment / "
                  "score within (as_of − {window}, as_of], knowable strictly ≤ as_of, never forward. ESG "
                  "data is EXTERNAL + heavily ESTIMATED and RESTATED — honour system_time to avoid "
                  "restated-data leakage. DESIGN-TIME declaration — no data plane enforces runtime PIT.")
_ESG_EXTERNAL = ("ESG data is largely EXTERNAL (ratings vendors / disclosures) + ESTIMATED — an esg_score "
                 "is itself a model output; availability/quality caveats apply (provenance-flagged).")
_ESG_SCOPE_GUARD = ("ADDITIVITY GUARD: GHG scopes are additive WITHIN a scope, but a naive scope 1+2+3 "
                    "total DOUBLE-COUNTS across the value chain (one firm's Scope 1 is another's Scope 3) "
                    "— a cross-scope total is a GUARDED downstream derivation, not a naive sum. "
                    "carbon_intensity is a ratio (non_additive); financed_emissions is PCAF-ATTRIBUTED "
                    "(additive across the book).")

ESG_TEMPLATES: tuple[Template, ...] = (
    # ── SCORING / EMISSIONS — absolute & intensity by scope (the additivity double-count guard) ──────
    # B11.1 — emissions_trend_by_scope (per-scope absolute + intensity; GUARD the cross-scope total)
    Template(
        id="emissions_trend_by_scope", family="emissions",
        intent="Absolute & intensity emissions trend BY SCOPE — the per-scope GHG level (Scope 1 direct, "
               "Scope 2 purchased-energy, Scope 3 value-chain) and its trend, or a carbon-intensity "
               "trend (measure=absolute_level / absolute_trend / intensity_trend). GUARD: never naively "
               "sum across scopes (double-counts the value chain).",
        needs=(Need("scope1_col", "scope_1_emissions"),
               Need("scope2_col", "scope_2_emissions", optional=True),
               Need("scope3_col", "scope_3_emissions", optional=True),
               Need("intensity_col", "carbon_intensity", optional=True),
               Need("asof", "as_of_date"), Need("entity", "counterparty_id")),
        params={"window": (365, 180, 90), "measure": ("absolute_level", "absolute_trend", "intensity_trend")},
        aggregation="emissions_by_scope", additivity="additive", explain="H",
        use_cases=("esg_scoring", "transition_risk", "climate_risk"),
        pit=_ESG_PIT_STATE,
        degrade="only a single emissions snapshot (no history) -> report the per-scope level (no trend).",
        stage="scoring",
        eligibility=_ESG_EXTERNAL,
        notes=("anchor: 'scope_1_emissions' (ESG-distinctive, non-structural) routes this off a churn "
               "catalog.",
               _ESG_SCOPE_GUARD,
               "OUTPUT additivity: a per-scope absolute level is ADDITIVE WITHIN that scope; the "
               "intensity_trend measure (carbon_intensity) is non-additive — the default carries the "
               "additive per-scope level."),
    ),
    # B11.2 — carbon_intensity_trajectory (emissions ÷ revenue level & trend)
    Template(
        id="carbon_intensity_trajectory", family="emissions",
        intent="Carbon-intensity trajectory — emissions per unit of activity/revenue (carbon_intensity) "
               "level and its trailing trend (measure=level / trend); a rising intensity is transition "
               "deterioration.",
        needs=(Need("intensity_col", "carbon_intensity"), Need("asof", "as_of_date"),
               Need("entity", "counterparty_id")),
        params={"window": (365, 180, 90), "measure": ("level", "trend")},
        aggregation="carbon_intensity", additivity="non_additive", explain="H",
        use_cases=("esg_scoring", "transition_risk", "climate_risk"),
        pit=_ESG_PIT_STATE,
        degrade="only a single intensity snapshot (no history) -> report the level only (no trend).",
        stage="transition-risk",
        eligibility=_ESG_EXTERNAL,
        notes=("anchor: 'carbon_intensity' (ESG-distinctive, non-structural) routes this off a churn "
               "catalog.",
               "carbon_intensity is a RATIO (emissions ÷ revenue) — non-additive; never sum across "
               "entities."),
    ),
    # B11.3 — financed_emissions_attribution (PCAF — attributed to loans/investments, additive)
    Template(
        id="financed_emissions_attribution", family="financed_emissions",
        intent="Financed-emissions attribution (PCAF) — the emissions ATTRIBUTED to a loan/investment "
               "book (measure=absolute), or a financed-emissions intensity vs the exposure "
               "(measure=intensity); the portfolio-decarbonisation signal. Heavily ESTIMATED.",
        needs=(Need("financed_col", "financed_emissions"), Need("exposure_col", "monetary_stock", optional=True),
               Need("asof", "as_of_date"), Need("entity", "counterparty_id")),
        params={"window": (365, 180, 90), "measure": ("absolute", "intensity", "trend")},
        aggregation="financed_emissions", additivity="additive", explain="H",
        use_cases=("esg_scoring", "transition_risk", "climate_risk"),
        pit=_ESG_PIT_STATE,
        degrade="no exposure base for the intensity -> report the absolute financed emissions only.",
        stage="scoring",
        eligibility=_ESG_EXTERNAL,
        notes=("anchor: 'financed_emissions' (ESG-distinctive, non-structural) routes this off a churn "
               "catalog.",
               "financed_emissions is PCAF-ATTRIBUTED — ADDITIVE across the book (attribution AVOIDS the "
               "cross-entity double-count that a raw Scope-3 sum would hit); heavily ESTIMATED.",
               "OUTPUT additivity is measure-dependent: absolute is additive; intensity is a non-additive "
               "ratio; a trend is n/a — the default carries the additive attribution."),
    ),
    # B11.4 — emissions_data_quality_reliance (provenance — PCAF data-quality / estimation reliance)
    Template(
        id="emissions_data_quality_reliance", family="data_quality",
        intent="Emissions data-quality / estimation reliance — the PCAF data-quality score (1 measured → "
               "5 estimated) or the estimated-share of an entity's emissions (measure=avg_data_quality / "
               "estimated_share); a PROVENANCE feature — high estimated-share = low confidence.",
        needs=(Need("dq_col", "emissions_data_quality"), Need("asof", "as_of_date"),
               Need("entity", "counterparty_id")),
        params={"window": (365, 180, 90), "measure": ("avg_data_quality", "estimated_share")},
        aggregation="emissions_data_quality", additivity="non_additive", explain="H",
        use_cases=("esg_scoring", "climate_risk", "data_quality"),
        pit=_ESG_PIT_STATE,
        degrade="no PCAF data-quality score reported -> SKIP.",
        stage="scoring",
        eligibility=_ESG_EXTERNAL,
        notes=("anchor: 'emissions_data_quality' (ESG-distinctive provenance score, non-structural) "
               "routes this off a churn catalog.",
               "a PCAF data-quality ORDINAL (or estimated-share) — non-additive; a provenance / "
               "confidence feature, high estimated-share = low confidence in the emissions figure."),
    ),
    # ── TRANSITION-RISK journey — taxonomy alignment, pathway gap, physical hazard, SLL KPI ──────────
    # B11.5 — taxonomy_alignment_share (EU-taxonomy % revenue/capex aligned)
    Template(
        id="taxonomy_alignment_share", family="taxonomy",
        intent="EU-Taxonomy alignment share — the % of an entity's revenue/capex/opex that is Taxonomy-"
               "eligible AND aligned, its trend, or an eligible-share (measure=aligned_share / "
               "eligible_share / trend); a green-revenue signal.",
        needs=(Need("taxonomy_col", "taxonomy_alignment"), Need("asof", "as_of_date"),
               Need("entity", "counterparty_id")),
        params={"window": (365, 180, 90), "measure": ("aligned_share", "eligible_share", "trend")},
        aggregation="taxonomy_alignment", additivity="non_additive", explain="H",
        use_cases=("esg_scoring", "transition_risk", "climate_risk"),
        pit=_ESG_PIT_STATE,
        degrade="only a single disclosure (no history) -> report the aligned share only (no trend).",
        stage="transition-risk",
        eligibility=_ESG_EXTERNAL,
        notes=("anchor: 'taxonomy_alignment' (ESG-distinctive, non-structural) routes this off a churn "
               "catalog.",
               "a Taxonomy-aligned SHARE (% of revenue/capex) — non-additive (a ratio)."),
    ),
    # B11.6 — transition_alignment_gap (net-zero pathway gap / implied temperature rise)
    Template(
        id="transition_alignment_gap", family="transition",
        intent="Transition / net-zero alignment gap — the entity's transition-alignment (implied "
               "temperature rise / SBTi alignment) level and its gap vs a net-zero pathway "
               "(measure=alignment_level / pathway_gap / trend); a widening gap is transition risk "
               "(ALIGNED → LAGGING → HIGH-RISK → STRANDED).",
        needs=(Need("transition_col", "transition_alignment"), Need("asof", "as_of_date"),
               Need("entity", "counterparty_id")),
        params={"window": (365, 180, 90), "measure": ("alignment_level", "pathway_gap", "trend")},
        aggregation="transition_alignment", additivity="non_additive", explain="H",
        use_cases=("esg_scoring", "transition_risk", "climate_risk"),
        pit=_ESG_PIT_STATE,
        degrade="only a single alignment snapshot (no history) -> report the level only (no gap trend).",
        stage="transition-risk",
        eligibility=_ESG_EXTERNAL,
        notes=("anchor: 'transition_alignment' (ESG-distinctive, non-structural) routes this off a churn "
               "catalog.",
               "an alignment level / pathway gap (implied temp rise) — non-additive."),
    ),
    # B11.7 — physical_hazard_exposure (flood/heat/wildfire, location-based — climate-legit geographic)
    Template(
        id="physical_hazard_exposure", family="physical_risk",
        intent="Physical-hazard exposure — the physical climate-risk hazard score (flood / heat / "
               "wildfire, location-based, scenario-dependent) of an entity's collateral/operations, its "
               "level or the high-hazard share (measure=hazard_score / high_hazard_share).",
        needs=(Need("hazard_col", "physical_hazard_score"), Need("geo", "geographic", optional=True),
               Need("asof", "as_of_date"), Need("entity", "counterparty_id")),
        params={"window": (365, 180, 90), "measure": ("hazard_score", "high_hazard_share")},
        aggregation="physical_hazard", additivity="non_additive", explain="H",
        use_cases=("esg_scoring", "climate_risk", "physical_risk"),
        pit=_ESG_PIT_STATE,
        degrade="no location granularity -> report the entity-level hazard score only (no location mix).",
        stage="physical-risk",
        eligibility="geographic here is CLIMATE-legitimate (physical-hazard location), NOT a fair-lending "
                    "credit proxy — use-case-scoped to climate risk. " + _ESG_EXTERNAL,
        notes=("anchor: 'physical_hazard_score' (ESG-distinctive, non-structural) routes this off a churn "
               "catalog.",
               "a hazard score (scenario-dependent) / high-hazard share — non-additive."),
    ),
    # B11.8 — sll_kpi_achievement (sustainability-linked-loan/bond KPI vs the SPT — margin ratchet)
    Template(
        id="sll_kpi_achievement", family="sustainability_linked",
        intent="SLL / KPI achievement — the sustainability-linked-loan/bond KPI vs its target (SPT the "
               "margin ratchet keys off): achievement level, a breach flag, or the trend "
               "(measure=achievement / breach_flag / trend); a greenwashing / performance signal.",
        needs=(Need("kpi_col", "sll_kpi"), Need("asof", "as_of_date"),
               Need("entity", "counterparty_id")),
        params={"window": (365, 180, 90), "measure": ("achievement", "breach_flag", "trend")},
        aggregation="sll_kpi_achievement", additivity="non_additive", explain="H",
        use_cases=("esg_scoring", "transition_risk", "sustainable_finance"),
        pit=_ESG_PIT_STATE,
        degrade="only a single KPI reading (no history) -> report the achievement level only (no trend).",
        stage="transition-risk",
        eligibility=_ESG_EXTERNAL,
        notes=("anchor: 'sll_kpi' (ESG-distinctive, non-structural) routes this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: achievement / trend are non-additive; breach_flag "
               "is n/a — the default carries the non-additive case."),
    ),
    # B11.9 — scope3_value_chain_exposure (Scope-3 specifically — additive WITHIN a firm, not a portfolio)
    Template(
        id="scope3_value_chain_exposure", family="emissions",
        intent="Scope-3 value-chain exposure — the entity's Scope-3 (15-category value-chain) emissions "
               "level and trend (measure=absolute / trend), the ESTIMATED category most banks miss; "
               "additive WITHIN one firm but NEVER summable across a portfolio (cross-entity "
               "double-count).",
        needs=(Need("scope3_col", "scope_3_emissions"),
               Need("dq_col", "emissions_data_quality", optional=True),
               Need("asof", "as_of_date"), Need("entity", "counterparty_id")),
        params={"window": (365, 180, 90), "measure": ("absolute", "trend")},
        aggregation="scope3_value_chain", additivity="additive", explain="M",
        use_cases=("esg_scoring", "transition_risk", "climate_risk"),
        pit=_ESG_PIT_STATE,
        degrade="only a single Scope-3 snapshot (no history) -> report the absolute level (no trend).",
        stage="scoring",
        eligibility=_ESG_EXTERNAL,
        notes=("anchor: 'scope_3_emissions' (ESG-distinctive, non-structural) routes this off a churn "
               "catalog.",
               _ESG_SCOPE_GUARD,
               "OUTPUT additivity: Scope 3 is ADDITIVE WITHIN one firm; it is NOT summable across a "
               "PORTFOLIO (a cross-entity double-count — use financed_emissions for the book) and never "
               "summed with Scope 1/2. Heavily ESTIMATED — pair with emissions_data_quality."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The cross-sell / CLV templates — the §B5 GROWTH journey (the INVERSE of attrition) authored to Part-F
# depth (Phase-3 Pass-6, the FINAL breadth pass — completes the 15-family library).
#
# The positive mirror of B1 churn: the SAME signals read in reverse (rising breadth/salary = growth,
# falling = attrition). Journey (§B5): ONBOARDING → ACTIVATION → DEEPENING → MATURITY → ADVOCACY. Two
# authoring disciplines are load-bearing:
#   • ROUTING (⚠ the HARDEST case in the library) — cross-sell/CLV is the INVERSE of churn and SHARES its
#     generic concepts (monetary_flow, event_timestamp, customer_id). A CLV recipe needing ONLY those would
#     CROSS-SURFACE onto the churn catalog and break the locked churn=churn-lens invariant. So every recipe
#     ADDITIONALLY REQUIRES a NON-STRUCTURAL distinctive concept absent from churn — product_type / segment /
#     peer_group / channel (all four exist in the taxonomy — no substitution needed). An ENTITY concept
#     (product_id / campaign_id / household_id / relationship_manager_id) gets structural is_grain credit in
#     _match and would bind ANY churn grain column, so it is NEVER a sole anchor — it rides as the grain or
#     an optional link. Grounding is the router; a churn catalog (monetary_flow + event_ts + customer_id, NO
#     product_type/segment/peer_group/channel) grounds NOTHING here.
#   • LEAKAGE (safety by construction) — a cross-sell PROPENSITY is built from the PRE-purchase BEHAVIOUR
#     (product gaps, engagement, campaign exposure), NEVER the conversion / purchased outcome_label (a
#     leakage anchor the engine refuses). No recipe is near-label — the growth journey's "conversion" is a
#     HARD leakage anchor, not a bordering near-label. CLV is a DECLARED PROJECTION (no data plane computes
#     the forward lifetime value). Fair-lending: no protected-attribute inference for targeting (engine-
#     blocked). Additivity: counts / revenue additive, ratios / share-of-wallet non_additive, propensity n/a.
# The Part-L appendix in docs/…/2026-07-08-banking-feature-template-library.md is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_CROSS_SELL_PIT_STATE = ("point-in-time cross-sell / relationship STATE observed as-of: product holdings / "
                         "share-of-wallet / penetration within (as_of − {window}, as_of], knowable strictly "
                         "≤ as_of, never forward. DESIGN-TIME declaration — no data plane enforces runtime PIT.")
_CLV_PROJECTION = ("CLV / forward revenue is a DECLARED PROJECTION — no data plane computes the lifetime "
                   "value here; the forward projection is a downstream derivation (§D.8), not a bound fact.")
_CROSS_SELL_NO_LABEL = ("built from PRE-purchase BEHAVIOUR (product gaps, engagement, campaign exposure), "
                        "NEVER the conversion / purchased outcome (outcome_label is a leakage anchor the "
                        "engine refuses by construction) — a propensity, not the label.")

CROSS_SELL_TEMPLATES: tuple[Template, ...] = (
    # ── ONBOARDING / ACTIVATION — channel adoption / digital engagement ─────────────────────────────
    # L.1 — channel_adoption_depth (digital / channel engagement)
    Template(
        id="channel_adoption_depth", family="channel_engagement",
        intent="Channel-adoption depth / digital engagement — the distinct servicing channels a customer "
               "uses and how digital-led the mix is (measure=digital_share / distinct_channels / "
               "adoption_trend); an activated, digitally-engaged customer is cross-sell-ready.",
        needs=(Need("channel", "channel"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (90, 180, 365), "measure": ("digital_share", "distinct_channels", "adoption_trend")},
        aggregation="channel_adoption", additivity="non_additive", explain="H",
        use_cases=("cross_sell", "engagement", "next_best_action"),
        pit=_PIT_TRAILING,
        degrade="no channel tag -> SKIP (channel adoption needs an origination/servicing channel).",
        stage="1-activation",
        eligibility=_CROSS_SELL_NO_LABEL,
        notes=("anchor: 'channel' (cross-sell-distinctive, non-structural) routes this off a churn catalog "
               "(channel exists in the taxonomy — no substitution needed).",
               "OUTPUT additivity is measure-dependent: digital_share is a non-additive ratio; "
               "distinct_channels is a mix/diversity (n/a); adoption_trend is n/a — the default carries "
               "the ratio case."),
    ),
    # ── DEEPENING (cross-sell windows) — whitespace, next-best-product, breadth growth, campaigns ─────
    # L.2 — product_gap_whitespace (products the segment holds that this customer lacks)
    Template(
        id="product_gap_whitespace", family="whitespace",
        intent="Product-gap / whitespace — the count of products the customer's SEGMENT typically holds "
               "that THIS customer lacks (measure=gap_count / whitespace_flag); the headline cross-sell "
               "opportunity signal. Compares held product_type vs the segment's typical basket.",
        needs=(Need("product", "product_type"), Need("segment", "segment"),
               Need("open_close", "effective_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("gap_count", "whitespace_flag")},
        aggregation="product_gap", additivity="additive", explain="H",
        use_cases=("cross_sell", "next_best_action", "whitespace"),
        pit=_CROSS_SELL_PIT_STATE,
        degrade="no segment tag -> compute the gap vs the WHOLE eligible product catalog (weaker; FLAG the "
                "loss of the peer-basket comparison).",
        stage="3-deepening",
        eligibility=_CROSS_SELL_NO_LABEL,
        derived=("segment_basket := the product_type set the segment typically holds; gap := basket − held "
                 "— computed DOWNSTREAM (no data plane); whitespace_flag := gap_count > 0.",),
        notes=("anchor: 'product_type' + 'segment' (both cross-sell-distinctive, non-structural) route "
               "this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: gap_count is an additive count; whitespace_flag is "
               "n/a — the default carries the additive count."),
    ),
    # L.3 — next_best_product_propensity (pre-purchase behaviour, NEVER the conversion label)
    Template(
        id="next_best_product_propensity", family="next_best_product",
        source_entity_need_role="entity",   # 3B.1: customer is the source grain (product is related)
        intent="Next-best-product propensity signals — a pre-purchase blend of product gaps, engagement "
               "and spend intensity that ranks the next product a customer is likely to take "
               "(measure=propensity_signal / gap_engagement_score). Built from BEHAVIOUR, NEVER the "
               "purchased outcome.",
        needs=(Need("product", "product_type"), Need("next_product", "product_id", optional=True),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (180, 90, 365), "measure": ("propensity_signal", "gap_engagement_score")},
        aggregation="next_best_product", additivity="n/a", explain="M",
        use_cases=("cross_sell", "next_best_action", "clv"),
        pit=_PIT_TRAILING,
        degrade="no product-holding data -> SKIP (a next-best-product signal needs the held product_type).",
        stage="3-deepening",
        eligibility=_CROSS_SELL_NO_LABEL,
        derived=("propensity_signal := f(product gaps, engagement recency/frequency, spend intensity) — a "
                 "RANKING signal computed DOWNSTREAM (no data plane); the conversion label is NEVER read.",),
        notes=("anchor: 'product_type' (cross-sell-distinctive, non-structural) routes this off a churn "
               "catalog ('product_id' names the candidate next product — optional).",
               "a propensity signal — n/a (a ranking score, not summable); explain M (a blended derived "
               "signal, not a single monotone measure)."),
    ),
    # L.4 — relationship_deepening_breadth (product-breadth GROWTH — the inverse of churn's product_attrition)
    Template(
        id="relationship_deepening_breadth", family="relationship_deepening",
        intent="Relationship-deepening / product-breadth GROWTH — breadth = count(distinct products held) "
               "and its GROWTH over the window (measure=breadth / breadth_growth); the positive inverse of "
               "churn's product_attrition (a DISTINCT recipe from churn's product_breadth).",
        needs=(Need("product", "product_type"), Need("open_close", "effective_date"),
               Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("breadth", "breadth_growth")},
        aggregation="relationship_deepening", additivity="additive", explain="H",
        use_cases=("cross_sell", "share_of_wallet", "clv"),
        pit=_CROSS_SELL_PIT_STATE,
        degrade="no product-holding data -> SKIP.",
        stage="3-deepening",
        eligibility=_CROSS_SELL_NO_LABEL,
        notes=("anchor: 'product_type' (cross-sell-distinctive, non-structural) routes this off a churn "
               "catalog.",
               "distinct id from churn's 'product_breadth' — this is the GROWTH direction (breadth_growth "
               "= breadth(as_of) − breadth(as_of−window)), the inverse of the unbundling attrition signal.",
               "a breadth count / its growth — additive."),
    ),
    # L.5 — campaign_response_recency (campaign exposure/response BEHAVIOUR, NEVER the conversion label)
    Template(
        id="campaign_response_recency", family="campaign_response",
        source_entity_need_role="entity",   # 3B.1: customer is the source grain (campaign is related)
        intent="Campaign response / recency — a customer's response rate, recency or count over exposure "
               "to product-cross-sell campaigns (measure=response_rate / days_since_last_response / "
               "response_count). Built from response BEHAVIOUR, NEVER the conversion outcome.",
        needs=(Need("product", "product_type"), Need("campaign", "campaign_id"),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (90, 180, 365),
                "measure": ("response_rate", "days_since_last_response", "response_count")},
        aggregation="campaign_response", additivity="non_additive", explain="H",
        use_cases=("cross_sell", "next_best_action", "campaign_analytics"),
        pit=_PIT_TRAILING,
        degrade="no campaign-exposure data -> SKIP (campaign response needs campaign touch events).",
        stage="3-deepening",
        eligibility=_CROSS_SELL_NO_LABEL,
        notes=("anchor: 'product_type' (cross-sell-distinctive, non-structural — the promoted product) "
               "routes this off a churn catalog ('campaign_id' is an ENTITY concept — it would "
               "structurally bind ANY grain column, so it cannot be the sole anchor).",
               "OUTPUT additivity is measure-dependent: response_rate is a non-additive ratio; "
               "days_since_last_response is a recency (n/a); response_count is additive — the default "
               "carries the ratio case."),
    ),
    # ── MATURITY — CLV / revenue trajectory, share-of-wallet growth, peer-relative penetration ────────
    # L.6 — clv_revenue_trajectory (monetary_flow + product_type; CLV is a DECLARED PROJECTION)
    Template(
        id="clv_revenue_trajectory", family="clv_revenue",
        source_entity_need_role="entity",   # 3B.1: customer is the source grain (product is related)
        intent="CLV / revenue trajectory by product — customer revenue summed by product_type "
               "(measure=revenue), its trailing trend (measure=revenue_trend) or a forward CLV projection "
               "(measure=clv_projection). Revenue is additive; the CLV is a declared forward projection.",
        needs=(Need("flow_col", "monetary_flow"), Need("product", "product_type"),
               Need("next_product", "product_id", optional=True), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("revenue", "revenue_trend", "clv_projection")},
        aggregation="clv_revenue", additivity="additive", explain="H",
        use_cases=("clv", "cross_sell", "pricing"),
        pit=_PIT_TRAILING,
        degrade="only a single revenue snapshot (no history) -> report the by-product revenue sum (no "
                "trend / projection).",
        stage="4-maturity",
        eligibility="single currency — convert to base first. " + _CROSS_SELL_NO_LABEL,
        derived=("clv_projection := a forward customer-lifetime-value projection from the revenue "
                 "trajectory — computed DOWNSTREAM (no data plane); a DECLARED projection, not a fact.",),
        notes=("anchor: 'product_type' (cross-sell-distinctive, non-structural) routes this off a churn "
               "catalog (monetary_flow + event_ts + customer_id are SHARED with churn — product_type is "
               "load-bearing for routing).",
               _CLV_PROJECTION,
               "OUTPUT additivity is measure-dependent: the by-product revenue SUM is additive; a "
               "revenue_trend is n/a; a clv_projection is n/a (a projection) — the default carries the "
               "additive revenue."),
    ),
    # L.7 — share_of_wallet_growth (held products vs the eligible catalog — a growing share)
    Template(
        id="share_of_wallet_growth", family="share_of_wallet",
        intent="Share-of-wallet growth — held products (or revenue) as a share of the customer's eligible "
               "product catalog / estimated total wallet, and its GROWTH over the window (measure=sow_level "
               "/ sow_growth); a rising share-of-wallet is relationship maturity.",
        needs=(Need("product", "product_type"), Need("flow_col", "monetary_flow", optional=True),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("sow_level", "sow_growth")},
        aggregation="share_of_wallet", additivity="non_additive", explain="H",
        use_cases=("cross_sell", "share_of_wallet", "clv"),
        pit=_CROSS_SELL_PIT_STATE,
        degrade="no revenue for a value-weighted wallet -> compute a product-count share of the eligible "
                "catalog (weaker; FLAG the count-vs-value scope).",
        stage="4-maturity",
        eligibility=_CROSS_SELL_NO_LABEL,
        derived=("share_of_wallet := held products (or revenue) ÷ the eligible product catalog / estimated "
                 "total wallet — the estimated wallet is a DECLARED downstream derivation (§D.8).",),
        notes=("anchor: 'product_type' (cross-sell-distinctive, non-structural) routes this off a churn "
               "catalog.",
               "a share-of-wallet ratio (or its growth) — non-additive; compute per customer, never sum."),
    ),
    # L.8 — segment_relative_penetration (peer_group under-penetration)
    Template(
        id="segment_relative_penetration", family="peer_penetration",
        intent="Segment-relative under-penetration — how a customer's product holding / revenue compares "
               "to their PEER GROUP (measure=penetration_gap / relative_holding_index); a customer "
               "under-penetrated vs peers is a cross-sell target.",
        needs=(Need("peer", "peer_group"), Need("product", "product_type", optional=True),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("penetration_gap", "relative_holding_index")},
        aggregation="peer_penetration", additivity="non_additive", explain="H",
        use_cases=("cross_sell", "next_best_action", "whitespace"),
        pit=_CROSS_SELL_PIT_STATE,
        degrade="no product breakdown -> report the peer-relative revenue index only (no per-product gap).",
        stage="4-maturity",
        eligibility=_CROSS_SELL_NO_LABEL,
        notes=("anchor: 'peer_group' (cross-sell-distinctive, non-structural — the benchmarking cohort) "
               "routes this off a churn catalog.",
               "a peer-relative penetration gap / index — non-additive (a benchmarked ratio)."),
    ),
    # ── AGGREGATION — household / relationship-manager rollup ─────────────────────────────────────────
    # L.9 — household_relationship_value (household / RM aggregation grain)
    Template(
        id="household_relationship_value", family="relationship_aggregation",
        source_entity_need_role="entity",   # 3B.1: household is the source grain (relationship_manager is related)
        intent="Household / relationship aggregation — product breadth, revenue or revenue-share summed "
               "across a HOUSEHOLD (or a relationship-manager's book) (measure=household_breadth / "
               "household_revenue / household_revenue_share); the relationship-primacy rollup grain.",
        needs=(Need("product", "product_type"), Need("entity", "household_id"),
               Need("rm", "relationship_manager_id", optional=True), Need("asof", "as_of_date")),
        params={"window": (365, 180, 90),
                "measure": ("household_breadth", "household_revenue", "household_revenue_share")},
        aggregation="relationship_aggregation", additivity="additive", explain="H",
        use_cases=("cross_sell", "share_of_wallet", "clv"),
        pit=_CROSS_SELL_PIT_STATE,
        degrade="no household grain -> aggregate on the relationship_manager's book instead (a coarser "
                "advisor-level rollup; FLAG the changed grain).",
        stage="aggregation",
        eligibility=_CROSS_SELL_NO_LABEL,
        notes=("anchor: 'product_type' (cross-sell-distinctive, non-structural) routes this off a churn "
               "catalog ('household_id' / 'relationship_manager_id' are ENTITY concepts — the aggregation "
               "grain, not the routing anchor).",
               "OUTPUT additivity is measure-dependent: household_breadth / household_revenue are additive "
               "(counts / a revenue flow); a household_revenue_share is non-additive — the default carries "
               "the additive rollup."),
    ),
    # ── DEEPENING (readiness) — tenure-based upsell readiness ─────────────────────────────────────────
    # L.10 — tenure_upsell_readiness (product_type + tenure)
    Template(
        id="tenure_upsell_readiness", family="upsell_readiness",
        intent="Tenure-based upsell readiness — combines relationship tenure (as_of − origination) with "
               "the held product_type to score whether a customer is seasoned enough for a next-product "
               "upsell (measure=upsell_ready_flag / tenure_gap_score); a seasoning-gated cross-sell signal.",
        needs=(Need("product", "product_type"), Need("origination", "effective_date"),
               Need("asof", "as_of_date"), Need("entity", "customer_id")),
        params={"window": (365, 180, 90), "measure": ("upsell_ready_flag", "tenure_gap_score")},
        aggregation="upsell_readiness", additivity="n/a", explain="H",
        use_cases=("cross_sell", "next_best_action", "pricing"),
        pit=_CROSS_SELL_PIT_STATE,
        degrade="no product-holding data -> SKIP (readiness is scoped to the held product mix).",
        stage="3-deepening",
        eligibility=_CROSS_SELL_NO_LABEL,
        derived=("upsell_ready := tenure ≥ a product-specific seasoning threshold AND a whitespace gap "
                 "exists — computed DOWNSTREAM (no data plane).",),
        notes=("anchor: 'product_type' (cross-sell-distinctive, non-structural) routes this off a churn "
               "catalog (tenure alone — effective_date + as_of — is generic and would cross-surface).",
               "a readiness flag / tenure-gap score — n/a (not summable)."),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The corporate / SME trade & supply-chain-finance templates — the §B15 multi-product, GROUP-level set
# authored to Part-F depth (Phase-3 Pass-6, the FINAL breadth pass — completes the 15-family library).
#
# Corporate is MULTI-PRODUCT + HIERARCHICAL — features aggregate across product families AND up the group
# (§A6 group_exposure_sum); cash-flow / trade-flow-based, not just financials. Coverage (§B15): trade
# finance (LC/guarantee), invoice / receivables finance, supply-chain finance, working-capital / facility,
# and the corporate deterioration funnel (mirrors credit at GROUP level): HEALTHY → EARLY STRESS
# (utilisation↑, DSO↑, term extension) → COVENANT PRESSURE (headroom↓) → BREACH ⚠ → DEFAULT/RESTRUCTURE.
# Two authoring disciplines are load-bearing:
#   • ROUTING — every recipe REQUIRES a corporate-distinctive, NON-STRUCTURAL concept (limit / limit_type /
#     contingent_exposure / covenant / syndication_share / collateral_type / ownership_percentage — that
#     binds only by exact concept match). An ENTITY concept (invoice_id / obligor_id / guarantor_id /
#     pooling_structure_id / facility_id) gets structural is_grain credit in _match and would bind ANY grain
#     column, so it is NEVER a sole anchor — it rides as the grain / an aggregation link. Grounding is the
#     router; a churn catalog (with none of these corporate concepts) grounds NOTHING here (the locked
#     invariant: ALL_TEMPLATES on the churn _CATALOG = exactly the churn lens).
#   • NEAR-LABEL — a covenant headroom / breach-proximity BORDERS the group default/restructure label
#     (covenant is a near_label concept) -> near_label=True + a ⚠ note (observe strictly pre-breach; the
#     default_flag / outcome_label leakage anchors are NEVER an input — the engine refuses them). DSO /
#     trade-cycle length / working-capital gap are DECLARED PROJECTIONS (no data plane computes them).
#     Additivity: exposures / contingents semi_additive (STOCKS — latest over time, never summed across
#     dates), utilisation / concentration / DSO non_additive (ratios), counts additive. Group exposures
#     aggregate UP the ownership hierarchy (ownership_percentage is the consolidation weight).
# The Part-L appendix in docs/…/2026-07-08-banking-feature-template-library.md is the doc source of record.
# ──────────────────────────────────────────────────────────────────────────────────────────────────
_CORP_PIT_STATE = ("point-in-time corporate / group-exposure STATE observed as-of: the latest exposure / "
                   "limit / covenant / utilisation within (as_of − {window}, as_of], knowable strictly ≤ "
                   "as_of, never forward. DESIGN-TIME declaration — no data plane enforces runtime PIT.")
_CORP_NEAR_LABEL_PREFIX = ("⚠ NEAR-LABEL: observe the covenant-headroom / breach signal STRICTLY before the "
                           "group default / restructure (never on/after it; window ≠ the label window); the "
                           "3-part leakage control must FLAG it. ")
_CORP_DSO_PROJECTION = ("DSO / trade-cycle length / working-capital gap is a DECLARED PROJECTION over the "
                        "invoice + flow history — no data plane computes it here (a downstream derivation, "
                        "§D.8), not a bound fact.")
_CORP_GROUP = ("GROUP-level: exposures aggregate UP the ownership hierarchy (ownership_percentage is the "
               "consolidation weight) — a subsidiary's risk needs the group total (§A6 group_exposure_sum).")
_CORP_SINGLE_CCY = ("single currency — convert to base first; an exposure / contingent STOCK takes the "
                    "LATEST over time (never summed across dates).")

CORPORATE_TRADE_TEMPLATES: tuple[Template, ...] = (
    # ── WORKING CAPITAL / FACILITY — utilisation & headroom (limit + contingent) ─────────────────────
    # L.11 — facility_utilisation_headroom
    Template(
        id="facility_utilisation_headroom", family="facility_utilisation",
        intent="Facility utilisation & headroom — drawn exposure ÷ limit (measure=utilisation), the "
               "remaining headroom, or the undrawn (contingent) share; rising utilisation into a shrinking "
               "headroom is the classic corporate early-stress signal.",
        needs=(Need("limit_col", "limit"), Need("contingent_col", "contingent_exposure", optional=True),
               Need("drawn_col", "monetary_stock", optional=True), Need("asof", "as_of_date"),
               Need("entity", "facility_id")),
        params={"window": (90, 180, 365), "measure": ("utilisation", "headroom", "undrawn_share")},
        aggregation="facility_utilisation", additivity="non_additive", explain="H",
        use_cases=("trade_finance", "working_capital", "limit_management"),
        pit=_CORP_PIT_STATE,
        degrade="no drawn balance -> size utilisation against the contingent (committed) line only (FLAG "
                "the drawn-vs-committed scope).",
        stage="1-early-stress",
        eligibility=_CORP_SINGLE_CCY,
        notes=("anchor: 'limit' (corporate-distinctive ceiling, non-structural) routes this off a churn "
               "catalog (nested sub-limits — never naively sum a nested limit; §E limit-vs-balance).",
               "a utilisation ratio / headroom — non-additive; compute per facility, never sum."),
    ),
    # ── TRADE FINANCE (LC / guarantee) — contingent usage & rollover ─────────────────────────────────
    # L.12 — lc_guarantee_rollover
    Template(
        id="lc_guarantee_rollover", family="trade_finance_contingent",
        intent="Letter-of-credit / guarantee usage & rollover — the contingent (LC / guarantee) exposure "
               "level (measure=contingent_level), its utilisation, or the rollover rate of expiring "
               "instruments; drawdowns on undrawn LCs are a stress signal (contingent converting on-BS).",
        needs=(Need("contingent_col", "contingent_exposure"), Need("event_ts", "event_timestamp", optional=True),
               Need("asof", "as_of_date"), Need("entity", "facility_id")),
        params={"window": (90, 180, 365), "measure": ("contingent_level", "utilisation", "rollover_rate")},
        aggregation="lc_guarantee", additivity="semi_additive", explain="H",
        use_cases=("trade_finance", "supply_chain_finance", "limit_management"),
        pit=_CORP_PIT_STATE,
        degrade="only a single contingent snapshot (no history) -> report the contingent level (no "
                "rollover / utilisation trend).",
        stage="trade-finance",
        eligibility=_CORP_SINGLE_CCY,
        notes=("anchor: 'contingent_exposure' (corporate-distinctive — an LC / guarantee / committed line, "
               "non-structural) routes this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: the contingent LEVEL is a semi-additive stock (sum "
               "across instruments, latest over time — converts on drawdown via the ccf); utilisation / "
               "rollover_rate are non-additive ratios — the default carries the semi-additive stock."),
    ),
    # ── INVOICE / RECEIVABLES FINANCE — DSO / dilution / debtor concentration ─────────────────────────
    # L.13 — invoice_finance_dynamics
    Template(
        id="invoice_finance_dynamics", family="invoice_finance",
        source_entity_need_role="entity",   # 3B.1: obligor is the source grain (invoice is related)
        intent="Invoice / receivables-finance behaviour — days-sales-outstanding (measure=dso), invoice "
               "dilution (unpaid / credit-noted) rate (measure=dilution_rate) or debtor concentration "
               "(measure=debtor_concentration_hhi) over the financed receivables pool; rising DSO / "
               "dilution is corporate cash stress.",
        needs=(Need("collateral_col", "collateral_type"), Need("invoice", "invoice_id"),
               Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp"),
               Need("entity", "obligor_id")),
        params={"window": (90, 180, 365), "measure": ("dso", "dilution_rate", "debtor_concentration_hhi")},
        aggregation="invoice_finance", additivity="non_additive", explain="H",
        use_cases=("trade_finance", "working_capital", "receivables_finance"),
        pit=_CORP_PIT_STATE,
        degrade="no receivables-collateral tag -> compute DSO / dilution over ALL invoices (weaker; FLAG "
                "the loss of the financed-pool scope).",
        stage="1-early-stress",
        eligibility=_CORP_SINGLE_CCY + " " + _CORP_DSO_PROJECTION,
        derived=("dso := mean(payment_date − invoice_date) over the financed invoices; dilution_rate := "
                 "credit-noted / unpaid ÷ invoiced — DECLARED downstream projections (no data plane).",),
        notes=("anchor: 'collateral_type' (corporate-distinctive — receivables ARE a collateral_type, "
               "non-structural) routes this off a churn catalog ('invoice_id' is an ENTITY concept — the "
               "receivables grain, not the routing anchor).",
               "concept sub: no dedicated DSO / dilution / receivables-finance concept — the receivables "
               "pool binds on collateral_type=receivables + invoice_id, and DSO / dilution are declared "
               "downstream projections.",
               "DSO / dilution / debtor concentration — non-additive (a duration / ratio / index)."),
    ),
    # ── SUPPLY-CHAIN FINANCE — anchor-buyer dependence, payment-term extension, program utilisation ────
    # L.14 — supply_chain_finance_dynamics
    Template(
        id="supply_chain_finance_dynamics", family="supply_chain_finance",
        intent="Supply-chain-finance (payables / receivables) dynamics — anchor-buyer dependence (the SCF "
               "program hinging on one anchor's health), buyer payment-term extension (extending terms = "
               "stress), or program utilisation (measure=anchor_buyer_dependence / payment_term_extension "
               "/ program_utilisation) over the committed SCF program.",
        needs=(Need("contingent_col", "contingent_exposure"), Need("flow_col", "monetary_flow", optional=True),
               Need("event_ts", "event_timestamp", optional=True), Need("asof", "as_of_date"),
               Need("entity", "obligor_id")),
        params={"window": (90, 180, 365),
                "measure": ("anchor_buyer_dependence", "payment_term_extension", "program_utilisation")},
        aggregation="supply_chain_finance", additivity="non_additive", explain="H",
        use_cases=("supply_chain_finance", "trade_finance", "working_capital"),
        pit=_CORP_PIT_STATE,
        degrade="no committed-program (contingent) line -> SKIP (an SCF signal needs the program exposure).",
        stage="1-early-stress",
        eligibility=_CORP_SINGLE_CCY,
        derived=("anchor_buyer_dependence := the anchor buyer's share of the program's financed flow; "
                 "payment_term_extension := Δ(buyer payment terms) — DECLARED downstream (no data plane).",),
        notes=("anchor: 'contingent_exposure' (corporate-distinctive — the committed SCF program line, "
               "non-structural) routes this off a churn catalog.",
               "anchor-buyer dependence / term extension / program utilisation — non-additive (shares / "
               "ratios); compute per program/obligor, never sum."),
    ),
    # ── COVENANT PRESSURE — headroom & breach proximity (NEAR-LABEL) ──────────────────────────────────
    # L.15 — covenant_headroom_breach
    Template(
        id="covenant_headroom_breach", family="covenant",
        intent="Covenant headroom & breach proximity — the margin between a covenant's actual and its "
               "threshold (leverage / DSCR / ICR), its proximity to a breach, or a breached flag / trend "
               "(measure=headroom / breach_proximity / breached_flag / trend); a shrinking or negative "
               "headroom is the corporate breach path.",
        needs=(Need("covenant_col", "covenant"), Need("asof", "as_of_date"),
               Need("entity", "obligor_id")),
        params={"window": (90, 180, 365), "measure": ("headroom", "breach_proximity", "breached_flag", "trend")},
        aggregation="covenant_headroom", additivity="non_additive", explain="H",
        use_cases=("trade_finance", "working_capital", "early_warning"),
        pit=_CORP_PIT_STATE,
        stage="2-covenant-pressure",
        near_label=True,
        eligibility=_CORP_NEAR_LABEL_PREFIX + "a covenant breach borders the group default / restructure "
                    "label; income / affordability inputs are SENSITIVE. " + _CORP_GROUP,
        notes=("anchor: 'covenant' (near-label, corporate-distinctive, non-structural) — leverage / DSCR / "
               "ICR headroom — routes this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: headroom / breach_proximity / DSCR are "
               "non-additive ratios; breached_flag is n/a — the default carries the ratio case.",
               "borders the group default / restructure label — observe strictly pre-breach."),
    ),
    # ── SYNDICATION — share concentration ────────────────────────────────────────────────────────────
    # L.16 — syndication_concentration
    Template(
        id="syndication_concentration", family="syndication",
        intent="Syndication-share concentration — the lender's share (%) of a syndicated facility "
               "(measure=share_level), the concentration of the book across syndicated deals (an HHI, "
               "measure=concentration_hhi), or the top-deal share; a book concentrated in a few "
               "syndications is fragile.",
        needs=(Need("syndication_col", "syndication_share"), Need("asof", "as_of_date"),
               Need("entity", "facility_id")),
        params={"window": (365, 180, 90), "measure": ("share_level", "concentration_hhi", "top_deal_share")},
        aggregation="syndication_concentration", additivity="non_additive", explain="M",
        use_cases=("trade_finance", "concentration_risk", "limit_management"),
        pit=_CORP_PIT_STATE,
        degrade="only a single deal's share -> report the syndication share level (no book concentration).",
        stage="concentration",
        eligibility=_CORP_SINGLE_CCY,
        notes=("anchor: 'syndication_share' (corporate-distinctive, non-structural) routes this off a "
               "churn catalog (shares sum to 100% within a deal — a constraint, not an aggregation).",
               "a share / concentration index (HHI / top-share) — non-additive."),
    ),
    # ── GROUP STRUCTURE — group-exposure aggregation & single-obligor concentration ──────────────────
    # L.17 — group_exposure_aggregation
    Template(
        id="group_exposure_aggregation", family="group_exposure",
        intent="Group-exposure aggregation & single-obligor concentration — combined exposure summed UP "
               "the ownership hierarchy (measure=group_exposure), the single-obligor share of the group "
               "(measure=single_obligor_share) or a group concentration HHI; a subsidiary's risk needs the "
               "GROUP total (§A6 group_exposure_sum).",
        needs=(Need("ownership_col", "ownership_percentage"), Need("exposure_col", "monetary_stock", optional=True),
               Need("asof", "as_of_date"), Need("entity", "obligor_id")),
        params={"window": (365, 180, 90),
                "measure": ("group_exposure", "single_obligor_share", "group_concentration_hhi")},
        aggregation="group_exposure", additivity="semi_additive", explain="H",
        use_cases=("trade_finance", "concentration_risk", "limit_management"),
        pit=_CORP_PIT_STATE,
        degrade="no exposure stock to aggregate -> report the ownership-weighted concentration only (no "
                "group exposure amount).",
        stage="group-aggregation",
        eligibility=_CORP_SINGLE_CCY + " " + _CORP_GROUP,
        derived=("group_exposure := Σ (exposure × ownership_percentage) UP the ownership hierarchy — a "
                 "DECLARED consolidation (no data plane); single_obligor_share := obligor ÷ group total.",),
        notes=("anchor: 'ownership_percentage' (corporate-distinctive — the group consolidation weight, "
               "non-structural) routes this off a churn catalog ('obligor_id' is an ENTITY concept — the "
               "group grain, not the routing anchor).",
               "OUTPUT additivity is measure-dependent: a group_exposure amount is a semi-additive stock "
               "(sum across the group, latest over time); a share / HHI is non-additive — the default "
               "carries the semi-additive group stock."),
    ),
    # ── CREDIT MITIGATION — guarantor reliance ───────────────────────────────────────────────────────
    # L.18 — guarantor_reliance
    Template(
        id="guarantor_reliance", family="guarantor_support",
        source_entity_need_role="entity",   # 3B.1: obligor is the source grain (guarantor is related)
        intent="Guarantor reliance — the share of exposure covered by a guarantee (measure=guaranteed_"
               "share), the concentration of reliance on a few guarantors (measure=guarantor_concentration)"
               " or a heavy-reliance flag; heavy reliance on one guarantor is credit-mitigation fragility.",
        needs=(Need("collateral_col", "collateral_type"), Need("guarantor", "guarantor_id"),
               Need("contingent_col", "contingent_exposure", optional=True), Need("asof", "as_of_date"),
               Need("entity", "obligor_id")),
        params={"window": (365, 180, 90),
                "measure": ("guaranteed_share", "guarantor_concentration", "reliance_flag")},
        aggregation="guarantor_reliance", additivity="non_additive", explain="H",
        use_cases=("trade_finance", "concentration_risk", "credit_mitigation"),
        pit=_CORP_PIT_STATE,
        degrade="no guaranteed (contingent) amount -> report the distinct-guarantor count / flag only (no "
                "guaranteed share).",
        stage="credit-mitigation",
        eligibility=_CORP_SINGLE_CCY,
        notes=("anchor: 'collateral_type' (corporate-distinctive — a guarantee IS a collateral_type, "
               "non-structural) routes this off a churn catalog ('guarantor_id' is an ENTITY concept — the "
               "guarantor grain, not the routing anchor).",
               "a guaranteed share / guarantor concentration — non-additive (a ratio / index)."),
    ),
    # ── WORKING CAPITAL — trade-cycle / working-capital gap (DECLARED PROJECTION) ─────────────────────
    # L.19 — trade_cycle_working_capital
    Template(
        id="trade_cycle_working_capital", family="working_capital",
        intent="Trade-cycle / working-capital gap — the working-capital gap (DSO + DIO − DPO, "
               "measure=working_capital_gap), the trade-cycle length (issue→settlement — lengthening = "
               "stress, measure=trade_cycle_length) or its trend, scoped to the trade / working-capital "
               "facility by its limit_type; a widening gap is cash stress.",
        needs=(Need("limit_type_col", "limit_type"), Need("limit_col", "limit", optional=True),
               Need("flow_col", "monetary_flow", optional=True), Need("event_ts", "event_timestamp", optional=True),
               Need("asof", "as_of_date"), Need("entity", "obligor_id")),
        params={"window": (90, 180, 365),
                "measure": ("working_capital_gap", "trade_cycle_length", "wc_gap_trend")},
        aggregation="trade_cycle", additivity="non_additive", explain="H",
        use_cases=("working_capital", "trade_finance", "early_warning"),
        pit=_CORP_PIT_STATE,
        degrade="no trade / working-capital limit_type -> SKIP (the gap is scoped to a trade-finance "
                "facility).",
        stage="working-capital",
        eligibility=_CORP_SINGLE_CCY + " " + _CORP_DSO_PROJECTION,
        derived=("working_capital_gap := DSO + DIO − DPO; trade_cycle_length := settlement − issue — "
                 "DECLARED downstream projections over the trade flows (no data plane).",),
        notes=("anchor: 'limit_type' (corporate-distinctive — the trade / working-capital facility type, "
               "non-structural) routes this off a churn catalog.",
               "OUTPUT additivity is measure-dependent: working_capital_gap is a non-additive amount/ratio;"
               " trade_cycle_length is a duration (n/a) — the default carries the non-additive gap."),
    ),
    # ── CASH MANAGEMENT — pooling-structure utilisation ──────────────────────────────────────────────
    # L.20 — pooling_structure_utilisation
    Template(
        id="pooling_structure_utilisation", family="cash_pooling",
        intent="Pooling-structure utilisation — the utilisation of a cash-pooling (notional / zero-"
               "balancing) structure against its pool limit (measure=pool_utilisation), the notional-"
               "pooling funding benefit, or an intraday-peak share; a pool running hard against its limit "
               "is liquidity stress.",
        needs=(Need("limit_col", "limit"), Need("balance_col", "monetary_stock", optional=True),
               Need("asof", "as_of_date"), Need("entity", "pooling_structure_id")),
        params={"window": (90, 180, 365),
                "measure": ("pool_utilisation", "notional_pool_benefit", "intraday_peak_share")},
        aggregation="pool_utilisation", additivity="non_additive", explain="M",
        use_cases=("working_capital", "cash_management", "liquidity_risk"),
        pit=_CORP_PIT_STATE,
        degrade="no pooled balance -> report the pool limit / headroom only (utilisation undefined; FLAG).",
        stage="cash-management",
        eligibility=_CORP_SINGLE_CCY,
        notes=("anchor: 'limit' (corporate-distinctive pool ceiling, non-structural) routes this off a "
               "churn catalog ('pooling_structure_id' is an ENTITY concept — the pool grain, not the "
               "routing anchor).",
               "a pool utilisation / benefit / peak share — non-additive; compute per pool, never sum."),
    ),
    # ── CORPORATE DETERIORATION FUNNEL — cross-product stress count (a strong early-warning) ──────────
    # L.21 — cross_product_stress_count
    Template(
        id="cross_product_stress_count", family="cross_product_stress",
        intent="Cross-product stress count — the number of product lines simultaneously stressed "
               "(utilisation↑ across facilities) across a group (measure=stressed_line_count), the combined "
               "exposure trend, or a trade-flow decline; multiple lines stressed at once is a strong "
               "corporate early-warning.",
        needs=(Need("limit_col", "limit"), Need("contingent_col", "contingent_exposure", optional=True),
               Need("asof", "as_of_date"), Need("entity", "obligor_id")),
        params={"window": (90, 180, 365),
                "measure": ("stressed_line_count", "combined_exposure_trend", "trade_flow_decline")},
        aggregation="cross_product_stress", additivity="additive", explain="H",
        use_cases=("trade_finance", "working_capital", "early_warning"),
        pit=_CORP_PIT_STATE,
        degrade="only a single facility -> report that line's utilisation (no cross-product count).",
        stage="3-deterioration",
        eligibility=_CORP_SINGLE_CCY + " " + _CORP_GROUP,
        derived=("stressed_line := a facility whose utilisation exceeds a stress threshold; "
                 "stressed_line_count := Σ stressed lines across the group — DECLARED downstream (no data "
                 "plane).",),
        notes=("anchor: 'limit' (corporate-distinctive — utilisation across product lines, non-structural) "
               "routes this off a churn catalog (an early-warning, NOT near-label — it counts stress "
               "BEFORE any breach/default).",
               "OUTPUT additivity is measure-dependent: stressed_line_count is an additive count; a "
               "combined_exposure_trend / trade_flow_decline is n/a (a slope) — the default carries the "
               "additive count."),
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
    + MARKETS_TEMPLATES + CUSTODY_TEMPLATES + ASSET_MGMT_TEMPLATES
    + INSURANCE_TEMPLATES + ISLAMIC_TEMPLATES + ESG_TEMPLATES
    + CROSS_SELL_TEMPLATES + CORPORATE_TRADE_TEMPLATES)


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
    _validate_family(INSURANCE_TEMPLATES, "INSURANCE_TEMPLATES", set())
    _validate_family(ISLAMIC_TEMPLATES, "ISLAMIC_TEMPLATES", set())
    _validate_family(ESG_TEMPLATES, "ESG_TEMPLATES", set())
    _validate_family(CROSS_SELL_TEMPLATES, "CROSS_SELL_TEMPLATES", set())
    _validate_family(CORPORATE_TRADE_TEMPLATES, "CORPORATE_TRADE_TEMPLATES", set())
    _validate_family(ALL_TEMPLATES, "ALL_TEMPLATES", set())


_validate_registry()
