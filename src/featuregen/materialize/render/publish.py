"""Spec §10.3 — the publication target's catalog entry, rendered from EVIDENCE.

**What this replaces.** Task 12 rendered the published entry as a Hive table with
``write_mode: errorifexists``. That was the only honest choice at the time: §10 bans
``INSERT OVERWRITE``, §10.3 forbids selecting any mechanism before a probe proves one, and
``SparkHiveDataset``'s remaining modes (``append`` / ``upsert`` / ``overwrite``) each publish
something no probe had attested. The cost was recorded as a blocker — a project rendered that way
is structurally unable to run twice against the same target, because the second run lands on the
first run's output.

**How the blocker lifts without loosening anything.** Under
:attr:`~featuregen.materialize.publish.PublishMechanism.VERSIONED_POINTER` the write target is
*generation-scoped*: ``${runtime_params:staging_root}`` already resolves to
``<staging base>/<generation_id>`` (§11.1), so every generation writes to a location that did not
exist before it. ``errorifexists`` therefore stops being the thing that blocks a re-run and becomes
the thing that protects a generation's own output from being written over — which is precisely what
§9 wants, since the staged output is the evidence its gates were run against. The write mode did
not have to relax; the *target* had to stop being shared.

That is the mechanism's own shape, not a workaround: §10.3's preferred first-slice mechanism is
"immutable versioned physical outputs with one reader-visible pointer/view switch". This module
renders the immutable versioned output. **The pointer switch is not rendered here and is not a
catalog entry** — it is a single metastore operation performed against the live cluster after the
run's gates pass, and it belongs with the live probe that has to demonstrate it is atomic.

**Only a mechanism this module can actually render is rendered.** ``EXCHANGE_PARTITION`` and
``SET_LOCATION`` are members of the vocabulary because a probe may legitimately be pointed at them
(§10.3 names known problems with both), but a probe attests what the *cluster* does, not what this
renderer knows how to emit. Asked for one of them, this refuses with
``PUBLISH_MECHANISM_UNSUPPORTED`` rather than inventing a catalog entry whose atomicity nothing has
demonstrated.

**A selection, never a mechanism.** :func:`render_publish` takes a
:class:`~featuregen.materialize.publish.PublisherSelection`. A ``PublishMechanism`` is a name any
caller can spell; a selection is the thing ``select_publisher`` only hands back when a passing
attestation exists for the exact environment, at the exact engine versions, covering schema
evolution if the publication adds a column. So rendered publication code cannot exist without that
evidence, and the evidence's id is written into the entry an operator reads.

**The target is derived, never accepted.** ``physical_target_for(plan.logical_group_name)`` decides
the table (§10.1); no caller passes a location string, because a caller who could would be choosing
where a governed group lands.
"""
from __future__ import annotations

from featuregen.materialize.binding import physical_target_for
from featuregen.materialize.boundary_v2 import FeatureGroupPlanV2
from featuregen.materialize.codes import MaterializationRefused, PublicationRefusalCode
from featuregen.materialize.group_plan import FeatureGroupPlanV1
from featuregen.materialize.publish import PublisherSelection, PublishMechanism
from featuregen.materialize.render._yaml import yaml_scalar
from featuregen.materialize.render.renderable import RenderablePlan

__all__ = [
    "RENDERABLE_MECHANISMS",
    "published_dataset_name",
    "published_output_location",
    "publish_entry_body",
    "render_publish",
]

#: The mechanisms this renderer can emit a catalog entry for. A probe attests what the CLUSTER can
#: do; this is what the RENDERER knows how to write down, and the two are different claims. Kept as
#: one definition so :func:`render_publish`'s refusal and any caller's pre-check agree.
RENDERABLE_MECHANISMS: frozenset[PublishMechanism] = frozenset({PublishMechanism.VERSIONED_POINTER})

#: Where the immutable versioned output for a generation lands, relative to that generation's root.
_PUBLISHED_PREFIX = "published"


def published_dataset_name(plan: RenderablePlan) -> str:
    """The catalog name of the publication target — the ONE definition of it.

    ``render.project.project_datasets`` reads it from here rather than spelling
    ``f"feature_{...}"`` a second time: the node wiring, the catalog entry and this module must all
    name the same dataset, and two spellings is a project whose publisher writes somewhere its
    catalog does not declare.
    """
    if not isinstance(plan, FeatureGroupPlanV1 | FeatureGroupPlanV2):
        raise TypeError(
            f"published_dataset_name needs a FeatureGroupPlanV1, got {type(plan).__name__}: the "
            f"dataset name is derived from the logical group name and there is nowhere else to "
            f"take one from")
    return f"feature_{plan.logical_group_name}"


