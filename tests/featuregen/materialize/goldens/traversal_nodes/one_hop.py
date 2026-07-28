def project_total_debit_amount_30d__body_expr(
        source: DataFrame,
        join_1_customers: DataFrame,
        business_dt: str) -> DataFrame:
    """Point-in-time rows for total_debit_amount_30d (§8).

    Which rows total_debit_amount_30d / body.expr may SEE — nothing is aggregated here. A row
    survives only if it had ARRIVED by the cutoff (rule 1) and its event time falls inside the
    declared 30-day trailing window (rule 2).

    Each surviving row then reaches the entity it belongs to over the governed path
    banking.transactions -> banking.customers (1 hop(s), §3.1). Every hop fans IN, so the
    traversal ANNOTATES rows and never multiplies them.
    """
    business_date = str(business_dt)

    # §1.3's authorized read set, named column by column. NEVER a star: a star reads whatever the
    # table happens to hold today, including a column added AFTER the authorization — so the group
    # would read more than it was granted, and nothing at all would say so.
    rows = source.select(
        F.col('cif_num'), F.col('dr_cr_flag'), F.col('posted_ts'), F.col('status_cd'),
        F.col('txn_amt'), F.col('txn_dt'))

    # §8's cutoff: the business date at 00:00:00 in Asia/Kolkata. The rendered
    # session timezone is pinned to UTC, so the governed zone is STATED here rather
    # than inherited — a cutoff computed in the wrong zone moves silently.
    cutoff = F.to_utc_timestamp(
        F.to_timestamp(F.lit(business_date + ' 00:00:00')), 'Asia/Kolkata')

    # §8 rule 1 — the availability gate, and the filter whose violation is INVISIBLE: a row
    # kept here that could not yet have been READ at the cutoff raises nothing at all.
    # Basis posted_at: 'posted_ts' IS the instant the row became readable, so it is compared to
    # the cutoff as it stands. It is the GOVERNED availability column and deliberately not the
    # event-time column — a row can happen long before anyone can see it, which is the entire
    # point of the gate.
    rows = rows.where(F.col('posted_ts') <= cutoff)

    # §8 rule 2 — the declared window: 30 days, trailing. Both boundaries are computed as DATES
    # with calendar arithmetic and turned into instants only at the end — which is also what keeps
    # a boundary from crossing a daylight-saving change wrong, as `instant - N x 24h` would.
    anchor = F.to_date(F.lit(business_date))
    # Trailing: the window ends at the business date itself.
    window_end = anchor
    # 30 days back. A day is a whole number of days in every calendar, so counting days here IS
    # the calendar operation rather than a conversion into one.
    window_start = F.date_sub(window_end, 30)

    # The window's zone is Asia/Kolkata — the same string as the cutoff's, and stated again rather
    # than reused because they are two governed fields that are free to differ. The rendered
    # session is pinned to UTC, so a zone left to be inherited moves both boundaries by hours.
    starts_at = F.to_utc_timestamp(window_start.cast('timestamp'), 'Asia/Kolkata')
    ends_at = F.to_utc_timestamp(window_end.cast('timestamp'), 'Asia/Kolkata')

    # Both flags as DECLARED: start inclusive (>=), end exclusive (<). Two filters, because they
    # are two boundaries — and they are carried per expression precisely because they vary.
    rows = rows.where(F.col('txn_dt') >= starts_at)
    rows = rows.where(F.col('txn_dt') < ends_at)

    # §3.1 — the governed traversal to the grain: 1 hop, each one an edge the join planner
    # returned as OPERATIONAL. The gates above ran first, on the source relation's own columns, so
    # the rows joined here are already the ones this feature may see. Every hop is a LEFT join:
    # the traversal says which entity a row belongs to and must not decide which rows EXIST, so a
    # row whose dimension row is missing survives with a null key and lands on no entity rather
    # than disappearing from the feature's population. Each hop's key travels under a name of its
    # own and is dropped once the hop is done, so nothing downstream can mistake it for the
    # entity: the key column a join keeps takes the LEFT side's value, which for an unmatched row
    # names an entity that is not there.

    # Hop 1: banking.transactions.cif_num -> banking.customers.cif_id, cardinality N:1 toward
    # customers — authorized by approved_join fact ajf-txn-cust (VERIFIED).
    hop_1 = join_1_customers.select(
        F.col('cif_id').alias('__join_1__key'), F.col('cif_id').alias('__join_1__cif_id'))
    rows = rows.withColumn('__join_1__key', F.col('cif_num'))
    rows = rows.join(hop_1, ['__join_1__key'], 'left').drop('__join_1__key')

    # What the traversal hands on: the source relation's authorized columns, plus the grain key(s)
    # it reached, each under the name the feature is grained BY. The hops' own join keys stop here
    # — they were read to travel on, and two tables on one path spell one entity's key the same
    # way.
    rows = rows.select(
        F.col('__join_1__cif_id').alias('cif_id'), F.col('cif_num'), F.col('dr_cr_flag'),
        F.col('posted_ts'), F.col('status_cd'), F.col('txn_amt'), F.col('txn_dt'))
    return rows
