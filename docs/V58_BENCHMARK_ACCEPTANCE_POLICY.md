# CertBound Quality Benchmark — Acceptance Policy (V58-QUALITY-04D/04E/04E-R1/04E-R2)

This document is the precommitted acceptance policy for evaluating the
legacy and V48 audit engines against the CertBound quality benchmark. It
was decided (V58-QUALITY-04D) and encoded as deterministic classification
logic (V58-QUALITY-04E, `workers/quality_benchmark_policy.py`) **before**
any real engine results were generated or viewed, so no threshold in this
document was chosen to make a particular engine's actual results look
better or worse.

The implementation lives in `workers/quality_benchmark_policy.py`
(`classify_scorecard`). This document and that module must always agree —
if they diverge, the module's `POLICY_VERSION` must be bumped and this
document updated in the same change (see "Policy versioning" below).

### V58-QUALITY-04E-R2 — configuration identity bound to the prediction artifact (no `POLICY_VERSION` bump)

V58-QUALITY-04E-R1 closed the "missing identity" gap but left a provenance
loophole: `fixture_metadata.engine_configuration` was populated at
**scoring** time from a separate `--engine-configuration-json` CLI flag, so
an operator could supply, invent, or relabel the tested configuration
independently of whatever the prediction artifact actually recorded (or
without the artifact recording anything at all). This task closes that
loophole by making the prediction artifact the sole, authoritative source
of configuration identity:

1. **Prediction artifact is authoritative.** Every prediction artifact
   produced by `workers.quality_benchmark_execution.generate_predictions`
   now carries a `configuration_identity` dict — `engine_id`,
   `engine_version`, `provider_id`, `model_id`, `prompt_version`,
   `ruleset_version`, `evidence_config_id`, `source_fixture_sha256` — built
   directly from the engine adapter's own `describe_config()` at
   generation time. `generate_predictions` refuses (raises
   `PredictionArtifactError`) to write an artifact with any missing/blank
   dimension.
2. **Scoring copies it unchanged.** `score_predictions` re-validates the
   artifact's `configuration_identity` is complete and internally
   consistent (cross-checked against the artifact's own top-level
   `engine_id`/`engine_version`/`source_fixture_sha256` fields and its raw
   `provider_config`) and copies it verbatim into the scorecard. It has no
   parameter through which a caller can supply or override identity.
3. **Classification reads the scorecard only.** `classify_scorecard` now
   reads all configuration identity exclusively from
   `scorecard["configuration_identity"]`. `fixture_metadata` no longer
   accepts (and this module no longer reads) an `engine_configuration`
   field at all — there is nothing left for a caller to override at
   classification time.
4. **`--engine-configuration-json` is removed**, not redesigned — since the
   only CLI-reachable prediction-generation path today
   (`generate --engine legacy`, non-live) is deterministic-only, the
   adapter itself already emits complete, correct identity
   (`provider_id="deterministic-only"`, `model_id="not-applicable"`,
   `prompt_version="not-applicable"`) with no operator input needed. A
   future live-provider CLI path would supply real
   `provider_id`/`model_id`/`prompt_version` values as constructor
   arguments to the engine adapter *at generation time*, embedded into the
   resulting artifact — never as a separate scoring-time flag.
5. **Tampering is detected, not silently accepted.** `score_predictions`
   raises if `configuration_identity` disagrees with the artifact's own
   top-level fields or `provider_config` (e.g. someone hand-edited only one
   copy of `engine_id` after generation). `classify_scorecard` raises
   `INVALID RUN` if the scorecard's `configuration_identity` is missing,
   incomplete, or its `source_fixture_sha256` disagrees with the
   scorecard's own `prediction_source_fixture_sha256` field.
6. **Deterministic path still requires explicit markers.** Unchanged from
   R1: `provider_id="deterministic-only"`, `model_id="not-applicable"`, and
   `prompt_version="not-applicable"` are never inferred by
   `workers.quality_benchmark_policy` — they are recorded by
   `LegacyEngineAdapter.describe_config()` itself, as a true statement
   about what actually ran.

