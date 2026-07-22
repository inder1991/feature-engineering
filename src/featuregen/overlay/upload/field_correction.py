"""Delivery F — the GENERIC scalar field-correction command over ``field_evidence``-governed fields.

A human-in-the-loop way to correct ONE governed scalar field (``definition`` / ``concept`` /
``domain`` / ``business_term`` / …) on a catalog asset by APPENDING human evidence — never rewriting
the source. It reuses the shipped evidence/resolution substrate wholesale
(:func:`record_field_evidence` / :func:`_evidence_set_hash` / :func:`resolve_and_project`) and adds
four governance guarantees the peer join / table-fact / semantic-binding surfaces also carry:

* **CAS, fail-closed (409).** The command names the exact view it was issued against — the field's
  ``expected_latest_decision_id`` + ``expected_evidence_set_hash`` + ``expected_policy_version``. If
  ANY differs from the CURRENT value it 409s — INCLUDING when new evidence arrived but the latest
  DECISION has not yet changed (a concurrent evidence append moves the set hash → 409). It never
  finalizes on a stale view.
* **Four-eyes on a load-bearing confirm.** ``confirm_existing`` / ``confirm_override`` require a
  confirmer who is NOT the proposer of the evidence being confirmed (a service producer — glossary /
  LLM / parser — is trivially distinct; a HUMAN proposer's ``subject`` must differ). ``reject`` may
  be single-reviewer; ``propose_override`` is a proposal (no confirm).
* **``human_editable`` opt-in.** Only a :class:`FieldPolicy` with ``human_editable=True`` (the
  advisory display/semantic scalars) is correctable here; identity, physical/logical TYPE,
  sensitivity, and the specialized grain/time/join/entity/currency facts keep ``human_editable=False``
  and their DEDICATED commands — the generic route REFUSES them (403).
* **Append-only.** Every action APPENDS a new immutable ``field_evidence`` row (and a decision
  event) — it NEVER overwrites existing evidence, and it never trusts a client-supplied authority
  label (authority is the server-rechecked ``platform-admin`` confirmer claim + four-eyes).

Authorization is the SAME confirmer authority the peer governance surfaces use: for the
upload-context catalog the source owner resolves to the platform-admin governance queue, so the route
gate is ``require_confirmer`` (the raw ``platform-admin`` claim) and the write NEVER reads an authority
label off the request body. A non-authority / four-eyes denial writes a tamper-evident
``COMMAND_DENIED`` row (mirroring ``_deny_audited``) and RETURNS the 4xx so ``get_conn`` commits the
audit trace.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from featuregen.contracts import DbConn
from featuregen.contracts.envelopes import Command
from featuregen.contracts.identity import IdentityEnvelope
from featuregen.overlay.evidence import AssertionStrength, EvidenceLifecycle, EvidenceProducer
from featuregen.overlay.field_decision import (
    FieldDecisionEventType,
    read_field_decisions,
    record_field_decision,
)
from featuregen.overlay.field_evidence import (
    FieldEvidence,
    canonical_hash,
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
)
from featuregen.overlay.upload.column_authority import logical_ref_of
from featuregen.overlay.upload.concepts import is_known_concept
from featuregen.overlay.upload.contract.invalidation import (
    REASON_METADATA_CORRECTED,
    ChangedRef,
    invalidate_contracts_for,
)
from featuregen.overlay.upload.field_policies import policy_for
from featuregen.overlay.upload.field_resolution import (
    FIELD_POLICY_VERSION,
    RESOLVER_VERSION,
    _evidence_set_hash,
    _graph_key,
    resolve_and_project,
    stale_and_clear_field,
)
from featuregen.overlay.upload.ingest import ingest_source_lock_key
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.security.audit import record_denial

ACTIONS: frozenset[str] = frozenset(
    {"confirm_existing", "propose_override", "confirm_override", "reject"}
)

# [5] the PROJECTING actions — each re-projects (or clears) the field's governed display column, which
# H2c hashes as contract-dependency state. ``propose_override`` is the sole non-projecting action (it
# only surfaces a pending proposal), so it never invalidates a dependent contract.
_PROJECTING_ACTIONS: frozenset[str] = frozenset({"confirm_existing", "confirm_override", "reject"})

# [F11] the correctable fields whose INGESTION-STAGE counterpart the D2 semantic-binding shortlist
# consumed (``concept`` drives the currency-code + monetary groups; ``business_term``/``term_type``
# are the curated term facets in the fingerprint material). A projecting correction on one of these
# changes the premise the table's CURRENT candidate set was authored against — a change the ``sbf-v1``
# fingerprint can NEVER observe (it hashes Pass-A inputs, which an unchanged re-upload cache-replays),
# so the correction command itself must retire the current set.
_SHORTLIST_INPUT_FIELDS: frozenset[str] = frozenset({"concept", "business_term", "term_type"})

# Per-field value bound (chars); ``definition`` gets a longer prose ceiling, everything else a short
# scalar bound. A value outside the bound (or empty/whitespace-only) is REFUSED before any write.
_MAX_LEN: dict[str, int] = {"definition": 4000}
_DEFAULT_MAX_LEN = 512

# Fields whose value is drawn from a CLOSED, known vocabulary — a correction must land a real registry
# term, not free text (M-8). ``definition`` / ``domain`` / ``business_term`` stay genuinely free-text.
_KNOWN_VOCAB_VALIDATORS = {"concept": is_known_concept}

_HUMAN = EvidenceProducer.HUMAN.value


class FieldCorrectionError(Exception):
    """A benign, PRE-WRITE refusal (unregistered field / not human-editable / asset not found /
    out-of-bounds value / invalid selection / CAS conflict / idempotency-key reuse). Carries an HTTP
    ``status_code``; the route RAISES it so nothing this command wrote is committed (there is none —
    every ``FieldCorrectionError`` is raised before the first append). It is NOT a four-eyes/authz
    denial: those RETURN an audited deny so the ``COMMAND_DENIED`` row commits."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _lock_key(logical_ref: str) -> int:
    """A stable signed 64-bit advisory-lock key for one ``logical_ref`` (COLUMN-scoped, F review C-1).

    Serializes ALL concurrent corrections on the SAME column — NOT just the same field — because a
    confirm's :func:`resolve_and_project` re-resolves fields on the ref: a per-``(logical_ref, field)``
    key let a sibling-field correction interleave and silently revert a just-confirmed four-eyes
    decision. Column-scoped, the loser observes the winner's appended evidence and CAS-409s (rather
    than both appending / one reverting)."""
    digest = hashlib.sha256(f"field_correction:{logical_ref}".encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def _human_proposal(e: FieldEvidence) -> bool:
    return e.producer == _HUMAN and e.strength == AssertionStrength.PROPOSED.value


def _proposed_by_actor(e: FieldEvidence, actor: IdentityEnvelope) -> bool:
    """Whether ``e`` was PROPOSED by ``actor`` as a human — the four-eyes bar. A NON-human producer
    (glossary / LLM / parser / taxonomy / source) is trivially distinct from a human confirmer."""
    return e.producer == _HUMAN and e.producer_ref == actor.subject


def _check_bounds(field: str, value: object) -> None:
    """Reject an out-of-bounds value BEFORE any evidence write: empty/whitespace, over the field's
    length ceiling, or — for a KNOWN-vocabulary field (``concept``, M-8) — a term not in the closed
    registry. Genuinely free-text fields (``definition`` / ``domain``) skip the vocab gate."""
    text = "" if value is None else str(value)
    if not text.strip():
        raise FieldCorrectionError(400, "replacement_value must be a non-empty value")
    if len(text) > _MAX_LEN.get(field, _DEFAULT_MAX_LEN):
        raise FieldCorrectionError(
            400, f"replacement_value exceeds the {field} bound of "
                 f"{_MAX_LEN.get(field, _DEFAULT_MAX_LEN)} characters")
    validator = _KNOWN_VOCAB_VALIDATORS.get(field)
    if validator is not None and not validator(text.strip()):
        raise FieldCorrectionError(
            400, f"{text.strip()!r} is not a recognized {field} (unrecognized_vocab)")


def _callable_actions(
    conn: DbConn, logical_ref: str, field: str, actor: IdentityEnvelope
) -> list[str]:
    """The subset of the four actions THIS authorized caller may execute against the field's CURRENT
    state — the read-model projection of the same four-eyes rule the execute path enforces. The route
    already gated the ``platform-admin`` confirmer claim, so ``propose_override`` / ``reject`` are
    always available; ``confirm_existing`` needs active evidence NOT proposed by the caller, and
    ``confirm_override`` needs a pending HUMAN override proposed by a DIFFERENT subject (a caller can
    never confirm their OWN proposal)."""
    active = read_active_field_evidence(conn, logical_ref, field)
    actions = ["propose_override", "reject"]
    if any(not _proposed_by_actor(e, actor) for e in active):
        actions.append("confirm_existing")
    if any(_human_proposal(e) and e.producer_ref != actor.subject for e in active):
        actions.append("confirm_override")
    return sorted(actions)


def _current_cas(conn: DbConn, logical_ref: str, field: str) -> tuple[str | None, str, str]:
    """The field's CURRENT CAS triple: (latest decision id or ``None``, active-evidence-set hash,
    policy version). The set hash moves on ANY active-evidence change even if the decision head does
    not — that is what makes a concurrent evidence append fail the CAS."""
    decisions = read_field_decisions(conn, logical_ref, field)
    latest = decisions[-1].decision_event_id if decisions else None
    set_hash = _evidence_set_hash(read_active_field_evidence(conn, logical_ref, field))
    return latest, set_hash, FIELD_POLICY_VERSION


def read_field_cas(conn: DbConn, *, source: str, object_ref: str, field: str) -> dict:
    """The field's CURRENT CAS anchor as a client should read it before issuing a correction:
    ``{latest_decision_id, evidence_set_hash, policy_version}`` for ``(source, object_ref, field)``
    (public-flattened, the way graph.build_graph stores object_ref). The three values are exactly what
    the command re-checks — a concurrent evidence append moves ``evidence_set_hash`` even when the
    decision head is unchanged, so a command issued against a stale anchor fails closed (409)."""
    logical_ref = logical_ref_of(conn, source.strip().lower(), object_ref.lower())
    latest, set_hash, policy_version = _current_cas(conn, logical_ref, field)
    return {"latest_decision_id": latest, "evidence_set_hash": set_hash,
            "policy_version": policy_version}


def _deny(conn: DbConn, logical_ref: str, action: str, actor: IdentityEnvelope, reason: str) -> dict:
    """Write a tamper-evident ``COMMAND_DENIED`` row on THIS connection (mirrors ``_deny_audited``)
    and return the 403 deny envelope — the route RETURNS it so ``get_conn`` commits the audit trace."""
    record_denial(
        conn,
        Command(action=f"field_correction:{action}", aggregate="field_decision",
                aggregate_id=logical_ref, args={}, actor=actor,
                idempotency_key=f"{action}:{logical_ref}:{actor.subject}"),
        reason)
    return {"accepted": False, "status_code": 403, "denied_reason": reason}


def _append_human_evidence(
    conn: DbConn, *, logical_ref: str, field: str, value: object, strength: AssertionStrength,
    lifecycle: EvidenceLifecycle, actor: IdentityEnvelope, idempotency_key: str, input_hash: str,
    spans: Sequence[str], note: str | None = None,
) -> str:
    """APPEND one immutable HUMAN ``field_evidence`` row (never an overwrite). ``producer_ref`` is the
    confirmer/proposer SUBJECT (load-bearing for four-eyes); ``producer_item_ref`` carries the
    idempotency key (the replay probe); ``input_hash`` fingerprints the request. ``note`` (M-9)
    persists the reviewer's free-text rationale on the human-action row so it survives (never dropped)."""
    return record_field_evidence(
        conn, logical_ref=logical_ref, field_name=field, proposed_value=value,
        producer=EvidenceProducer.HUMAN, strength=strength, lifecycle=lifecycle,
        producer_ref=actor.subject, producer_item_ref=idempotency_key,
        source_snapshot_id=f"human-correction:{idempotency_key}", input_hash=input_hash,
        evidence_spans=list(spans), note=note)


def apply_field_correction(
    conn: DbConn, *, source: str, object_ref: str, field: str, action: str, actor: IdentityEnvelope,
    idempotency_key: str, expected_latest_decision_id: str | None,
    expected_evidence_set_hash: str, expected_policy_version: str,
    selected_evidence_ids: Sequence[str] = (), replacement_value: str | None = None,
    note: str | None = None, now: datetime | None = None,
) -> dict:
    """Apply one field-correction command; see the module docstring for the guarantees.

    Returns ``{"accepted": True, "body": {...}}`` on success, or ``{"accepted": False,
    "status_code": 403, "denied_reason": ...}`` on a four-eyes/authz denial (the route RETURNS that so
    the audit commits). Every benign pre-write refusal raises :class:`FieldCorrectionError`.
    """
    del now  # decisions self-stamp a monotonic created_at; the seam is accepted for symmetry
    if action not in ACTIONS:
        raise FieldCorrectionError(400, f"unknown action {action!r}")

    # 1. human_editable opt-in — an unregistered field 400s; a NON-editable one (identity / TYPE /
    #    sensitivity / specialized fact) 403s and keeps its dedicated command. Never a generic write.
    policy = policy_for(field)
    if policy is None:
        raise FieldCorrectionError(400, f"unknown field {field!r}")
    if not policy.human_editable:
        raise FieldCorrectionError(
            403, f"field {field!r} is not correctable via the generic command; use its dedicated "
                 "command")

    # 2. Resolve the schema-preserving logical_ref + confirm the asset exists AND is VISIBLE to this
    #    caller (I-1 read-scope). The anchor is loaded under the actor's sensitivity scope (mirroring
    #    asset_detail._load_anchor); a hidden column (e.g. pii) the caller can't read is
    #    INDISTINGUISHABLE from a missing one — the SAME 404, raised BEFORE the idempotency probe / CAS
    #    read / any write — so a platform-admin WITHOUT pii_reader gets no existence oracle and no blind
    #    write path on a column that GET 404s for the same caller.
    norm_source = source.strip().lower()
    allowed = allowed_sensitivities(actor.role_claims)
    anchor = conn.execute(
        "SELECT 1 FROM graph_node WHERE catalog_source = %s AND object_ref = lower(%s) "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s))",
        (norm_source, object_ref, allowed)).fetchone()
    if anchor is None:
        raise FieldCorrectionError(404, "asset not found")
    logical_ref = logical_ref_of(conn, norm_source, object_ref.lower())

    # 3. Serialize this correction against BOTH the ingest writer (I-2) AND sibling corrections on this
    #    column (C-1). CONSISTENT lock ordering — the SOURCE lock FIRST, then the finer column lock —
    #    matches ingest_upload (which holds ingest_source_lock_key for the whole ingest) so the two
    #    writers can never deadlock. The column-scoped _lock_key then serializes ALL corrections on this
    #    column: the loser observes the winner's appended evidence and CAS-409s instead of double-
    #    appending (or reverting a just-confirmed sibling field).
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (ingest_source_lock_key(norm_source),))
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(logical_ref),))

    # 4. The request fingerprint doubles as the evidence input_hash AND the idempotency probe.
    input_hash = field_input_hash(
        logical_ref=logical_ref, field_name=field,
        material={"action": action, "replacement_value": replacement_value,
                  "selected_evidence_ids": sorted(selected_evidence_ids),
                  "subject": actor.subject, "idempotency_key": idempotency_key})

    # 5. Idempotency — BEFORE the CAS, so a replay carrying the (now-stale) original CAS view still
    #    replays success rather than 409ing. HUMAN field_evidence is written ONLY by this command, so
    #    (producer=human, producer_item_ref=idempotency_key) is a clean key.
    prior = conn.execute(
        "SELECT input_hash FROM field_evidence WHERE logical_ref = %s AND field_name = %s "
        "AND producer = %s AND producer_item_ref = %s",
        (logical_ref, field, _HUMAN, idempotency_key)).fetchall()
    if prior:
        if all(row[0] != input_hash for row in prior):
            raise FieldCorrectionError(409, "idempotency_key reused with different parameters")
        return _success_body(conn, logical_ref, field, action, actor, projected=None, replayed=True)

    # 6. CAS recheck — fail CLOSED on ANY drift (decision head, active-evidence set, or policy).
    cur_decision, cur_hash, cur_policy = _current_cas(conn, logical_ref, field)
    if (expected_latest_decision_id != cur_decision or expected_evidence_set_hash != cur_hash
            or expected_policy_version != cur_policy):
        raise FieldCorrectionError(
            409, "the field changed since you loaded it (concurrent decision, evidence, or policy) "
                 "— refresh and retry")

    # 7. Action-specific validation (bounds / selection / four-eyes) BEFORE any write, then effect. The
    #    reviewer ``note`` (M-9) is persisted on the appended human-action evidence row by each helper.
    if action == "propose_override":
        projected = _propose_override(conn, logical_ref, field, replacement_value, actor,
                                      idempotency_key, input_hash, note)
    elif action == "confirm_override":
        projected = _confirm_override(conn, norm_source, logical_ref, field, replacement_value,
                                      actor, idempotency_key, input_hash, note)
    elif action == "confirm_existing":
        projected = _confirm_existing(conn, norm_source, logical_ref, field, selected_evidence_ids,
                                      actor, idempotency_key, input_hash, note)
    else:  # reject
        projected = _reject(conn, norm_source, logical_ref, field, selected_evidence_ids, actor,
                            idempotency_key, input_hash, note)

    if isinstance(projected, dict):  # a four-eyes/authz deny envelope — return it (audit commits)
        return projected

    # 8. [5] composition-audit — a PROJECTING correction (confirm_override / confirm_existing / reject)
    #    just re-projected (or cleared) a graph_node display column that H2c hashes as contract-
    #    dependency state. EAGERLY append INVALIDATED to every dependent confirmed contract (SAME tx), so
    #    the read-gate downgrade is DURABLE + AUDITED (an invalidation_reason on /contracts) + round-trip-
    #    proof — consistent with the ingest dropped-field wire, closing the "two deliveries, opposite
    #    durability" gap. ``propose_override`` does NOT project, so it is absent from _PROJECTING_ACTIONS.
    #    ``object_ref`` is the PUBLIC-FLATTENED graph ref the dependency rows store (the SAME key
    #    ``_graph_key`` derives for the display projection and the ingest wire uses).
    if action in _PROJECTING_ACTIONS:
        graph_ref = _graph_key(norm_source, logical_ref)[1]
        invalidate_contracts_for(conn, changed=[ChangedRef(
            catalog_source=norm_source, reason=REASON_METADATA_CORRECTED, object_ref=graph_ref)])
        # 9. [F11] composition-audit — the D2 semantic-binding shortlist consumed this field's
        #    INGESTION-STAGE counterpart, and its ``sbf-v1`` fingerprint hashes Pass-A inputs, which an
        #    unchanged re-upload cache-replays — NO later ingest can ever observe this correction. So a
        #    projecting correction on a shortlist-consumed field retires the table's CURRENT candidate
        #    set HERE: flip it to 'unverifiable' (candidate_set_id NULL), the SAME CAS the ingest I-B
        #    dropped-table wire uses. The immutable set stays in the WORM store as history; the F2b
        #    asset subsection (which serves on cur.status='current') stops rendering stale-premise
        #    candidates. Idempotent; a table with no current set is untouched.
        if field in _SHORTLIST_INPUT_FIELDS:
            _src, schema, table, _column = parse_ref(logical_ref)
            conn.execute(
                "UPDATE current_semantic_binding_candidate_set "
                "SET status = 'unverifiable', candidate_set_id = NULL, projected_at = %s "
                "WHERE catalog_source = %s AND table_graph_ref = %s "
                "AND candidate_set_id IS NOT NULL",
                (datetime.now(UTC), norm_source, f"{schema}.{table}"))
    return _success_body(conn, logical_ref, field, action, actor, projected=projected)


