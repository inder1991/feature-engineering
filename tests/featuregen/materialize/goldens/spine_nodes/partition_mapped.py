def build_spine(source: DataFrame, business_dt: str) -> DataFrame:
    """The declared entity population for one business date — partition_mapped (§4).

    Exactly one row per (cif_id, business_dt). Every feature is LEFT
    JOINed onto this, so an entity missing here has no row to land on and an entity
    duplicated here duplicates every feature value in the published table.
    """
    business_date = str(business_dt)
    # §8's cutoff: business_dt at 00:00:00 in Asia/Kolkata. The rendered
    # session timezone is pinned to UTC, so the governed zone is STATED here rather
    # than inherited — a cutoff computed in the wrong zone moves silently.
    cutoff = F.to_utc_timestamp(
        F.to_timestamp(F.lit(business_date + ' 00:00:00')), 'Asia/Kolkata')

    # §4.2 / §3.4 — the business date selects partition values through the DECLARED
    # mapping. The mapping is governed in the environment inventory; this applies it.
    rows = source
    rows = rows.where(F.col('snapshot_dt') == F.lit(business_date))

    # §4.2 rule 6 — the spine's OWN availability_ref participates in PIT filtering
    # exactly as an expression's does: a member that had not yet arrived at the cutoff
    # was not knowable, so it is not in the population.
    rows = rows.where(F.col('load_ts') <= cutoff)

    # The landing shape (§8 rule 3): the planned key columns, plus the business date the
    # run was given. Nothing else — the system columns are added once, at assembly.
    spine = rows.select(F.col('cif_id').alias('cif_id')).withColumn(
        'business_dt', F.to_date(F.lit(business_date)))

    # §4.2 rule 5 — duplicate spine keys are a BLOCKING GATE, never a de-duplication step.
    # Collapsing the rows here would turn a declaration that does not hold into a smaller
    # population that looks right, and every count downstream would move with it.
    duplicated = spine.groupBy(F.col('cif_id'), F.col('business_dt')).count().where(F.col('count') > 1)
    if duplicated.limit(1).count() > 0:
        raise RuntimeError(
            'SPINE_DUPLICATE_KEY: ' + "the declared population produced more than one "
            "row for a (cif_id, business_dt). §4.2 makes that a blocking gate: "
            "collapsing the rows would hide a population the declaration got wrong.")
    return spine
