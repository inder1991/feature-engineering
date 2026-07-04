"""The profiler service command (SP-1 design §6.6).

Houses `_run_profiler` — the service handler registered under `run_profiler` — and its profiler-only
preflight helper `_existing_proposal_fingerprint`. Both were lifted out of `commands.py`; `commands`
re-exports them (and references `_run_profiler` from `_OVERLAY_CATALOG`) so existing
`featuregen.overlay.commands` imports keep resolving.
"""
from __future__ import annotations

from featuregen.commands.registry import get_command
from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.overlay._lifecycle import _NON_TERMINAL, _latest_proposed
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.config import current_overlay_config
from featuregen.overlay.identity import (
    CatalogObjectRef,
    display_object_ref,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.profiler import (
    ProfilerLimits,
    SchemaNotAllowedError,
    run_profiler_scan,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.runtime.observability import counters
from featuregen.security.audit import record_denial


def _existing_proposal_fingerprint(conn: DbConn, fk: str) -> tuple[str | None, str | None]:
    """Return (folded status, latest-proposed fingerprint) for `fk` read from the AUTHORITATIVE
    event stream — NOT the asynchronous `overlay_proposal` projection. Reading
    the stream means the profiler's preflight sees exactly what `propose_fact` will, so it never
    writes evidence for a proposal that `propose_fact` would then deny under projection lag."""
    stream = load_fact(conn, fk)
    if not stream:
        return None, None
    state = fold_overlay_state(stream)
    proposed = _latest_proposed(stream)
    fp = proposed.payload.get("proposal_fingerprint") if proposed else None
    return state.status, fp


def _profiler_denial(cmd: Command, adapter, ref) -> str | None:
    """TABLE-GRANULAR server-side profiler gate (SP-1.5 Task 8 + review #4/#8). Returns a denial
    reason, or None when the profiler may scan `ref`.

    When an OverlayConfig is sealed, config.profiler_rules is AUTHORITATIVE and evaluated PER TABLE
    (not per schema): a target `schema.table` for this adapter's catalog_source is permitted iff it
    matches an allow rule AND no deny rule — DENY WINS, DEFAULT DENY. So a single table-scoped allow
    no longer opens the whole schema, and a table-scoped deny is honored. A caller's `allowed_schemas`
    can only NARROW this. With no OverlayConfig sealed, the caller-supplied schemas stand (dev /
    backward-compat) — counted loudly, since production is expected to seal a config."""
    caller = frozenset(cmd.args.get("allowed_schemas", ()))
    try:
        config = current_overlay_config()
    except RuntimeError:
        counters.incr("overlay.profiler.no_config_caller_allowlist")
        return None if ref.schema in caller else (
            f"schema {ref.schema!r} is not on the caller allowlist {sorted(caller)}"
        )
    matches = [
        r for r in config.profiler_rules
        if r.catalog_source == adapter.catalog_source
        and r.schema == ref.schema
        and r.table == ref.table
    ]
    if any(not r.allow for r in matches):
        return f"{ref.schema}.{ref.table} is denied by profiler policy"
    if not any(r.allow for r in matches):
        return f"{ref.schema}.{ref.table} matches no profiler allow rule (default deny)"
    if caller and ref.schema not in caller:
        return f"schema {ref.schema!r} is not on the caller allowlist {sorted(caller)}"
    return None


def _profiler_requires_restricted_role() -> bool:
    """OverlayConfig.profiler_require_restricted_role, or False when no config is sealed."""
    try:
        return current_overlay_config().profiler_require_restricted_role
    except RuntimeError:
        return False


def _run_profiler(conn: DbConn, cmd: Command) -> CommandResult:
    """Service command (§6.6): run the deterministic profiler over `cmd.args["ref"]` and, for each
    candidate, write evidence and issue a `propose_fact`. Runs inside `execute_command`'s
    transaction, so `run_profiler_scan`'s `SET LOCAL statement_timeout` applies to this scan.

    Stream-based preflight: for each candidate the folded stream status decides
    BEFORE any evidence is written — a non-terminal fact (DRAFT/PARTIALLY_CONFIRMED/VERIFIED/
    REVERIFY/STALE) blocks any new proposal, and a REJECTED fact with the SAME
    `proposal_fingerprint` is sticky-skipped (fresh evidence alone never revives it). Skipping first
    guarantees no orphan evidence is left for a candidate `propose_fact` would deny."""
    ref = CatalogObjectRef(**dict(cmd.args["ref"]))
    adapter = current_catalog_adapter()
    # TABLE-GRANULAR server-side gate (review #4): decide allow/deny for THIS exact schema.table
    # before scanning; a single table-allow no longer opens the whole schema.
    denial = _profiler_denial(cmd, adapter, ref)
    if denial is not None:
        record_denial(conn, cmd, denial)
        return CommandResult(
            accepted=False, aggregate_id=display_object_ref(ref), denied_reason=denial
        )
    limits = ProfilerLimits(allowed_schemas=frozenset({ref.schema}))  # gate passed for this target
    # Run the SCAN phase under an in-code read-only guard (defense-in-depth for §5.2's
    # read-only DB role) so a stray write inside run_profiler_scan fails closed. The scan only
    # SELECTs, so a savepoint we immediately roll back loses nothing; the rollback also clears
    # `transaction_read_only = on` (it was SET LOCAL after the savepoint), restoring read-write for
    # the subsequent propose_fact write phase — preserving the intentional single-transaction design.
    # An off-allowlist target raises SchemaNotAllowedError; every other handler denial returns
    # a CommandResult, so catch it, record a §6.5 security-audit denial (authz_policy checks only
    # capability+kind, NOT the schema, so the handler must audit it) and return cleanly.
    conn.execute("SAVEPOINT profiler_readonly")
    conn.execute("SET LOCAL transaction_read_only = on")
    # profiler_require_restricted_role (review #9): actually ENFORCE the flag — verify the read-only
    # guard took effect on this session; fail closed if a misconfigured/privileged connection ignored
    # SET LOCAL, rather than scanning under an unrestricted session.
    if _profiler_requires_restricted_role() and (
        conn.execute("SHOW transaction_read_only").fetchone()[0] != "on"
    ):
        conn.execute("ROLLBACK TO SAVEPOINT profiler_readonly")
        reason = "profiler_require_restricted_role: read-only session guard did not take effect"
        record_denial(conn, cmd, reason)
        return CommandResult(
            accepted=False, aggregate_id=display_object_ref(ref), denied_reason=reason
        )
    try:
        proposals = run_profiler_scan(conn, adapter, ref, limits=limits)
    except SchemaNotAllowedError as exc:
        conn.execute("ROLLBACK TO SAVEPOINT profiler_readonly")  # clears read-only -> audit can write
        record_denial(conn, cmd, str(exc))
        return CommandResult(
            accepted=False, aggregate_id=display_object_ref(ref), denied_reason=str(exc)
        )
    conn.execute("ROLLBACK TO SAVEPOINT profiler_readonly")  # clears read-only -> writes allowed again
    conn.execute("RELEASE SAVEPOINT profiler_readonly")

    propose = get_command("propose_fact")
    produced: list[str] = []
    for proposal in proposals:
        fk = fact_key(proposal.ref, proposal.fact_type, proposal.use_case)
        metrics = proposal.evidence_metrics
        # Compute the fingerprint the SAME way propose_fact will (proposed_value + profile_version +
        # thresholds), so the dedup below matches the fingerprint that would be appended.
        fingerprint = proposal_fingerprint(
            proposal.proposed_value,
            profile_version=metrics["profile_version"],
            thresholds=metrics["thresholds"],
        )
        status, existing_fp = _existing_proposal_fingerprint(conn, fk)
        # Preflight matching propose_fact's replacement semantics — skip BEFORE writing
        # evidence so the profiler never creates orphan evidence for a denied proposal.
        if status in _NON_TERMINAL:
            continue  # a non-terminal fact exists; propose_fact would deny ANY new proposal
        if status == "REJECTED" and existing_fp == fingerprint:
            continue  # sticky dedup: a rejected candidate is not re-proposed on identical value

        # Issue propose_fact using ITS exact arg contract: a live CatalogObjectRef under
        # "ref"; propose_fact derives the proposal_fingerprint itself from proposed_value +
        # profile_version + thresholds (matching `fingerprint` above). The evidence metric payload is
        # handed through under "evidence" so propose_fact mints the evidence row ATOMICALLY with the
        # accepted append — a denied proposal then never leaves orphan evidence. The preflight
        # skip gates above remain a cheap fast path, but correctness no longer depends on them.
        propose_cmd = Command(
            action="propose_fact",
            aggregate="overlay_fact",
            aggregate_id=fk,
            args={
                "ref": proposal.ref,
                "fact_type": proposal.fact_type,
                "use_case": proposal.use_case,
                "proposed_value": dict(proposal.proposed_value),
                "evidence": dict(metrics),
                "profile_version": metrics["profile_version"],
                "thresholds": metrics["thresholds"],
            },
            actor=cmd.actor,
            idempotency_key=f"profiler:{fk}:{fingerprint}",
        )
        result = propose(conn, propose_cmd)
        if not result.accepted:
            continue  # defensive: a concurrent change made the fact non-terminal — do not count it
        produced.extend(result.produced_event_ids)

    return CommandResult(
        accepted=True,
        aggregate_id=display_object_ref(ref),
        produced_event_ids=tuple(produced),
    )
