def calculate_cross_border_value_ratio_90d(
        numerator_projection: DataFrame,
        denominator_projection: DataFrame,
        spine: DataFrame,
        business_dt: str,
        generation_id: str,
        run_id: str,
        sandbox_execution_hash: str,
        staging_root: str) -> tuple[DataFrame, dict]:
    """cross_border_value_ratio_90d for one business date (§8).

    Exactly one row per (cif_id, business_dt), carrying cross_border_value_ratio_90d and nothing
    else. EACH of this body's aggregates is reduced onto the SPINE with its own LEFT join (§8
    rule 3), so an entity with no source rows is still present and carries that expression's own
    declared empty-window value (body.numerator: null, body.denominator: null). An inner join
    here would shrink the population and the feature would still look plausible.

    The final operation is a RATIO, and the formula's declared zero_denominator policy is
    `null`. A denominator of ZERO and an ABSENT denominator are different facts that both look
    like 'no value' at the end: the first is answered by that policy and the second by the
    denominator's own empty_window declaration, so the test below is on the denominator's VALUE
    and never on the quotient.

    Returns the staged frame and its StagingManifestV1 (§9) — both, or neither. A gate below
    raises, Kedro writes no output for the node at all, and assembly reports a MISSING manifest
    rather than reading a column no manifest describes.
    """
    # The business date is read only to BIND the manifest to this run. The staged frame's own
    # business_dt comes from the spine, which decides which population the row belongs to —
    # deriving it here as well would be a second answer that could disagree with it.
    business_date = str(business_dt)

    # `body.numerator` — a FULL aggregate with its own governed filter, its own point-in-time
    # projection and its own window; the operands of a final operation are separate expressions
    # and are not guaranteed to share any of the three.

    # §6's governed filter, read from the compiled filter tree — never assembled from text. It
    # runs BEFORE the aggregate: the projection decided which rows this expression may SEE, and
    # this decides which of those it counts.
    numerator_rows = numerator_projection.where(F.col('cross_border_flag') == True)

    # §8 rule 4 — the declared null_input is `ignore`, which is what Spark's aggregates do: a NULL
    # txn_amt is skipped. Nothing is rendered for it, so this comment is the evidence that the
    # policy was READ rather than defaulted to.
    numerator_aggregate = F.sum(F.col('txn_amt'))

    # The operand count rides along for §9's overflow gate below: Spark answers a sum that exceeds
    # its own result type with NULL INSIDE the aggregation (`CheckOverflowInSum`, before any
    # cast), and under `ignore` the only NULL the policy itself produces is the all-null group —
    # which this count records as 0. A NULL sum beside a count above zero is therefore overflow,
    # never policy. Dropped once the gate has read it.
    numerator_operand_count = F.count(F.col('txn_amt'))

    # The grain-level aggregate. `sum` is the expression's own declared aggregate, and the
    # grouping is the DECLARED grain — one row per landing key, which is what the spine reduction
    # below can then join onto exactly once.
    numerator_grouped = numerator_rows.groupBy(F.col('cif_id')).agg(
        numerator_aggregate.alias('__numerator'),
        numerator_operand_count.alias('__numerator_operand_count'),
    )

    # `body.denominator` — a FULL aggregate with its own governed filter, its own point-in-time
    # projection and its own window; the operands of a final operation are separate expressions
    # and are not guaranteed to share any of the three.

    # The expression declares no filter, so every row the point-in-time projection admitted is
    # aggregated. Named rather than silent: a filter dropped between the compiler and here would
    # leave exactly this shape and nothing would say which case it was.
    denominator_rows = denominator_projection

    # §8 rule 4 — the declared null_input is `ignore`, which is what Spark's aggregates do: a NULL
    # txn_amt is skipped. Nothing is rendered for it, so this comment is the evidence that the
    # policy was READ rather than defaulted to.
    denominator_aggregate = F.sum(F.col('txn_amt'))

    # The operand count rides along for §9's overflow gate below: Spark answers a sum that exceeds
    # its own result type with NULL INSIDE the aggregation (`CheckOverflowInSum`, before any
    # cast), and under `ignore` the only NULL the policy itself produces is the all-null group —
    # which this count records as 0. A NULL sum beside a count above zero is therefore overflow,
    # never policy. Dropped once the gate has read it.
    denominator_operand_count = F.count(F.col('txn_amt'))

    # The grain-level aggregate. `sum` is the expression's own declared aggregate, and the
    # grouping is the DECLARED grain — one row per landing key, which is what the spine reduction
    # below can then join onto exactly once.
    denominator_grouped = denominator_rows.groupBy(F.col('cif_id')).agg(
        denominator_aggregate.alias('__denominator'),
        denominator_operand_count.alias('__denominator_operand_count'),
    )

    # §8 rule 3 — the spine reduction. LEFT, and never INNER: an entity with no source rows in
    # this window must still be present, carrying the declared empty-window value, and an inner
    # join would silently drop it — the population shrinks, every downstream count moves with it,
    # and the feature still looks plausible. It cannot inflate either: the spine holds one row per
    # (keys…, business_dt) (§4.2 rule 5 makes a duplicate there a blocking gate) and the aggregate
    # holds one row per key. Nothing de-duplicates here; fan-out is refused upstream, and a
    # row-collapsing repair in the renderer is how row inflation becomes invisible.
    staged = spine.join(numerator_grouped, on=['cif_id'], how='left')
    staged = staged.join(denominator_grouped, on=['cif_id'], how='left')

    # §8 rule 4 — `body.numerator` and `body.denominator` both declare empty_window `null`, which
    # is what the LEFT joins already leave for an entity with no rows. No marker column is
    # rendered for either because none is needed: a null from an empty window and a null from the
    # aggregate are the same declared answer here, so nothing has to tell them apart.

    # §9 OVERFLOW_VIOLATION at the AGGREGATE level, per operand and BEFORE the final operation
    # consumes the two halves — afterwards a NULL operand is indistinguishable from every policy
    # answer that also leaves one. Spark answers a sum exceeding its own result type with NULL
    # before ANY cast (`CheckOverflowInSum`), and each operand count above is zero for every NULL
    # its own §8 rule 4 policies produce — so a NULL operand beside a count above zero is that
    # overflow, and nothing else.
    numerator_agg_overflowed = staged.where(
        F.col('__numerator_operand_count').isNotNull()
        & (F.col('__numerator_operand_count') > F.lit(0)) & F.col('__numerator').isNull())
    if numerator_agg_overflowed.limit(1).count() > 0:
        raise RuntimeError(
            "OVERFLOW_VIOLATION: body.numerator of cross_border_value_ratio_90d was aggregated to "
            "NULL over a group with at least one non-null operand: the sum overflowed INSIDE the "
            "aggregation, before any publish cast could see it. The formula declares "
            "overflow=error, so the run stops rather than publishing a NULL indistinguishable "
            "from an empty window. Rows affected: " + str(numerator_agg_overflowed.count()))
    denominator_agg_overflowed = staged.where(
        F.col('__denominator_operand_count').isNotNull()
        & (F.col('__denominator_operand_count') > F.lit(0)) & F.col('__denominator').isNull())
    if denominator_agg_overflowed.limit(1).count() > 0:
        raise RuntimeError(
            "OVERFLOW_VIOLATION: body.denominator of cross_border_value_ratio_90d was aggregated "
            "to NULL over a group with at least one non-null operand: the sum overflowed INSIDE "
            "the aggregation, before any publish cast could see it. The formula declares "
            "overflow=error, so the run stops rather than publishing a NULL indistinguishable "
            "from an empty window. Rows affected: " + str(denominator_agg_overflowed.count()))
    # The operand counts are dropped the moment the gates have read them: they are this node's own
    # working state, and a column it invented must never reach the published table.
    staged = staged.drop('__numerator_operand_count', '__denominator_operand_count')

    # §8 rule 4's ÷0 half — the declared zero_denominator is `null`. It is tested on the
    # DENOMINATOR's value and never on the quotient. A denominator that is zero and one that is
    # ABSENT are different facts, and a division answers both with the same NULL — so reading the
    # quotient back would settle one declaration's question with another's answer. What an absent
    # denominator becomes was decided above by `body.denominator`'s own empty_window declaration;
    # this reads whatever value that declaration left behind.
    numerator_value = F.col('__numerator')
    denominator_value = F.col('__denominator')
    # The literal below carries no cast. The denominator is an OPERAND, and §6 resolves a type for
    # the published column only — so there is no declared operand type for a cast to state, and a
    # comparison against zero is exact in every numeric type Spark would promote to.
    denominator_is_zero = denominator_value.isNotNull() & (denominator_value == F.lit(0))
    # The DIVISOR is replaced, rather than the quotient repaired afterwards. Non-ANSI Spark
    # answers a division by zero with a NULL and ANSI Spark raises, so a `/` left to meet a zero
    # would make this declared policy depend on a session setting — right by accident today, and
    # an error the day ANSI is enabled.
    undefined = F.lit(None).cast('decimal(38,6)')
    divisor = F.when(denominator_is_zero, undefined).otherwise(denominator_value)
    quotient = numerator_value / divisor
    staged = staged.withColumn('cross_border_value_ratio_90d', quotient)

    # §9 OVERFLOW_VIOLATION at the OPERATION level. The ratio's own arithmetic carries a Spark
    # result type of its own, and a result that exceeds it is NULL under ANSI-off with BOTH
    # operands present. No other check can see that: the cast comparison below needs a non-null
    # value to compare, and each operand's own column is healthy — so both are read here, while
    # they still exist. The `null` zero_denominator policy answers a zero denominator with this
    # same NULL, so its own test excludes those rows here.
    operation_overflowed = staged.where(
        numerator_value.isNotNull() & denominator_value.isNotNull() & (~denominator_is_zero)
        & F.col('cross_border_value_ratio_90d').isNull())
    if operation_overflowed.limit(1).count() > 0:
        raise RuntimeError(
            "OVERFLOW_VIOLATION: the ratio producing cross_border_value_ratio_90d evaluated to "
            "NULL although both operands were present and the denominator was not zero: the "
            "operation's own arithmetic overflowed its Spark result type, before the publish cast "
            "could see it. The formula declares overflow=error, so the run stops rather than "
            "publishing a NULL indistinguishable from an empty window. Rows affected: "
            + str(operation_overflowed.count()))

    # The operand columns are dropped: per-feature staging carries the keys, the business date and
    # ONE feature column. The two halves are this node's own working state, and either one
    # surviving is an extra column §9 reports against the whole group at assembly.
    staged = staged.drop('__numerator', '__denominator')

    # §6 — rounding is applied EXPLICITLY, from the formula's own declared `half_up` mode, and
    # never inherited from an engine default. `F.round` is Spark's function for exactly that mode
    # — a tie rounds AWAY from zero, so 2.5 becomes 3. A different mode would move every tie in
    # this column, and an engine default states no mode at all.
    staged = staged.withColumn(
        'cross_border_value_ratio_90d',
        F.round(F.col('cross_border_value_ratio_90d'), 6),
    )

    # §9 OVERFLOW_VIOLATION. The formula declares `error` on overflow, and `error` is not a mode
    # Spark has: its default is to return NULL for a value that does not fit DECIMAL(38,6). So the
    # cast is compared against the value that went into it — a row that was NOT null and became
    # null overflowed, and a NULL silently replacing a number is exactly what the declaration
    # refuses. The other two thirds of this obligation were checked above, while the operand
    # columns still existed: each operand's own sum (already NULL before any cast —
    # `CheckOverflowInSum`), and the final operation's own arithmetic (NULL with both operands
    # present).
    typed = F.col('cross_border_value_ratio_90d').cast('decimal(38,6)')
    # A null that was ALREADY null passes: that is the empty-window or null-input policy's own
    # answer, not an overflow, and reporting it here would fire the wrong gate.
    overflowed = staged.where(F.col('cross_border_value_ratio_90d').isNotNull() & typed.isNull())
    if overflowed.limit(1).count() > 0:
        raise RuntimeError(
            "OVERFLOW_VIOLATION: cross_border_value_ratio_90d produced at least one value that "
            "does not fit its declared DECIMAL(38,6). The formula declares overflow=error, so the "
            "run stops rather than publishing the NULL Spark would otherwise substitute. Rows "
            "affected: " + str(overflowed.count()))
    staged = staged.withColumn('cross_border_value_ratio_90d', typed)

    # §10.2 — per-feature staging carries the landing keys, the business date and ONE feature
    # column, and nothing else. The three system columns are added ONCE, at assembly: one per
    # staging output would collide there, and any other column this node invented would be an
    # extra column §9 reports against the whole group. The business date is the SPINE's — it
    # decides which population the row belongs to, and re-deriving it from the run parameter would
    # be a second answer that could disagree with the row.
    staged = staged.select(
        F.col('cif_id'),
        F.col('business_dt'),
        F.col('cross_border_value_ratio_90d'),
    )

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
         'feature': ['cross_border_value_ratio_90d', 'DECIMAL(38,6)', True]},
        separators=(',', ':'), sort_keys=True).encode('utf-8')).hexdigest()

    # Where the output went. Composed from the `staging_root` run parameter and the catalog's OWN
    # relative path for this dataset, so the manifest cannot name a path nothing wrote to. The
    # root is a run parameter because §9's staging area is generation-scoped: one fixed at render
    # time would be shared by every run.
    staging_path = '/feature_staging/cross_border_value_ratio_90d/data'
    output_location = str(staging_root).rstrip('/') + staging_path

    # No data value appears in a manifest and none may be added (§14): counts, types, hashes and
    # locations only. The generation/run/date binding is what makes a reused staging path
    # detectable — an older SUCCESSFUL manifest whose ir_hash still matches would otherwise
    # publish stale output past every other check (§9).
    manifest = {
        'intent_feature_name': 'cross_border_value_ratio_90d',
        'ir_hash': 'a5b257ab3e2f42a21c4d007bb2f308d278b5a8f4fedfccb624eb89cc689cbd8a',
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