None of this changed any gate threshold, so `POLICY_VERSION` is unchanged —
this is an enforcement/provenance correction to the already-approved v1
policy, and (as with R1) no real scorecard has ever been classified under
the closed loophole, so there is nothing to retroactively reclassify.

### V58-QUALITY-04E-R1 fail-closed corrections (no `POLICY_VERSION` bump)

This task closed four enforcement gaps discovered in the V58-QUALITY-04E
implementation, before any real scorecard was ever classified (no
benchmark result has been generated yet — see the "Real-fixture
contradiction" note below). Because nothing has ever been classified under
the flawed enforcement, there is nothing to retroactively reclassify and no
approved threshold changed, so this is recorded as a same-version
correction rather than a new `POLICY_VERSION` (see "Policy versioning"):

1. **Pilot eligibility is now exact, not a band.** Pilot policy v1 requires
   `total_case_count == 40` exactly. The previous `[20, 100)` band allowed a
   differently-sized fixture to be classified under a policy version that
   was never evaluated against it. A future pilot of a different size
   requires a new `POLICY_VERSION`.
2. **Launch safety-category coverage can no longer be weakened by a
   caller.** The required baseline (`correctness`, `ambiguity`,
   `source_support`) is a fixed module constant
   (`REQUIRED_LAUNCH_SAFETY_CATEGORIES`). The fixture-metadata key was
   renamed from `safety_relevant_categories` (a full override) to
   `additional_safety_relevant_categories` (strictly additive) so it is no
   longer possible to omit, empty out, or shrink the baseline via caller
   input.
3. **Configuration identity is now complete, not partial.** Previously
   `model_name`/`ruleset_version`/`prompt_version`/`evidence_config_id`
   were optional fields that were never actually gated — a scorecard could
   reach PASS with all four null. Classification (both tiers) now requires
   an explicit, non-blank `provider_id`/`model_id`/`prompt_version`/
   `ruleset_version`/`evidence_config_id` (via
   `fixture_metadata.engine_configuration`) in addition to `engine_id`/
   `engine_version` and a non-blank `prediction_source_fixture_sha256` —
   nothing is defaulted to a passing value.
4. **Real-fixture contradiction — resolved as a reporting-precision issue,
   not a code defect.** The V58-QUALITY-04E completion report described a
   smoke test as "pilot on real 40-case fixture → clean PASS". That smoke
   test actually classified an in-memory, fully-SME-approved *copy* of the
   real fixture's content (`_build_all_approve_reviewed_fixture()` in
   `tests/test_quality_benchmark_policy.py`), not the actual on-disk
   `workers/fixtures/quality_benchmark_v1.json`, which has never been
   SME-reviewed. Reproduced directly for this task:
   `load_finalized_sme_ground_truth_fixture()` already raised
   `GroundTruthNotFinalizedError` for the real on-disk fixture (the CLI's
   `score` command refuses before ever reaching classification), and
   `classify_scorecard()` independently returns `INVALID RUN` for it even
   when fed an honestly-derived `fixture_metadata` and a hand-constructed,
   numerically flawless scorecard (see
   `RealFixtureContradictionTestCase` in
   `tests/test_quality_benchmark_policy.py`). No enforcement code change was
   needed for this specific claim — the ground-truth gates were already
   correct; only the report's wording was imprecise.

## Two separate policies, never conflated

The current 40-case benchmark (`workers/fixtures/quality_benchmark_v1.json`,
once SME-reviewed and finalized) is a **pilot benchmark**, not a
statistically sufficient launch benchmark. This document defines two
separate policies:

1. **Pilot policy** — for the ~40-case reviewed set. Supports `continue`,
   `fix major defects and rerun`, or `reject the current engine
   configuration`. **Never** supports a claim that the engine is
   production-accurate or launch-ready.
2. **Launch policy** — for a future reviewed benchmark of at least 100
   cases, with explicit double-review requirements. Only a launch PASS may
   be described with the approved launch-PASS language (see below).

