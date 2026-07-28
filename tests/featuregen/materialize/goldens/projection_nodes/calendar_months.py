def project_total_debit_amount_30d__body_expr(source: DataFrame, business_dt: str) -> DataFrame:
    """Point-in-time rows for total_debit_amount_30d (§8).

    Which rows total_debit_amount_30d / body.expr may SEE — nothing is aggregated here. A row
    survives only if it had ARRIVED by the cutoff (rule 1) and its event time falls inside the
    declared 3-month calendar_period window (rule 2).
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
    # Basis posted_at: 'posted_ts' IS the instant the row became readable, so it is compared to
    # the cutoff as it stands. It is the GOVERNED availability column and deliberately not the
    # event-time column — a row can happen long before anyone can see it, which is the entire
    # point of the gate.
    rows = rows.where(F.col('posted_ts') <= cutoff)

    # §8 rule 2 — the declared window: 3 months, calendar_period. Both boundaries are computed as
    # DATES with calendar arithmetic and turned into instants only at the end — which is also what
    # keeps a boundary from crossing a daylight-saving change wrong, as `instant - N x 24h` would.
    anchor = F.to_date(F.lit(business_date))
    # A calendar period: the window ends where the CURRENT month begins, so the incomplete month
    # the business date sits in is outside it. That is the whole difference from a trailing
    # window, and it is a different set of rows.
    window_end = F.trunc(anchor, 'month')
    # 3 months back, as CALENDAR months. A month is NOT thirty days: `add_months` steps whole
    # months and clamps to the end of one, so 2026-01-31 back a month is 2026-02-28 where a 30-day
    # count gives 2026-01-01, and 3 months back from 2026-07-06 is 2026-04-06 where 90 days gives
    # 2026-04-07. Either conversion moves the boundary and changes which rows are summed.
    window_start = F.add_months(window_end, -3)

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
