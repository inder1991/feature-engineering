# NOTE: fixtures use REAL concepts/entities verified against the registry + graph:
#   transaction_id -> entity_link "transaction"; customer_id -> "customer"; monetary_flow is entity-neutral.
#   "transaction"->"account" is DERIVABLE and "account"->"account" is EXACT in ENTITY_GRAPH (38 entities).
#   EntityCompatibility members are UPPERCASE (EXACT/DERIVABLE/AMBIGUOUS/UNKNOWN).
from featuregen.overlay.upload.planner.assembly import ingredient_eligibility, semantic_rollup_paths
from featuregen.overlay.upload.planner.contracts import ReasonCode
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility
from featuregen.overlay.upload.templates import Need, Template


def _tmpl(needs, *, source_entity_need_role=None):
    return Template(id="t3b3b", family="f", intent="i", needs=needs, params={}, aggregation="sum",
                    additivity="additive", explain="M", use_cases=(), pit="trailing",
                    source_entity_need_role=source_entity_need_role)


def test_single_source_entity_eligible():
    # a lone transaction-grain key -> source resolves to 'transaction'; nothing gates
    t = _tmpl((Need(role="txn", concept="transaction_id"),))
    e = ingredient_eligibility(t)
    assert e.eligible is True and e.source_entity == "transaction"


def test_multi_grain_ingredient_rejected():
    # source anchored on the transaction key; a REQUIRED customer-grain need is a second grain -> rejected
    t = _tmpl((Need(role="txn", concept="transaction_id"), Need(role="cust", concept="customer_id")),
              source_entity_need_role="txn")
    e = ingredient_eligibility(t)
    assert e.eligible is False and e.reason is ReasonCode.unsupported_multi_grain_ingredients


def test_no_single_source_grain_is_skipped_not_rejected():
    # an entity-neutral measure-only recipe has NO SOURCE_ENTITY_KEY -> skipped, NOT a rejection
    t = _tmpl((Need(role="amt", concept="monetary_flow"),))
    e = ingredient_eligibility(t)
    assert e.eligible is False and e.reason is None


def test_semantic_rollup_paths_derivable():
    paths, status = semantic_rollup_paths("transaction", "account")
    assert status is EntityCompatibility.DERIVABLE
    assert paths and all(p.hops[0].from_entity == "transaction" for p in paths)


def test_exact_source_equals_target_is_empty_path():
    paths, status = semantic_rollup_paths("account", "account")
    assert status is EntityCompatibility.EXACT and paths == ()
