# Hand reconciliation before crosswalk activation

**Status:** procedure, ready to run. The RUN itself is a Gate-B activity against live data and needs
its own explicit approval (plan §Task 13, final row: *stop for explicit approval before a live
profile, generated Hadoop run or catalog mutation*). Nothing in this document may be executed
against a cluster without that approval.

**Owner of the checks:** `src/featuregen/overlay/upload/crosswalk_reconciliation.py`.
**Proof the checks are executable:** `tests/featuregen/overlay/upload/test_crosswalk_reconciliation.py`.

---

## Why this exists

Every other gate in Release C compares one recorded artifact against another: the observation
against the applicability scope, the resolved binding against the pinned one, the directional
verdict against the admission policy. Each of those is satisfied by a measurement that is internally
consistent and **wrong** — a probe that read the wrong partition, an `as_of` a day out, a mapping
table filtered by a rule nobody intended. None of that is visible from inside the record.

A person running the same join by hand and reporting four numbers is the only signal in the system
that originates outside it. That is the whole reason the plan asks for this before activation, and
the reason it asks for **two** subjects rather than one.

## Why both a direct bridge and a crosswalk

Reconciling only the crosswalk leaves the question *"does hand-counting this platform's joins agree
at all?"* unanswered. If both subjects diverge, the finding is about the method — the operator's
query, the environment, the snapshot they read. If only the crosswalk diverges, the finding is about
the crosswalk. Running one subject cannot tell those apart.

## What to pick

* **The direct bridge:** any current `BridgeJoinRealizationRevisionV1` whose recorded cardinality is
  a fan-in shape (`one_to_one` or `many_to_one`). A `one_to_many` realization makes no claim a count
  can contradict, so reconciling one proves nothing.
* **The crosswalk:** a REAL mapping-table crosswalk with a composed observation under the scope you
  intend to activate, and — this is the part most likely to be skipped — one whose mapping table
  genuinely has history. A mapping table with one row per key cannot exhibit the defect the whole
  release is built to prevent, so reconciling it is a formality.

## The procedure

### 0. Get approval. Then assemble the subject.

Build the crosswalk through `crosswalk_assembly.assemble_admitted_crosswalk` for the scope you are
activating. Everything below reads from that bundle: the three pinned addresses, the pinned mapping
row rule and the composed observation. **Do not read the numbers off a dashboard** — the point is to
compare against what the platform would actually execute under.

### 1. Write down what the platform claims, before you count.

From the bundle: the three physical addresses, the pinned `mapping_temporal_policy_revision_id`, and
the observation's `composed_row_count`, `mapping.row_count`, `source_to_target_max_matches`,
`target_to_source_max_matches`. Writing them down first is not ceremony — a count taken while
looking at the expected answer is not an independent count.

### 2. Count, by hand, against the real tables.

Run the two joins yourself, in the order the generated project runs them:

1. filter the mapping table by the pinned row rule **first**;
2. join source → mapping;
3. join mapping → target;
4. count the composed rows, the surviving mapping rows, and the maximum matches per source tuple and
   per target tuple.

The order is load-bearing. Counting before applying the row rule measures uniqueness across all
history, which is a different claim and is the exact defect `uniqueness_measured_before_time_filtering`
exists to kill.

### 3. Record WHAT you counted, not only the numbers.

Fill in `HandCountsV1.read_addresses` with the three tables exactly as typed into your query, and
`row_rule_applied` with the policy revision you filtered by (or `None` if you filtered by nothing).
The commonest reconciliation error is not a wrong count — it is a right count of the wrong thing,
and these two fields are what catch it.

### 4. Run the comparison.

```python
report = reconcile_crosswalk(admitted, hand_counts)
bridge_report = reconcile_direct_bridge(realization, bridge_hand_counts)
```

### 5. Read the answer.

* `report.agrees is True` for **both** subjects → the reconciliation is satisfied. Record both
  reports with the activation decision.
* `ADDRESS_DISAGREES` or `ROW_RULE_DISAGREES` → **stop and re-count.** Every other number in that
  report describes a different question, so nothing else in it means anything yet.
* Any count disagreement → **a finding.** There is no tolerance and no rounding, deliberately: a
  tolerance is a policy, and a policy belongs where somebody can review it, not compiled into the
  check that is supposed to be independent.
* `NOT_MEASURED` → not a disagreement. There is nothing recorded to compare against; profile the
  crosswalk first, then come back.

`report.blocks_activation` is the single boolean the activation decision consumes. It is true for
**any** finding — there is no partial pass, because a reconciliation that disagreed about one of its
numbers has not established the thing activation needs it to establish.

## What this does NOT do

* It does not query anything. It compares numbers a person brings back against artifacts the
  platform already holds — which is what makes it testable without a cluster, and why the live half
  is a separate approved activity that FEEDS this rather than something this performs.
* It does not grant execution. A satisfied reconciliation is one input to the activation decision;
  `FEATUREGEN_CROSSWALK_EXECUTION`, the flag-dependency boot refusal and the deterministic
  direction-specific verdicts all still apply, unchanged.
* It is not a review. Nothing in `crosswalk_reconciliation` reads or writes a review status, and a
  confirmed crosswalk that fails reconciliation fails it exactly as loudly as an unreviewed one.