def _success_body(
    conn: DbConn, logical_ref: str, field: str, action: str, actor: IdentityEnvelope,
    *, projected: bool | None, replayed: bool = False,
) -> dict:
    latest, set_hash, policy_version = _current_cas(conn, logical_ref, field)
    outcome = {"confirm_existing": "confirmed", "confirm_override": "confirmed",
               "propose_override": "proposed", "reject": "rejected"}[action]
    return {"accepted": True, "body": {
        "field": field, "action": action,
        "outcome": "replayed" if replayed else outcome, "replayed": replayed,
        "projected": bool(projected),
        # The NEW CAS anchor, so the client can chain a follow-up command without a re-read.
        "latest_decision_id": latest, "evidence_set_hash": set_hash,
        "policy_version": policy_version,
        "actions": _callable_actions(conn, logical_ref, field, actor),
    }}


def _propose_override(
    conn: DbConn, logical_ref: str, field: str, replacement_value: str | None,
    actor: IdentityEnvelope, idempotency_key: str, input_hash: str, note: str | None,
) -> bool:
    """Append a NON-load-bearing HUMAN/PROPOSED override + surface it for review; do NOT project (a
    later ``confirm_override`` by a DIFFERENT subject projects). HUMAN/PROPOSED is absent from every
    display/operational rule, so this proposal is neither shown nor load-bearing until confirmed."""
    _check_bounds(field, replacement_value)
    _append_human_evidence(
        conn, logical_ref=logical_ref, field=field, value=replacement_value,
        strength=AssertionStrength.PROPOSED, lifecycle=EvidenceLifecycle.ACTIVE, actor=actor,
        idempotency_key=idempotency_key, input_hash=input_hash, spans=[], note=note)
    return False  # not projected — the pending proposal IS the review item


