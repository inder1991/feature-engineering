"""Seed APPROVED recipe reviews on a SANDBOX, so the contract gate can be exercised end to end.

Recipe review is the gate between "the engine proposed this" and "the bank governs this": until
every role a recipe itself names has approved it at its current revision, `create_contract` and
`author_formula` refuse with `RECIPE_REVIEW_NOT_CURRENT` (`activation_policy._contract_blockers`).
That gate is doing its job, and on a sandbox with zero recorded reviews it means no governed
contract can ever be drafted — so nothing downstream of it can be tested at all.

**THESE ARE FIXTURES. THEY ARE NOT SIGN-OFF, AND THE RECORD SAYS SO.** Every seeded event is
attributed to a transparently synthetic identity (`dev-fixture:<role>`) and carries a rationale
that names itself as a fixture. That is the whole reason this script exists instead of a loop
that types somebody's name: a real reviewer's name on a review they never performed is a forged
governance record, and the audit trail this platform keeps is worth exactly as much as its worst
entry. Anyone reading `recipe_review_event` later can tell these apart at a glance, without
knowing this script exists.

**FOUR EYES IS SATISFIED HONESTLY, NOT BYPASSED.** `review_validity` refuses a recipe whose roles
were all signed by ONE identity (`single_identity_violation`). One distinct synthetic identity per
role clears that rule for the reason the rule exists — separate roles were separately attested —
rather than by defeating it. Nothing here touches the validity fold.

**IT WRITES THROUGH THE REAL WRITER.** `record_review_event`, the same function the review route
calls, with the same validation, pinned to each recipe's CURRENT `canonical_recipe_v2_hash`. So a
definition edited after seeding stales its fixtures exactly as it would stale a human's approval —
the fixtures are not privileged, and this script cannot create a state the product could not.

**REVERSAL.** `recipe_review_event` is append-only by design. A seeded approval is withdrawn the
way any approval is: record a `retired` (or `rejected`) decision for that role at that revision,
which supersedes it in the fold. Never a DELETE — see migration 1060/1061.

USAGE (operator; nothing is defaulted, and the write needs two independent statements):

    FEATUREGEN_DSN=postgresql://postgres:postgres@localhost:15432/featuregen \\
    python scripts/seed_dev_recipe_reviews.py --confirm-database featuregen --apply

Without `--apply` it reports what it WOULD write and touches nothing — run it that way first.

**THE GUARDS.** `FEATUREGEN_DSN` has no default, because a default is how a sandbox seeder ends up
pointed at somebody's real catalog. `--confirm-database` must equal the database the DSN actually
resolves to: the operator states where they BELIEVE they are writing, and a mismatch is a refusal
rather than 950 governance rows in the wrong place. `--apply` is separate again, so reading the
plan and performing it are two decisions.
"""
from __future__ import annotations

import argparse
import os
import sys

#: Attribution for every seeded event. The prefix is the point — it is not a person, it does not
#: look like a person, and it never collides with a real reviewer id.
REVIEWER_TEMPLATE = "dev-fixture:{role}"
RATIONALE = ("seeded dev fixture for sandbox end-to-end testing — NOT a subject-matter "
             "sign-off; supersede with a 'retired' decision to withdraw")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-database", required=True,
                        help="the database name you believe FEATUREGEN_DSN resolves to")
    parser.add_argument("--apply", action="store_true",
                        help="actually write; omit for a dry run that touches nothing")
    args = parser.parse_args()

    import psycopg

    from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES
    from featuregen.overlay.upload.recipe_review import record_review_event, review_events
    from featuregen.overlay.upload.recipe_review_validity import (
        by_role_at_revision,
        required_reviewer_roles,
        review_validity,
    )

    dsn = os.environ.get("FEATUREGEN_DSN")
    if not dsn:
        print("FEATUREGEN_DSN is required and has no default", file=sys.stderr)
        return 2

    with psycopg.connect(dsn) as conn:
        # The database the connection ACTUALLY reached — asked of the server, never parsed out of
        # the DSN string, because the string is the thing that might be wrong.
        actual = conn.execute("SELECT current_database()").fetchone()[0]
        if actual != args.confirm_database:
            print(f"REFUSED: --confirm-database={args.confirm_database!r} but this DSN reaches "
                  f"{actual!r}", file=sys.stderr)
            return 3

        planned: list[tuple[str, str, str]] = []      # (recipe_id, revision, role)
        for definition in V2_RECIPES:
            revision = canonical_recipe_v2_hash(definition)
            signed = by_role_at_revision(review_events(conn, definition.recipe_id), revision)
            planned.extend((definition.recipe_id, revision, role)
                           for role in required_reviewer_roles(definition) if role not in signed)

        roles = sorted({role for _r, _rev, role in planned})
        print(f"database        : {actual}")
        print(f"recipes         : {len(V2_RECIPES)}")
        print(f"events to write : {len(planned)}  across roles {', '.join(roles) or '(none)'}")
        print(f"attributed to   : {', '.join(REVIEWER_TEMPLATE.format(role=r) for r in roles)}")
        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply to seed.")
            return 0

        for recipe_id, revision, role in planned:
            record_review_event(
                conn, recipe_id=recipe_id, recipe_revision_hash=revision, decision="approved",
                reviewer=REVIEWER_TEMPLATE.format(role=role), reviewer_role=role,
                rationale=RATIONALE)
        conn.commit()

        # Re-read every recipe through the VALIDITY FOLD. A write that succeeded is not the claim
        # being made — "the platform now reads this recipe as reviewed" is, and only the fold can
        # say so (a missing role, a stale revision or a single-identity violation all land here).
        current = [d.recipe_id for d in V2_RECIPES
                   if review_validity(d, by_role_at_revision(
                       review_events(conn, d.recipe_id),
                       canonical_recipe_v2_hash(d))).current]

    print(f"\nwritten         : {len(planned)}")
    print(f"review-current  : {len(current)} of {len(V2_RECIPES)} recipes")
    if len(current) != len(V2_RECIPES):
        print("SOME RECIPES STILL DO NOT READ CURRENT — inspect before relying on this seed",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
