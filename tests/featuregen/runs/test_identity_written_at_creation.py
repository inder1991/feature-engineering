"""The creation transaction writes identity AFTER the chain (spec §6.1: 'written in the same
transaction that completes run creation').

An API-level test through the generate route is heavyweight (LLM client fakes, catalog projection,
snapshot build), so the SEAM contract is tested instead: the route's step order (input →
considered + snapshot → identity) is exactly the order under which the writer succeeds, and the
route module genuinely calls the writer.
"""
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.runs.run_identity import record_run_identity

_ENV = mint_test_identity(subject="user:priya", role_claims=("feature_engineer",), tenant="t1")


def test_creation_order_input_considered_then_identity(db):
    # seed_run_chain performs, in order, the same writes the route performs before the seam:
    # intent → run → recognition → confirmed scope → generation input → snapshot → considered.
    seed_run_chain(db, run_id="wire-a")
    assert record_run_identity(db, "wire-a", _ENV) is not None
    assert db.execute("SELECT count(*) FROM feature_run_identity "
                      "WHERE generation_run_id='wire-a'").fetchone()[0] == 1


def test_contract_route_calls_the_writer():
    import inspect

    from featuregen.api.routes import contract
    src = inspect.getsource(contract)
    assert "record_run_identity(" in src, (
        "the generate endpoint must write feature_run_identity in its creation transaction")


# Ordering (identity AFTER the considered set + snapshot) is proved BEHAVIOURALLY, through the real
# route, in tests/featuregen/api/test_run_identity_at_creation.py: called any earlier there would be
# no considered revision and no snapshot to hash, so the row it asserts could not exist.