Both engines (legacy and V48) are always measured under the identical
policy and identical fixture — thresholds are never adjusted per engine,
per cost, per model choice, or per implementation difficulty.

## Metric definitions (exact operational meaning)

These match the code in `workers/quality_benchmark.py` and
`workers/quality_benchmark_execution.py` exactly — this document does not
redefine them, only names and gates them.

- **False approval** (`BenchmarkCaseResult.false_approval`, defective cases
  only): the engine failed to raise all of a defective case's
  `expected_finding_codes` (at the expected materiality, when specified),
  or — for a defective case with no specific expected codes — raised zero
  findings at all. A known-good case can never be a "false approval".
- **False rejection** (`BenchmarkCaseResult.false_rejection`, known-good
  cases only): the engine raised any blocking-materiality finding against
  a case the reviewer certified as good. **This definition is preserved
  exactly as-is and is never extended to defective cases** — an engine
  raising a wrong or extra blocking finding on an already-defective case is
  a false-positive/over-flagging diagnostic issue (see
  `finding_category_metrics[...]​.false_positives`), not a "false
  rejection".
- **Blocking-defect recall**: among defective cases whose reviewer-resolved
  `expected_materiality == "blocking"`, the fraction with
  `detection_success == True`. Tracked per-case as
  `blocking_false_approval_case_ids` (the complement).
- **Known-good approval rate**: `(known_good_cases − false_rejections) /
  known_good_cases`.
- **Defective-case rejection rate**: `(defective_cases − false_approvals) /
  defective_cases` — numerically identical to `overall_recall` restricted
  to defective cases; reported as a duplicate framing of the same signal,
  not an independent one.
- **Precision and recall by finding category**
  (`finding_category_metrics`, keyed by exact canonical `finding_code`):
  `recall = true_positives / n` where `n = expected_total` (cases that
  expected that code); `precision = true_positives / (true_positives +
  false_positives)`; `false_negatives = n − true_positives`.
- **Unscored/error case count**: any prediction with a non-null `error`
  field is excluded from all scored metrics — the denominator shrinks.
  This policy closes the resulting gap explicitly: `unscored_case_ids`,
  `unscored_blocking_case_ids`, and the unscored count are always gated on
  directly (see below), rather than being allowed to silently disappear
  from a rate's denominator.
- **Warning-level recall** (`warning_recall`, `warning_recall_detected` /
  `warning_recall_total`): recall restricted to defective cases whose
  `expected_materiality == "warning"`.
- **Per-category diagnostics** (`sample_counts.category_diagnostics` in a
  classification result): for each `defect_category`, `n` (ground-truth
  case count), `recall`, `false_negatives`, and a `diagnostic_only` flag
  that is `True` whenever `n` is below `LAUNCH_MIN_CATEGORY_SAMPLE_FLOOR`
  (15) for that tier. A category below the floor can never independently
  gate a classification, at either tier.

## Pilot policy

**Eligibility**: `total_case_count` must equal exactly `40`
(`PILOT_REQUIRED_CASE_COUNT`). Any other count — including a smaller
20-case or a larger 41-/99-case run — is `INVALID RUN`. Pilot policy v1 was
designed and evaluated only against the 40-case fixture; a differently
sized pilot must not be classified under it. A future pilot of a different
size requires a new `POLICY_VERSION`.

**Ground-truth and configuration gates (shared with launch, checked
first)** — any failure here is `INVALID RUN` regardless of every other
metric:
- fixture is not AI-drafted, and is finalized SME ground truth
  (`sme_review_status == "complete"`, no `rejected_case_ids`, no
  `unresolved_second_review_case_ids`)