def _confirm_override(
    conn: DbConn, source: str, logical_ref: str, field: str, replacement_value: str | None,
    actor: IdentityEnvelope, idempotency_key: str, input_hash: str, note: str | None,
) -> bool | dict:
    """Confirm a pending human override to a load-bearing HUMAN/CONFIRMED value + re-resolve/project.
    Four-eyes: a matching pending HUMAN/PROPOSED override by a DIFFERENT subject must exist — the
    confirmer can never be its sole proposer."""
    _check_bounds(field, replacement_value)
    target_hash = canonical_hash(replacement_value)
    pending = [e for e in read_active_field_evidence(conn, logical_ref, field)
               if _human_proposal(e) and canonical_hash(e.proposed_value) == target_hash]
    if not pending:
        raise FieldCorrectionError(
            400, "no pending human override proposal for this value to confirm")
    if all(e.producer_ref == actor.subject for e in pending):
        return _deny(conn, logical_ref, "confirm_override", actor,
                     "four_eyes: the confirmer is the sole proposer of this override")
    _append_human_evidence(
        conn, logical_ref=logical_ref, field=field, value=replacement_value,
        strength=AssertionStrength.CONFIRMED, lifecycle=EvidenceLifecycle.ACTIVE, actor=actor,
        idempotency_key=idempotency_key, input_hash=input_hash,
        spans=[e.evidence_id for e in pending], note=note)
    # C-1: re-resolve ONLY the corrected field — never a sibling field's confirmed decision.
    resolve_and_project(conn, source=source, logical_refs=[logical_ref], fields=[field])
    return True


