def calculate_cross_border_value_ratio_90d(
        minuend_projection: DataFrame,
        subtrahend_projection: DataFrame,
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
    declared empty-window value (body.minuend: null, body.subtrahend: null). An inner join here
    would shrink the population and the feature would still look plausible.

    The final operation is a DIFFERENCE. A NULL on either side makes the difference NULL, and it
    is deliberately not coalesced: an operand absent from its window has already been answered
    by its own empty_window declaration, and substituting a zero here would replace that answer
    with this node's — 0 - x is a real, plausible number.

    Returns the staged frame and its StagingManifestV1 (§9) — both, or neither. A gate below
    raises, Kedro writes no output for the node at all, and assembly reports a MISSING manifest
    rather than reading a column no manifest describes.
    """
    # The business date is read only to BIND the manifest to this run. The staged frame's own
    # business_dt comes from the spine, which decides which population the row belongs to —
    # deriving it here as well would be a second answer that could disagree with it.
    business_date = str(business_dt)

    # `body.minuend` — a FULL aggregate with its own governed filter, its own point-in-time
    # projection and its own window; the operands of a final operation are separate expressions
    # and are not guaranteed to share any of the three. Any literal it needs carries the PUBLISHED
    # type, because §6 resolves a type for the published column and none for an operand, so that
    # is the only declared type there is to state.
    # §6's governed filter, read from the compiled filter tree — never assembled from text. It
    # runs BEFORE the aggregate: the projection decided which rows this expression may SEE, and
    # this decides which of those it counts.
    minuend_rows = minuend_projection.where(F.col('cross_border_flag') == True)

    # §8 rule 4 — the declared null_input is `ignore`, which is what Spark's aggregates do: a NULL
    # txn_amt is skipped. Nothing is rendered for it, so this comment is the evidence that the
    # policy was READ rather than defaulted to.
    minuend_aggregate = F.sum(F.col('txn_amt'))

    # The grain-level aggregate. `sum` is the expression's own declared aggregate, and the
    # grouping is the DECLARED grain — one row per landing key, which is what the spine reduction
    # below can then join onto exactly once.
    minuend_grouped = minuend_rows.groupBy(F.col('cif_id')).agg(
        minuend_aggregate.alias('__minuend'),
    )

    # `body.subtrahend` — a FULL aggregate with its own governed filter, its own point-in-time
    # projection and its own window; the operands of a final operation are separate expressions
    # and are not guaranteed to share any of the three. Any literal it needs carries the PUBLISHED
    # type, because §6 resolves a type for the published column and none for an operand, so that
    # is the only declared type there is to state.
    # The expression declares no filter, so every row the point-in-time projection admitted is
    # aggregated. Named rather than silent: a filter dropped between the compiler and here would
    # leave exactly this shape and nothing would say which case it was.
    subtrahend_rows = subtrahend_projection

    # §8 rule 4 — the declared null_input is `ignore`, which is what Spark's aggregates do: a NULL
    # txn_amt is skipped. Nothing is rendered for it, so this comment is the evidence that the
    # policy was READ rather than defaulted to.
    subtrahend_aggregate = F.sum(F.col('txn_amt'))

    # The grain-level aggregate. `sum` is the expression's own declared aggregate, and the
    # grouping is the DECLARED grain — one row per landing key, which is what the spine reduction
    # below can then join onto exactly once.
    subtrahend_grouped = subtrahend_rows.groupBy(F.col('cif_id')).agg(
        subtrahend_aggregate.alias('__subtrahend'),
    )

    # §8 rule 3 — the spine reduction. LEFT, and never INNER: an entity with no source rows in
    # this window must still be present, carrying the declared empty-window value, and an inner
    # join would silently drop it — the population shrinks, every downstream count moves with it,
    # and the feature still looks plausible. It cannot inflate either: the spine holds one row per
    # (keys…, business_dt) (§4.2 rule 5 makes a duplicate there a blocking gate) and the aggregate
    # holds one row per key. Nothing de-duplicates here; fan-out is refused upstream, and a
    # row-collapsing repair in the renderer is how row inflation becomes invisible.
    staged = spine.join(minuend_grouped, on=['cif_id'], how='left')
    staged = staged.join(subtrahend_grouped, on=['cif_id'], how='left')

    # §8 rule 4 — `body.minuend`'s declared empty_window is `null`, which is what the LEFT join
    # already leaves for an entity with no rows. No marker column is rendered because none is
    # needed: a null from an empty window and a null from the aggregate are the same declared
    # answer here, so nothing has to tell them apart.

    # §8 rule 4 — `body.subtrahend`'s declared empty_window is `null`, which is what the LEFT join
    # already leaves for an entity with no rows. No marker column is rendered because none is
    # needed: a null from an empty window and a null from the aggregate are the same declared
    # answer here, so nothing has to tell them apart.

    # The DIFFERENCE. A NULL on either side makes it NULL, and it is deliberately NOT coalesced:
    # an operand absent from its window was already answered by its own empty_window declaration
    # above, and substituting a zero here would replace that answer with this node's — `0 - x` is
    # a real, plausible number and nothing downstream would question it.
    minuend_value = F.col('__minuend')
    subtrahend_value = F.col('__subtrahend')
    staged = staged.withColumn('cross_border_value_ratio_90d', minuend_value - subtrahend_value)

    # The operand columns are dropped: per-feature staging carries the keys, the business date and
    # ONE feature column, and the two halves of an operation the caller never asked for would be
    # extra columns §9 reports against the whole group at assembly.
    staged = staged.drop('__minuend', '__subtrahend')

    # §6 — rounding is applied EXPLICITLY, from the formula's own declared `half_even` mode, and
    # never inherited from an engine default. `F.bround` is Spark's function for exactly that mode
    # — a tie rounds to the EVEN neighbour, so 2.5 becomes 2 and 3.5 becomes 4. A different mode
    # would move every tie in this column, and an engine default states no mode at all.
    staged = staged.withColumn(
        'cross_border_value_ratio_90d',
        F.bround(F.col('cross_border_value_ratio_90d'), 6),
    )

    # §9 OVERFLOW_VIOLATION. The formula declares `error` on overflow, and `error` is not a mode
    # Spark has: its default is to return NULL for a value that does not fit DECIMAL(38,6). So the
    # cast is compared against the value that went into it — a row that was NOT null and became
    # null overflowed, and a NULL silently replacing a number is exactly what the declaration
    # refuses.
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
        'ir_hash': '927aaa403ef961367ea6a5c069f7c8410d0cf10f8d4518ba4872a705ae786787',
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