- **complete engine-configuration identity**: `engine_id`, `engine_version`,
  `provider_id`, `model_id`, `prompt_version`, `ruleset_version`,
  `evidence_config_id`, and `source_fixture_sha256` — all read exclusively
  from `scorecard["configuration_identity"]` (copied unchanged by
  `score_predictions` from the prediction artifact generated for this run;
  see the V58-QUALITY-04E-R2 section above) — are explicit, non-blank
  strings, and the configuration is not mixed across the run. A genuinely
  not-applicable dimension (e.g. a deterministic-only run with no LLM call)
  must already be an explicit sentinel recorded at prediction-generation
  time (e.g. `"deterministic-only"`, `"not-applicable"`) — never inferred
  or defaulted by this module, and never supplied or overridden at
  classification time.
- scorecard/fixture provenance hashes match
  (`prediction_source_fixture_matches_ground_truth_source == True`, the
  prediction source-fixture hash itself is present and non-blank, and it
  agrees with `configuration_identity.source_fixture_sha256`)
- all expected cases are represented (scorecard `case_count` equals the
  finalized benchmark's total case count)

**Hard gates → FAIL** (any one triggers FAIL):
- at least one blocking-defect case is falsely approved
  (`blocking_false_approval_case_ids` non-empty)
- at least one blocking case is unscored (`unscored_blocking_case_ids`
  non-empty)
- more than 2 cases are unscored (`unscored_case_count > 2`)
- at least 2 known-good cases receive a false rejection
  (`len(false_rejection_case_ids) >= 2`)

**CONDITIONAL PASS** (only if no FAIL/INVALID condition applies, and any of):
- 1 or 2 non-blocking cases are unscored
- exactly 1 known-good case receives a false rejection
- warning-level defective-case recall (`warning_recall`) is below `0.50`

These three checks are fully deterministic — no human/subjective override
is used in the automatic classification.

**Diagnostic only at pilot tier** (reported, never gate PASS/FAIL):
- overall finding precision
- per-`defect_category` and per-`finding_code` recall/precision (every
  pilot-scale category is below the 15-case eligibility floor, so
  `diagnostic_only=True` for all of them by construction)

**PASS** — otherwise.

### Pilot PASS language (mandatory)

Every pilot classification's `classification_language` explicitly states:
"This reflects the ~40-case pilot benchmark only. It is NOT a
launch-readiness claim and NOT a production-accuracy claim." This applies
to PASS, CONDITIONAL PASS, FAIL, and INVALID RUN alike.

### Limitations of the 40-case sample (always attached)

- Category buckets of ~4–10 cases cannot support a confidence interval; a
  100% observed rate on n=4 is not a reliable estimate of anything.
- Ground truth reflects a single primary SME reviewer per case (second
  review is opt-in/escalation only) — the label set carries unquantified
  label-noise risk.
- The fixture is hand-curated to deliberately contain defects; it is not a
  random sample of real exam content, so observed rates are not
  generalizable prevalence estimates.
- A pilot PASS/CONDITIONAL PASS is a development-iteration signal only.

## Launch policy

**Minimum requirements** (checked as part of the ground-truth/composition
gates; any failure is `INVALID RUN`):
- at least 100 finalized reviewed cases (`total_case_count >= 100`)
- at least 25 known-good cases
- at least 30 blocking-defect cases
- at least 15 cases for any individual category used as a gated category
  (categories below 15 remain diagnostic-only; this alone is not an
  `INVALID RUN` condition, but see the safety-category coverage gate below)

**Review-process attestation** (required, never inferred from case count
alone; `INVALID RUN` if absent or insufficient):
- 100% of blocking-materiality-labeled cases independently double-reviewed
- at least 20% of all remaining cases independently double-reviewed
- material reviewer disagreements adjudicated or excluded (never resolved
  unilaterally)

**Zero-error requirement**: any unscored/execution-error case at all
(`unscored_case_count > 0`) is `INVALID RUN` — a production go/no-go
decision cannot be drawn from a run with unknown cases.

**Hard gates → FAIL** (any one triggers FAIL, only evaluated once all of
the above pass):
- at least one blocking-defect case is falsely approved
- known-good false-rejection rate is greater than 5%
- overall defective-case recall is below 80%

**CONDITIONAL PASS** (only if no FAIL/INVALID condition applies, and any of):
- known-good false-rejection rate is greater than 2% and at most 5%
- overall defective-case recall is at least 80% but below 90%
- any category with `n >= 15` has recall below 85%
- a required safety-relevant category (fixed baseline: `correctness`,
  `ambiguity`, `source_support` — see
  `REQUIRED_LAUNCH_SAFETY_CATEGORIES`; a caller may only *add* further
  categories via `fixture_metadata.additional_safety_relevant_categories`,
  never remove or replace the baseline) lacks the required `n >= 15`
  coverage
- overall finding precision is below 70% (diagnostic — **never** an
  automatic FAIL on its own; always reported with its exact numerator,
  denominator, and sample count)

**PASS** — otherwise.

### Launch PASS language (mandatory, exact)

A launch PASS's `classification_language` is **exactly**:

> "Passed CertBound's launch benchmark for the exact engine, model, prompt,
> ruleset, evidence configuration, and version tested."

This sentence is never paraphrased, extended, or combined with any of the
following prohibited phrases, in any classification-tier language this
module emits (enforced defensively by
`workers.quality_benchmark_policy._assert_no_prohibited_language`):

- "production-accurate"
- "universally accurate"
- "proven safe"
- "guaranteed"
- "statistically conclusive"

A launch PASS is scoped exclusively to the exact engine, model, prompt,
ruleset, evidence configuration, and version tested — any change to any of
those invalidates the PASS and requires a fresh full launch run.

## Rationale

Zero-tolerance is concentrated on exactly one failure mode — a missed
blocking defect reaching a candidate — at both tiers, because that is the
one outcome with real user harm. Every other dimension (explanations,
distractors, precision, warning-level defects) gets a graduated band
instead of an instant kill, so the engine doesn't have to be flawless
everywhere to be useful; it only ever has to avoid waving through a
proven-wrong answer key.

Graduated CONDITIONAL PASS bands give both pilot iterations and launch
go/no-go decisions a truthful, actionable middle state ("this is good
enough to act on, here is exactly what to fix") instead of a binary that
either overstates readiness or blocks all progress.

Launch minimums (100 total / 30 blocking / 15 per gated category) are
derived from confidence-interval reasoning, not a round number — a 100%
observed rate on n=30 supports a materially tighter bound than n=16 does,
which is exactly why the launch bar is allowed to be tighter (zero
tolerance, 5%/2% bands) than the pilot bar.

Treating unscored cases as sinking the run (`INVALID RUN` at launch,
FAIL/CONDITIONAL-gated at pilot) instead of silently shrinking a rate's
denominator closes the loophole where an engine with retry/timeout
problems could "score well" only on the cases it happened to finish.

One shared gate set and one shared fixture for both engines is what makes
a legacy-vs-V48 comparison meaningful instead of apples-to-oranges.

## Prohibited claims (all tiers)

No classification result may ever state or imply:
- that a pilot result is a launch-readiness or production-accuracy claim
- that a launch PASS is universally accurate, proven safe, guaranteed, or
  statistically conclusive beyond the exact tested configuration
- a category-level percentage as reliable when `diagnostic_only == True`
- a lowered threshold justified by cost, model choice, or implementation
  difficulty

## Policy versioning rule

`workers.quality_benchmark_policy.POLICY_VERSION` identifies the exact
threshold set that produced a classification result. Changing any
threshold, gate, or classification rule in this document or in
`workers/quality_benchmark_policy.py` requires, together, in the same
change:

1. A new `POLICY_VERSION` string (never silently reuse an existing
   version string for different thresholds).
2. A written rationale for the change appended to this document.
3. **No retroactive reclassification** of any scorecard that was already
   classified under a prior policy version — a stored classification
   result is a historical record of what that policy version said at the
   time it ran, not a live view that updates when the policy changes. If a
   previously-classified run needs to be evaluated under new thresholds, it
   must be re-classified explicitly (producing a new, separately-labeled
   result under the new `POLICY_VERSION`), never overwritten in place.