def _confirm_existing(
    conn: DbConn, source: str, logical_ref: str, field: str, selected_evidence_ids: Sequence[str],
    actor: IdentityEnvelope, idempotency_key: str, input_hash: str, note: str | None,
) -> bool | dict:
    """Confirm an EXISTING active proposal's value with a load-bearing HUMAN/CONFIRMED append + re-
    resolve/project. Four-eyes: none of the selected evidence may be a HUMAN proposal by the actor,
    NOR (M-7) SOURCE-declared evidence (whose author — the file uploader — four-eyes can't verify)."""
    selected_ids = list(dict.fromkeys(selected_evidence_ids))
    if not selected_ids:
        raise FieldCorrectionError(400, "confirm_existing requires selected_evidence_ids")
    active = {e.evidence_id: e for e in read_active_field_evidence(conn, logical_ref, field)}
    selected = [active[eid] for eid in selected_ids if eid in active]
    if len(selected) != len(selected_ids):
        raise FieldCorrectionError(400, "selected_evidence_ids include unknown or inactive evidence")
    for e in selected:
        if _proposed_by_actor(e, actor):
            return _deny(conn, logical_ref, "confirm_existing", actor,
                         "four_eyes: the confirmer is the proposer of the selected evidence")
    # M-7 (source-evidence four-eyes gap): SOURCE evidence's producer_ref is the snapshot id, not the
    # uploading principal, so four-eyes cannot prove the confirmer is NOT the file's uploader — a single
    # admin could upload a file declaring X then confirm_existing it (single-party author+approve).
    # Lightest SOUND mitigation without an ingest-side schema change: DENY confirm_existing on
    # SOURCE-declared evidence — a source value must go through a human propose_override then
    # confirm_override (two DISTINCT human parties). Service producers (llm / parser / taxonomy /
    # structural_connector) are genuine third parties distinct from the confirmer, so they stay OK.
    if any(e.producer == EvidenceProducer.SOURCE.value for e in selected):
        return _deny(conn, logical_ref, "confirm_existing", actor,
                     "four_eyes: source-declared evidence cannot be single-party confirmed; use "
                     "propose_override then confirm_override with a different reviewer")
    if len({canonical_hash(e.proposed_value) for e in selected}) != 1:
        raise FieldCorrectionError(400, "selected_evidence_ids disagree on a value")
    _check_bounds(field, selected[0].proposed_value)  # M-6: bound/vocab-validate the confirmed value
    _append_human_evidence(
        conn, logical_ref=logical_ref, field=field, value=selected[0].proposed_value,
        strength=AssertionStrength.CONFIRMED, lifecycle=EvidenceLifecycle.ACTIVE, actor=actor,
        idempotency_key=idempotency_key, input_hash=input_hash, spans=selected_ids, note=note)
    # C-1: re-resolve ONLY the corrected field — never a sibling field's confirmed decision.
    resolve_and_project(conn, source=source, logical_refs=[logical_ref], fields=[field])
    return True


