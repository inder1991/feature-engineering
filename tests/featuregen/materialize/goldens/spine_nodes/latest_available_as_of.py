def build_spine(source: DataFrame, business_dt: str) -> DataFrame:
    """The declared entity population for one business date — latest_available_as_of (§4).

    Exactly one row per (cif_id, business_dt). Every feature is LEFT JOINed onto this, so an
    entity missing here has no row to land on, and an entity duplicated here duplicates every
    feature value in the published table.
    """
    business_date = str(business_dt)
    # §8's cutoff: the business date at 00:00:00 in Asia/Kolkata. The rendered
    # session timezone is pinned to UTC, so the governed zone is STATED here rather
    # than inherited — a cutoff computed in the wrong zone moves silently.
    cutoff = F.to_utc_timestamp(
        F.to_timestamp(F.lit(business_date + ' 00:00:00')), 'Asia/Kolkata')

    # §4.2 rule 1 — a CONJUNCTION, and BOTH halves matter. The first keeps out a version
    # that had not taken effect by the cutoff; the second keeps out one that had taken
    # effect and had not yet ARRIVED. The same cutoff for both, and they are written as two
    # filters because they are two obligations.
    eligible = source.where(F.col('effective_from') <= cutoff)
    eligible = eligible.where(F.col('load_ts') <= cutoff)

    # §4.2 rules 2 and 3 — the greatest effective time per key wins, and the declared
    # tie-break refs order rows that share one. F.rank() deliberately, NOT a row-numbering
    # window: numbering the rows would invent an order the declaration does not provide, so
    # every tie would resolve and none would be seen. Under rank() a tie SURVIVES, below.
    latest_first = Window.partitionBy(F.col('cif_id')).orderBy(
        F.col('effective_from').desc(), F.col('version_seq').desc())
    ranked = eligible.withColumn('__spine_rank', F.rank().over(latest_first))
    winners = ranked.where(F.col('__spine_rank') == 1).drop('__spine_rank')

    # A key with more than one rank-1 row is a tie NO declared ordering separates. It
    # depends on the actual rows, so it is a gate here and not a compilation refusal.
    unresolved = winners.groupBy(F.col('cif_id')).count().where(F.col('count') > 1)
    if unresolved.limit(1).count() > 0:
        raise RuntimeError(
            "SPINE_NON_DETERMINISTIC: two eligible rows share an effective time and every "
            "declared tie-break (version_seq), so no declared ordering separates them. Picking "
            "one would change the population between runs and move every feature value with it, "
            "so the spine refuses.")
    rows = winners

    # §4.2 rule 6 — the spine's OWN availability_ref is 'load_ts', which the
    # eligibility filter above already applied at this same cutoff. Filtering on it a
    # second time would change nothing and would read as though it were a second rule.

    # The landing shape (§8 rule 3): the planned key columns, plus the business date the
    # run was given. Nothing else — the system columns are added once, at assembly.
    spine = rows.select(F.col('cif_id').alias('cif_id')).withColumn(
        'business_dt', F.to_date(F.lit(business_date)))

    # §4.2 rule 5 — duplicate spine keys are a BLOCKING GATE, never a de-duplication step.
    # Collapsing the rows here would turn a declaration that does not hold into a smaller
    # population that looks right, and every count downstream would move with it.
    duplicated = spine.groupBy(
        F.col('cif_id'), F.col('business_dt')).count().where(F.col('count') > 1)
    if duplicated.limit(1).count() > 0:
        raise RuntimeError(
            "SPINE_DUPLICATE_KEY: the declared population produced more than one row for a "
            "(cif_id, business_dt). §4.2 makes that a blocking gate rather than a de-duplication "
            "step: collapsing the rows would hide a population the declaration got wrong.")
    return spine
