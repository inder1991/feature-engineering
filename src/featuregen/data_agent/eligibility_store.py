"""Where a source's definition of "a transaction that counts" lives.

`TransactionEligibilityPolicyV1` had nowhere to live: every test invented one and no deployment could
supply it, so `ExecutionInputs` stayed unassemblable even once a table was bound. The analysis IR
refuses without a policy — counting pending, failed and reversed transactions as activity changes the
answer rather than widening it — so this was the last thing making every plan unrunnable by
construction.

**Not configuration.** A binding is an address with one right answer. This is a JUDGEMENT about the
bank's data: is a PENDING transaction activity? was a reversal that arrived after the cutoff known at
the cutoff? Two reasonable people can disagree, so the record keeps who decided and when.

**Usable before confirmation, and disclosed.** An unconfirmed policy still governs the query — the
alternative is refusing every question until someone signs a form, which is how a governance step
becomes theatre. What it must never do is pass silently: a plan resting on an unconfirmed definition
of "counts" carries a finding, exactly like a plan resting on an unconfirmed join identity. That is
the planning layer's rule everywhere else, and eligibility is the place it matters most.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from featuregen.data_agent.eligibility import (
    NullBehavior,
    ReversalMode,
    TransactionEligibilityPolicyV1,
)


@dataclass(frozen=True, slots=True)
class StoredEligibility:
    """The policy plus the provenance that decides whether it is disclosed."""

    policy: TransactionEligibilityPolicyV1
    proposed_by: str
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None

    @property
    def confirmed(self) -> bool:
        return bool(self.confirmed_by)


def record_eligibility(conn, *, catalog_source: str, table: str,
                       policy: TransactionEligibilityPolicyV1, proposed_by: str,
                       confirmed_by: str | None = None,
                       now: datetime | None = None) -> None:
    """Upsert one table's policy.

    Re-proposing CLEARS a previous confirmation. That is the point: a human agreed to a specific
    definition of "counts", and changing the status codes underneath that agreement while keeping the
    signature would be the worst kind of silent change — the audit trail would say a person approved
    something they never saw.
    """
    conn.execute(
        "INSERT INTO eligibility_policy (catalog_source, table_name, status_column, "
        "  included_status_values, reversal_mode, reversal_column, non_reversed_values, "
        "  null_behavior, proposed_by, confirmed_by, confirmed_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (catalog_source, table_name) DO UPDATE SET "
        "  status_column = EXCLUDED.status_column, "
        "  included_status_values = EXCLUDED.included_status_values, "
        "  reversal_mode = EXCLUDED.reversal_mode, "
        "  reversal_column = EXCLUDED.reversal_column, "
        "  non_reversed_values = EXCLUDED.non_reversed_values, "
        "  null_behavior = EXCLUDED.null_behavior, "
        "  proposed_by = EXCLUDED.proposed_by, "
        "  confirmed_by = EXCLUDED.confirmed_by, "
        "  confirmed_at = EXCLUDED.confirmed_at",
        (catalog_source, table, policy.status_column, list(policy.included_status_values),
         str(policy.reversal_mode), policy.reversal_column, list(policy.non_reversed_values),
         str(policy.null_behavior), proposed_by, confirmed_by,
         now if confirmed_by else None))


def confirm_eligibility(conn, *, catalog_source: str, table: str, actor: str,
                        now: datetime) -> bool:
    """Mark an existing policy as agreed. Returns False when there is nothing to confirm.

    Deliberately separate from `record_eligibility`: confirming is an act by a person about a
    specific definition, and letting one call both write and approve would make self-approval the
    default path.
    """
    result = conn.execute(
        "UPDATE eligibility_policy SET confirmed_by = %s, confirmed_at = %s "
        "WHERE catalog_source = %s AND lower(table_name) = lower(%s)",
        (actor, now, catalog_source, table))
    return bool(getattr(result, "rowcount", 0))


def resolve_eligibility(conn, *, catalog_source: str, table: str) -> StoredEligibility | None:
    """One table's policy, or None when nobody has defined one.

    None is an ABSENCE, not an error: most tables have no policy, and the honest downstream result is
    that the plan cannot run — which the analysis IR already says precisely.
    """
    row = conn.execute(
        "SELECT status_column, included_status_values, reversal_mode, reversal_column, "
        "       non_reversed_values, null_behavior, proposed_by, confirmed_by, confirmed_at "
        "FROM eligibility_policy "
        "WHERE catalog_source = %s AND lower(table_name) = lower(%s)",
        (catalog_source, table)).fetchone()
    if row is None:
        return None
    # Constructed through the contract, never assembled field-by-field into a bare object: the
    # dataclass refuses an unsupported reversal mode and an empty status list, and a row that no
    # longer satisfies those must fail loudly here rather than produce a query that counts the wrong
    # rows.
    policy = TransactionEligibilityPolicyV1(
        status_column=row[0], included_status_values=tuple(row[1] or ()),
        reversal_mode=ReversalMode(row[2]), reversal_column=row[3],
        non_reversed_values=tuple(row[4] or ()), null_behavior=NullBehavior(row[5]))
    return StoredEligibility(policy=policy, proposed_by=row[6], confirmed_by=row[7],
                             confirmed_at=row[8])