def published_output_location(published_object: str, staging_root: str) -> str:
    """WHERE a generation's immutable versioned output is — the ONE definition of that path.

    Two readers, and that is the whole reason it is a function. The catalog entry below renders it
    with Kedro's ``${runtime_params:staging_root}`` placeholder, so the RUN writes there; the
    deployment's :class:`~featuregen.materialize.publish_sql.SqlPublicationSwap` renders it with the
    run's resolved staging root, so the pointer switch points THERE. Two spellings of one derived
    path are two paths, and the second one finds an empty directory rather than an error.

    The LAST dot separates namespace from table, exactly as :func:`publish_entry_body` splits it:
    the sandbox namespace may itself be catalog-qualified, and a group name (a Hive identifier)
    never carries a dot.
    """
    _, table = published_object.rsplit(".", 1)
    return f"{staging_root}/{_PUBLISHED_PREFIX}/{table}"


def _check(plan: RenderablePlan, selection: PublisherSelection) -> None:
    if not isinstance(plan, FeatureGroupPlanV1 | FeatureGroupPlanV2):
        raise TypeError(
            f"render_publish needs a FeatureGroupPlanV1, got {type(plan).__name__}: the "
            f"publication target is DERIVED from the plan's logical group name (§10.1), never "
            f"accepted as a caller's location string")
    if not isinstance(selection, PublisherSelection):
        raise TypeError(
            f"render_publish needs a PublisherSelection, got {type(selection).__name__}: §10.3 "
            f"says the renderer consumes a selection and never a bare mechanism, so that rendered "
            f"publication code cannot exist without the evidence that selection succeeded")
    if selection.mechanism not in RENDERABLE_MECHANISMS:
        raise MaterializationRefused(
            PublicationRefusalCode.PUBLISH_MECHANISM_UNSUPPORTED,
            f"the probe attested {selection.mechanism.value} for environment "
            f"{selection.environment_id!r} (attestation "
            f"{selection.capability_attestation_id!r}), and this renderer emits a publication "
            f"entry only for "
            f"{sorted(mechanism.value for mechanism in RENDERABLE_MECHANISMS)}: an attestation "
            f"says what the CLUSTER does, and rendering a mechanism whose emitted form nothing has "
            f"demonstrated would publish on an assumed capability by another route")


def publish_entry_body(
    plan: RenderablePlan,
    *,
    selection: PublisherSelection,
) -> tuple[str, ...]:
    """The catalog entry's BODY lines, for ``render.project`` to place among the other entries.

    Split from :func:`render_publish` only so the project renderer can build its ``_CatalogEntry``
    without re-deriving anything: both go through here, so the entry a caller renders on its own is
    the entry the project contains.
    """
    _check(plan, selection)
    target = physical_target_for(plan.logical_group_name)
    versions = selection.engine_versions
    return (
        f"  # THE PUBLICATION TARGET (§10.3), derived from the sandbox binding: {target}",
        f"  # mechanism {selection.mechanism.value}, selected on capability attestation "
        f"{selection.capability_attestation_id} — probed against environment "
        f"{selection.environment_id} running hive {versions.hive} / spark {versions.spark} / "
        f"metastore {versions.metastore}.",
        f"  # This publication {'ADDS' if selection.adds_feature else 'does not add'} a column the "
        f"published table does not already have, which is why the attestation "
        f"{'had to cover' if selection.adds_feature else 'was not required to cover'} schema "
        f"evolution.",
        "  # The location is GENERATION-SCOPED (staging_root resolves to <base>/<generation_id>),",
        "  # so the output is immutable and a second run never lands on the first run's evidence.",
        "  # `errorifexists` is KEPT. §10 bans in-place bulk replacement of a published table, and",
        "  # with a per-generation target the mode no longer blocks a re-run — it now protects",
        "  # THIS generation's own output from being written over.",
        f"  # The one reader-visible pointer switch onto {target} is a metastore operation run "
        f"after §9's gates pass; it is not a catalog entry and is deliberately not rendered here.",
        "  type: spark.SparkDataset",
        f"  filepath: "
        f"{yaml_scalar(published_output_location(target, '${runtime_params:staging_root}'))}",
        '  file_format: "parquet"',
        "  save_args:",
        '    mode: "errorifexists"',
        "  metadata:",
        "    kedro-viz:",
        '      layer: "feature"',
    )


def render_publish(plan: RenderablePlan, *, selection: PublisherSelection) -> str:
    """The complete catalog entry for the publication target, as YAML text.

    Note the signature: there is a ``selection`` and there is no ``mechanism``. That is §10.3's
    rule expressed as an API rather than as a comment — a caller holding only a mechanism name has
    nothing to pass, and a caller holding a selection is holding the attestation id this entry
    records.

    Args:
        plan: the packing list. The publication target is derived from its logical group name.
        selection: what ``select_publisher`` returned — the evidence a mechanism was proven for
            this environment at these engine versions.

    Returns:
        The entry as it appears in ``conf/base/catalog.yml``: the dataset name, then its body.

    Raises:
        TypeError: ``plan`` or ``selection`` is the wrong type.
        MaterializationRefused: ``PUBLISH_MECHANISM_UNSUPPORTED`` — the selected mechanism is one
            this renderer has no attested form for.
    """
    body = publish_entry_body(plan, selection=selection)
    return "\n".join((f"{published_dataset_name(plan)}:", *body)) + "\n"
