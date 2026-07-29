"""One marker for every test that needs a cross-catalog ROLL-UP to be production-safe.

The fail-close correction removed the fabricated cardinality from ``declarations._hop_evidence``: a
bridge-rollup hop used to report ``many_to_one`` "BY CONSTRUCTION", which was an ASSUMPTION about the
far side's grain dressed as evidence. Nothing in a bridge attests the direction of the join or the
grain it lands on; if the far side is not at that grain the hop is 1:N, rows multiply, and every SUM
over it inflates into a plausible number that raises no error. So the hop now resolves UNKNOWN.

**What that costs, stated plainly rather than hidden behind rewritten assertions.** In this codebase
that fabricated constant was the ONLY physical evidence a cross-catalog roll-up hop ever had. With it
gone, every governed multi-source contract that crosses a bridge as a roll-up realizer fails closed
on ``aggregation_unsafe_on_path`` / ``unresolved_aggregation_declaration``. The gold corpus is built
entirely on one such topology (``core_banking.transactions`` -> bridge at account -> intra-wealth
realization -> ``wealth.customers``), so NONE of its authoritative shapes can resolve today.

The tests below assert that a governed cross-catalog contract RESOLVES. That capability is
deliberately withdrawn, not broken, so they are skipped rather than inverted: an inverted gold that
asserts "the engine fails" would be worse than no gold at all, and a weakened criterion would quietly
survive the day the capability comes back. Each skip is one line and deleting it is the whole
re-arming.

**What unblocks them: DIRECTIONAL REALIZATIONS** — which direction the join runs, at what
cardinality, in what scope, with what fan-out — derived from evidence rather than asserted. Note that
the narrower half of that evidence is ALREADY in these fixtures: ``_seed_all_hop_grains`` establishes
a VERIFIED grain fact on every hop endpoint, and a bridge whose far endpoint IS its table's governed
grain really is many-to-one into that table. ``_hop_evidence`` never read it; it asserted the
conclusion instead. Deriving the cardinality from that governed grain fact (and returning UNKNOWN
without it) would re-arm most of this corpus without fabricating anything — see the fail-close
correction report for why that was left as a product decision rather than taken here.

AVAILABILITY is unaffected and must stay that way: the bridge is still discoverable, still returned
by ``cross_catalog_links``, still in ``active_bridges``, still usable for suggestions and bounded
sandbox analysis. Only the silent claim of a production-safe fan-in is gone. Any "fix" that re-greens
these by dropping the bridge from the active set is a different and worse bug — see
``test_withholding_the_cardinality_claim_does_not_withdraw_the_bridge``.
"""
from __future__ import annotations

import pytest

_REASON = (
    "cross-catalog roll-up cardinality is UNATTESTED since the fail-close correction "
    "(declarations._hop_evidence no longer fabricates many_to_one), so no governed multi-source "
    "contract over the bridged gold topology can resolve. Re-arms when directional realizations "
    "supply real fan-in evidence — see tests/.../planner/_unattested_bridge.py"
)

#: Apply to any test whose subject is a RESOLVED governed cross-catalog contract.
needs_attested_bridge_cardinality = pytest.mark.skip(reason=_REASON)
