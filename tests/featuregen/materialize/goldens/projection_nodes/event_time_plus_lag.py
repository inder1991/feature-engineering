def project_total_debit_amount_30d__body_expr(source: DataFrame, business_dt: str) -> DataFrame:
    """Point-in-time rows for total_debit_amount_30d (§8).

    Which rows total_debit_amount_30d / body.expr may SEE — nothing is aggregated here. A row
    survives only if it had ARRIVED by the cutoff (rule 1) and its event time falls inside the
    declared 30-day trailing window (rule 2).
    """
    business_date = str(business_dt)

    # §1.3's authorized read set, named column by column. NEVER a star: a star reads whatever the
    # table happens to hold today, including a column added AFTER the authorization — so the group
    # would read more than it was granted, and nothing at all would say so.
    rows = source.select(
        F.col('cif_id'), F.col('dr_cr_flag'), F.col('posted_ts'), F.col('status_cd'),
        F.col('txn_amt'), F.col('txn_dt'))

    # §8's cutoff: the business date at 00:00:00 in Asia/Kolkata. The rendered
    # session timezone is pinned to UTC, so the governed zone is STATED here rather
    # than inherited — a cutoff computed in the wrong zone moves silently.
    cutoff = F.to_utc_timestamp(
        F.to_timestamp(F.lit(business_date + ' 00:00:00')), 'Asia/Kolkata')

    # §8 rule 1 — the availability gate, and the filter whose violation is INVISIBLE: a row
    # kept here that could not yet have been READ at the cutoff raises nothing at all.
    # Basis event_time_plus_lag: 'posted_ts' holds the EVENT time, and the declared lag of 6 hours
    # is how long after it the row could first be read. The lag is added rather than dropped —
    # dropping it admits every row a whole 6 hours early.
    available_at = F.col('posted_ts') + F.expr('INTERVAL 6 HOURS')
    rows = rows.where(available_at <= cutoff)

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
    return rows
