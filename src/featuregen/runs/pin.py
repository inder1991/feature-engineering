"""The 1101 selection→formula pin as ONE fact, for every surface that gates on it.

Two surfaces answer for the same absent table. `api.routes.build_sets` refuses the WRITE — a build
set declared before the pin converts 1101's clean zero-row NOT NULL into a nullable column plus a
backfill of exactly the rows it constrains. `runs.projection` labels the `GENERATE_PREVIEW` rail
stage UNAVAILABLE, so a person is told the entrance is shut instead of walking into it.

They must agree BY CONSTRUCTION, not by two people remembering to copy a string: a rail reading
NOT_STARTED over an entrance that refuses is the false rail spec §7 [R3.1] forbids. So the probe and
the code string live here once and neither call site spells them again.

Only the FACT is shared. What each surface builds from it stays its own — the route raises an
`HTTPException` carrying the operator sentence, the projection emits a rail entry — because the two
refusals are read by different audiences and are not one object.
"""
from __future__ import annotations

import psycopg

#: The closed vocabulary token BOTH surfaces answer with: the 409's `code` and the rail entry's
#: `reason_code` are the same string because they are the same refusal seen from two sides.
PRE_PIN_REASON_CODE = "BUILD_SET_DECLARATION_WITHHELD_PRE_PIN"


def pin_exists(conn: psycopg.Connection) -> bool:
    """Whether migration 1101's `selection_formula_binding` has landed in THIS database.

    Availability is DERIVED from the deployment, never a static list (spec §7 [R3.1]): the answer is
    read from the database actually being served, so an environment where 1101 has not been applied
    cannot be shown a stage its only entrance would refuse.

    **Self-retiring by design.** There is no flag and nothing to remember to remove: the moment 1101
    applies, `to_regclass` returns the relation, this answers True forever after, and both refusals
    disappear with zero code change. It is a guard whose whole purpose is to stop being reachable.
    """
    return conn.execute("SELECT to_regclass('selection_formula_binding')").fetchone()[0] is not None