def _reject(
    conn: DbConn, source: str, logical_ref: str, field: str, selected_evidence_ids: Sequence[str],
    actor: IdentityEnvelope, idempotency_key: str, input_hash: str, note: str | None,
) -> bool:
    """Reject the selected evidence DURABLY (I-3): append an inert HUMAN/REJECTED marker (the audit of
    who rejected what), flip the SELECTED active rows to the ``rejected`` lifecycle (the substrate's
    sanctioned per-row transition, mirroring :func:`stale_source_evidence`), RE-PROJECT the field from
    whatever active evidence survives — or CLEAR the display if none remains — so the display no longer
    serves the rejected value, and record the REJECTED decision LAST so it is the decision head. May be
    single-reviewer. Writes NO operational replacement value of its own."""
    now = datetime.now(UTC)
    selected_ids = list(dict.fromkeys(selected_evidence_ids))
    marker = _append_human_evidence(
        conn, logical_ref=logical_ref, field=field, value=None,
        strength=AssertionStrength.PROPOSED, lifecycle=EvidenceLifecycle.REJECTED, actor=actor,
        idempotency_key=idempotency_key, input_hash=input_hash, spans=selected_ids, note=note)
    # I-3: DURABLY retire the selected ACTIVE evidence so the resolver stops reading the rejected value
    # (mirrors the per-row lifecycle flip stale_source_evidence uses; scoped to THIS field's rows).
    if selected_ids:
        conn.execute(
            "UPDATE field_evidence SET lifecycle = 'rejected' "
            "WHERE logical_ref = %s AND field_name = %s AND evidence_id = ANY(%s) "
            "AND lifecycle = 'active'",
            (logical_ref, field, selected_ids))
    # Re-project the field from the SURVIVING active evidence (or CLEAR the display if none remains),
    # BEFORE the REJECTED head — so the flat display column no longer serves the rejected value. C-1:
    # scoped to THIS field so a reject never re-resolves (nor reverts) a sibling field's decision.
    if read_active_field_evidence(conn, logical_ref, field):
        resolve_and_project(conn, source=source, logical_refs=[logical_ref], fields=[field], now=now)
    else:
        stale_and_clear_field(conn, source=source, logical_ref=logical_ref, field_name=field, now=now)
    # Record the REJECTED decision LAST, at a strictly-later created_at, so it is the decision HEAD the
    # read model surfaces (the re-projection above wrote a RESOLVED/STALED decision, not the head).
    record_field_decision(
        conn, logical_ref=logical_ref, field_name=field,
        event_type=FieldDecisionEventType.REJECTED,
        selected_evidence_ids=[marker, *selected_ids],
        evidence_set_hash=_evidence_set_hash(read_active_field_evidence(conn, logical_ref, field)),
        display_value_hash=None, load_bearing_value_hash=None, conflict_status="rejected",
        reason_codes=["human_rejected"], field_policy_version=FIELD_POLICY_VERSION,
        resolver_version=RESOLVER_VERSION, actor_ref=actor.subject, supersedes_event_id=None,
        now=now + timedelta(microseconds=1))
    return False  # no operational replacement — the rejected value is retired + the display re-projected
