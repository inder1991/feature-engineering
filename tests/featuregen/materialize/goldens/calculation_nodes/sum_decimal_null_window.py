def calculate_total_debit_amount_30d(
        projection: DataFrame, spine: DataFrame, business_dt: str,
        generation_id: str, run_id: str, sandbox_execution_hash: str,
        staging_root: str) -> tuple[DataFrame, dict]:
    """total_debit_amount_30d for one business date (§8).

    Exactly one row per (cif_id, business_dt), carrying total_debit_amount_30d and nothing else.
    The aggregate is reduced onto the SPINE with a LEFT join (§8 rule 3), so an entity with no
    source rows is still present and carries this formula's declared empty-window value (null).
    An inner join here would shrink the population and the feature would still look plausible.

    Returns the staged frame and its StagingManifestV1 (§9) — both, or neither. A gate below
    raises, Kedro writes no output for the node at all, and assembly reports a MISSING manifest
    rather than reading a column no manifest describes.
    """
    # The business date is read only to BIND the manifest to this run. The staged frame's own
    # business_dt comes from the spine, which decides which population the row belongs to —
    # deriving it here as well would be a second answer that could disagree with it.
    business_date = str(business_dt)

    # §6's governed filter, read from the compiled filter tree — never assembled from text. It
    # runs BEFORE the aggregate: the projection decided which rows this expression may SEE, and
    # this decides which of those it counts.
    rows = projection.where((F.col('status_cd') == 'posted') & (F.col('dr_cr_flag') == 'D'))

    # §8 rule 4 — the declared null_input is `ignore`, which is what Spark's aggregates do: a NULL
    # txn_amt is skipped. Nothing is rendered for it, so this comment is the evidence that the
    # policy was READ rather than defaulted to.
    aggregate = F.sum(F.col('txn_amt'))

    # The operand count rides along for §9's overflow gate below: Spark answers a sum that exceeds
    # its own result type with NULL INSIDE the aggregation (`CheckOverflowInSum`, before any
    # cast), and under `ignore` the only NULL the policy itself produces is the all-null group —
    # which this count records as 0. A NULL sum beside a count above zero is therefore overflow,
    # never policy. Dropped once the gate has read it.
    operand_count = F.count(F.col('txn_amt'))

    # The grain-level aggregate. `sum` is the expression's own declared aggregate, and the
    # grouping is the DECLARED grain — one row per landing key, which is what the spine reduction
    # below can then join onto exactly once.
    grouped = rows.groupBy(F.col('cif_id')).agg(
        aggregate.alias('total_debit_amount_30d'),
        operand_count.alias('__operand_count'),
    )

    # §8 rule 3 — the spine reduction. LEFT, and never INNER: an entity with no source rows in
    # this window must still be present, carrying the declared empty-window value, and an inner
    # join would silently drop it — the population shrinks, every downstream count moves with it,
    # and the feature still looks plausible. It cannot inflate either: the spine holds one row per
    # (keys…, business_dt) (§4.2 rule 5 makes a duplicate there a blocking gate) and the aggregate
    # holds one row per key. Nothing de-duplicates here; fan-out is refused upstream, and a
    # row-collapsing repair in the renderer is how row inflation becomes invisible.
    staged = spine.join(grouped, on=['cif_id'], how='left')

    # §8 rule 4 — the declared empty_window is `null`, which is what the LEFT join already leaves
    # for an entity with no rows. No marker column is rendered because none is needed: a null from
    # an empty window and a null from the aggregate are the same declared answer here, so nothing
    # has to tell them apart.

    # §6 — rounding is applied EXPLICITLY, from the formula's own declared `half_even` mode, and
    # never inherited from an engine default. `F.bround` is Spark's function for exactly that mode
    # — a tie rounds to the EVEN neighbour, so 2.5 becomes 2 and 3.5 becomes 4. A different mode
    # would move every tie in this column, and an engine default states no mode at all.
    staged = staged.withColumn(
        'total_debit_amount_30d',
        F.bround(F.col('total_debit_amount_30d'), 6),
    )

    # §9 OVERFLOW_VIOLATION, in TWO checks — the formula declares `error` on overflow, `error` is
    # not a mode Spark has, and Spark's silent NULL appears in two different places. FIRST, inside
    # the aggregation: a sum exceeding its own RESULT type is already NULL before any cast
    # (`CheckOverflowInSum`), so no cast comparison can see it. Every group has at least one
    # source row by construction, and `__operand_count` above is zero for every NULL the §8 rule 4
    # policies produce themselves — so a NULL beside a count above zero is overflow inside the
    # aggregation, and nothing else.
    agg_overflowed = staged.where(
        F.col('__operand_count').isNotNull()
        & (F.col('__operand_count') > F.lit(0)) & F.col('total_debit_amount_30d').isNull())
    if agg_overflowed.limit(1).count() > 0:
        raise RuntimeError(
            "OVERFLOW_VIOLATION: total_debit_amount_30d was aggregated to NULL over a group with "
            "at least one non-null operand: the sum overflowed INSIDE the aggregation, before any "
            "publish cast could see it. The formula declares overflow=error, so the run stops "
            "rather than publishing a NULL indistinguishable from an empty window. Rows affected: "
            + str(agg_overflowed.count()))
    # SECOND, the publish cast, whose default is to return NULL for a value that does not fit
    # DECIMAL(38,6). The cast is compared against the value that went into it — a row that was NOT
    # null and became null overflowed, and a NULL silently replacing a number is exactly what the
    # declaration refuses.
    typed = F.col('total_debit_amount_30d').cast('decimal(38,6)')
    # A null that was ALREADY null passes: that is the empty-window or null-input policy's own
    # answer, not an overflow, and reporting it here would fire the wrong gate.
    overflowed = staged.where(F.col('total_debit_amount_30d').isNotNull() & typed.isNull())
    if overflowed.limit(1).count() > 0:
        raise RuntimeError(
            "OVERFLOW_VIOLATION: total_debit_amount_30d produced at least one value that does not "
            "fit its declared DECIMAL(38,6). The formula declares overflow=error, so the run "
            "stops rather than publishing the NULL Spark would otherwise substitute. Rows "
            "affected: " + str(overflowed.count()))
    staged = staged.withColumn('total_debit_amount_30d', typed)
    # The operand count is dropped after BOTH checks: it is this node's own working state, and a
    # column it invented must never reach the published table.
    staged = staged.drop('__operand_count')

    # §10.2 — per-feature staging carries the landing keys, the business date and ONE feature
    # column, and nothing else. The three system columns are added ONCE, at assembly: one per
    # staging output would collide there, and any other column this node invented would be an
    # extra column §9 reports against the whole group. The business date is the SPINE's — it
    # decides which population the row belongs to, and re-deriving it from the run parameter would
    # be a second answer that could disagree with the row.
    staged = staged.select(F.col('cif_id'), F.col('business_dt'), F.col('total_debit_amount_30d'))

    # §9's evidence. `generated_project_hash` is read from the lock AT RUN TIME: §7 keeps it out
    # of every other generated file, because the hash is taken OVER those files, so a literal here
    # would be a value the hash is computed from.
    project_root = pathlib.Path(__file__).resolve().parents[4]
    lock = json.loads((project_root / 'GENERATED.lock').read_text(encoding='utf-8'))

    # The staged output's own column NAMES, plus the type THIS compilation declared for the
    # feature column. It is not a read of the physical dtype: §9 compares types against
    # `expected_schema` on the ASSEMBLED group, where every column of the published row is
    # present, and a second per-feature answer would be a second verdict on one question.
    schema_hash = hashlib.sha256(json.dumps(
        {'columns': staged.columns,
         'feature': ['total_debit_amount_30d', 'DECIMAL(38,6)', True]},
        separators=(',', ':'), sort_keys=True).encode('utf-8')).hexdigest()

    # Where the output went. Composed from the `staging_root` run parameter and the catalog's OWN
    # relative path for this dataset, so the manifest cannot name a path nothing wrote to. The
    # root is a run parameter because §9's staging area is generation-scoped: one fixed at render
    # time would be shared by every run.
    staging_path = '/feature_staging/total_debit_amount_30d/data'
    output_location = str(staging_root).rstrip('/') + staging_path

    # No data value appears in a manifest and none may be added (§14): counts, types, hashes and
    # locations only. The generation/run/date binding is what makes a reused staging path
    # detectable — an older SUCCESSFUL manifest whose ir_hash still matches would otherwise
    # publish stale output past every other check (§9).
    manifest = {
        'intent_feature_name': 'total_debit_amount_30d',
        'ir_hash': '985525a5f1410a2031049fbe3aa64866b83e3c2ab0f23c0a4f39bbf4db2889dc',
        'generation_id': str(generation_id),
        'run_id': str(run_id),
        'business_dt': business_date,
        'generated_project_hash': lock['generated_project_hash'],
        'sandbox_execution_hash': str(sandbox_execution_hash),
        'output_location': output_location,
        'schema_hash': schema_hash,
        'row_count': staged.count(),
        'status': 'completed',
    }
    return staged, manifest
