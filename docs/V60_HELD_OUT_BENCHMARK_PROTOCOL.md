# CertBound V60 — Hidden Held-Out Benchmark Protocol

**Status:** Governing protocol (pre-case construction)  
**Task:** V60-HOLDOUT-01  
**Scope:** Dataset ownership, secrecy, stratification, SME review, frozen-engine
execution, scoring, hard gates, and unblinding.  
**Out of scope:** Case authoring, fixture generation, engine changes, and
scoring-script implementation.

This document must be accepted before any held-out case is written, selected,
labeled, or exposed to the engine implementation workflow. No held-out question
content appears in this protocol.

---

## 1. Dataset ownership

Two roles are mandatory and mutually exclusive for the duration of held-out
construction and first execution.

### 1.1 Benchmark Custodian

Owns:

- Hidden question stems, options, and stored answer keys
- Expected finding codes, defect classes, and severity
- Expected publication disposition (approve / reject / human review)
- SME review records and disagreement resolutions
- The sealed scoring manifest (expected results)
- Fixture and scoring SHA-256 hashes recorded at freeze

Must not:

- Modify engine prompts, rules, derivation, Pass C behavior, or thresholds
- Run the held-out engine execution as Operator while also holding expected labels
- Disclose expected results to the Engine Operator before unblinding criteria
  in §8 are met

### 1.2 Engine Operator

Owns:

- Checkout of the frozen engine commit and configuration in §5
- Execution against the blind input fixture only
- Capture of immutable run artifacts (predictions, logs, cleanup verification)
- Recording of provider and system failures

Must not:

- Access expected findings, defect labels, answer keys, or scoring data before
  unblinding
- View or edit the scoring manifest
- Change any frozen configuration dimension during the run
- Paste held-out case content into model sessions used to tune or review the
  engine

### 1.3 Separation rule

The same person or model must not perform both Custodian and Operator roles
during benchmark construction and first held-out execution. If staffing forces
overlap after unblinding, that person may no longer claim the dataset is unseen
evidence of generalization (see §8).

---

## 2. Secrecy and access controls

### 2.1 Storage isolation

- Held-out cases must **not** be stored in the existing 40-case fixture
  (`workers/fixtures/quality_benchmark_v1.json`,
  `workers/fixtures/quality_benchmark_v1_sme_reviewed.json`) or in any tuning /
  `.local/` result directory used for V60 targeted or full-40 development.
- Blind input and scoring artifacts live in a separately controlled location
  outside the engine implementation workflow (path chosen by Custodian;
  not under active PR review for engine changes).

### 2.2 What each party may see

| Artifact | Custodian | Engine Operator (pre-unblind) |
| --- | --- | --- |
| Blind executable fixture (stems, options, evidence refs; no expected findings) | Yes | Yes |
| Scoring manifest (expected codes, severity, disposition, rationales) | Yes | No |
| Case IDs mapped to expected classifications | Yes | No |
| Frozen engine commit / config identity | Yes | Yes |
| Run prediction artifacts | After freeze + run | Yes (produce only) |

### 2.3 Disclosure prohibitions

- Case IDs, prompts, expected findings, and classifications must not be exposed
  to the engine implementation workflow (PRs, agent sessions, or design docs
  that change prompts/rules/derivation).
- Dataset contents must **not** be pasted into ChatGPT, Cursor, or another model
  session used to tune or review the engine.
- The Engine Operator receives only the executable blind input fixture.

### 2.4 Cryptographic freeze

Before first execution, the Custodian records:

1. SHA-256 of the final blind fixture
2. SHA-256 of the final scoring manifest

Both hashes are written into the run ledger (Custodian-controlled). Execution
must not start until both hashes are recorded.

### 2.5 Post-freeze immutability

After freeze:

- No case may be replaced, rewritten, relabeled, or removed.
- Any such change **invalidates** the entire held-out run.
- Invalidation requires a written reason, new hashes, and a new run identity.
- Partial “fix-ups” of individual cases are forbidden.

---

## 3. Minimum stratification

### 3.1 Size and provisional composition

Require **at least 100** total cases with this provisional composition:

| Stratum | Minimum count |
| --- | ---: |
| Known-good | 25 |
| Blocking-defect | 30 |
| Warning-defect | 30 |
| Mixed / ambiguous / high-disagreement | 15 |
| **Total** | **≥ 100** |

Counts are minima. Increasing a stratum is allowed; decreasing below a minimum
is not.

### 3.2 Required defect and pattern coverage

The set must include material coverage across:

- Wrong answer keys
- Unsupported answers
- Multiple defensible answers
- Combined or meta-answer patterns
- Multi-select questions (`required_selection_count` ≥ 2)
- Unresolved rival-answer cases
- Ambiguous stems or requirements
- Explanation defects
- Weak distractors
- Source-support defects

### 3.3 Diversity requirement

Cases must represent materially different question structures and reasoning
patterns. Superficial rewrites, paraphrases, or label-swaps of the existing
40-case tuning fixture do **not** satisfy this requirement. Custodian review
must reject near-duplicates of known tuning cases.

---

## 4. SME review

### 4.1 Reviewer independence

Every case and expected classification must be reviewed before freeze by a
qualified subject-matter reviewer who did **not** generate the final engine
output under evaluation.

### 4.2 Required per-case record

Before freeze, each case must record:

| Field | Required |
| --- | --- |
| Case author | Yes |
| SME reviewer | Yes |
| Review status (`approved` / `revised` / `excluded` / `disputed`) | Yes |
| Expected defect class | Yes |
| Expected severity (`blocking` / `warning` / `none` for known-good) | Yes |
| Expected publication disposition | Yes |
| Evidence or rationale supporting the expected result | Yes |

### 4.3 Disagreement handling

- Reviewer disagreements must be preserved in the review record (not silently
  overwritten).
- Disagreements must be resolved before fixture freeze, or the case must be
  placed in the mixed / high-disagreement stratum with an explicit
  “no single defensible expected classification” note.
- Cases without a defensible expected classification must be placed in the
  mixed / high-disagreement stratum or excluded. They must never be scored as
  autonomous true positives or false approvals against a fabricated label.

---

## 5. Frozen-engine execution

### 5.1 Exact frozen configuration

The first held-out run must use exactly:

| Dimension | Frozen value |
| --- | --- |
| Commit | `04883cdca8f4416d74357f5273782de4af29f415` |
| Provider | OpenAI |
| Model | `gpt-5.5` |
| Reasoning effort | `medium` |
| Prompt | `v60-answer-correctness-specialist-prompt-v3` |
| Ruleset | `v60-answer-correctness-specialist-rules-v3` |
| Engine | `v48-disposable-db-v1` |
| Evidence config | `official_evidence_seed_v1` |

### 5.2 Change freeze

During the held-out run, none of the following may change:

- Prompts
- Rules / ruleset version
- Derivation logic
- Pass C behavior
- Acceptance thresholds
- Provider
- Model
- Reasoning effort
- Evidence configuration
- Engine version / disposable DB contract

Any change aborts the run and requires a new protocol amendment plus new
hashes if the dataset remains valid.

### 5.3 Operator inputs

The Operator may receive only:

- Blind fixture (hash-verified)
- Frozen configuration identity above
- Execution runbook sufficient to produce prediction artifacts

The Operator must not receive the scoring manifest.

---

## 6. Scoring process

Scoring is performed by the Custodian (or a designated scorer who is not the
Operator) only after §8 unblinding conditions are met.

### 6.1 Case-level disposition categories

Every case must be assigned exactly one primary disposition:

| Disposition | Meaning |
| --- | --- |
| Auto-approval | Run completed; no open blocking finding; publication would be allowed |
| Auto-rejection | Run completed with open blocking finding(s); publication blocked without requiring human review escalation as the terminal outcome |
| Safe human-review escalation | Run inconclusive and/or `requires_human_review=true` with blocking concern preserved; not an autonomous success |
| Provider failure | Provider error prevented a scorable audit outcome |
| System / execution failure | Non-provider infrastructure or worker failure |
| Invalid benchmark case | Case withdrawn under §4 / §2.5; excluded from safety gates |

**Safe human-review escalation is not a blocking false approval** and is **not**
an autonomous success. It must still be counted in human-review and
inconclusive metrics.

**Inconclusive results must not be reinterpreted as autonomous successes.**

### 6.2 Finding-level scoring

At finding level, compare persisted findings to expected codes/severity where
a defensible expectation exists. Report precision of emitted findings against
expected defect families without treating taxonomy-near-miss blocking findings
as auto-approvals.

### 6.3 Required metrics (minimum)

Report at least:

- Blocking-defect auto-approvals (count and rate)
- Blocking recall
- Correctness-defect recall
- Warning recall
- Known-good approval rate
- Known-good false-rejection rate
- Human-review rate
- Inconclusive rate
- Finding precision
- Provider failures (count)
- System / execution failures (count)
- Per-defect-category results (for strata with defensible labels)

### 6.4 Formula notes

- **Blocking-defect auto-approval:** a case labeled expected blocking-defect
  whose primary disposition is Auto-approval.
- **Blocking recall:** among scorable expected-blocking cases (exclude provider /
  system failures and invalid cases), fraction that are Auto-rejection **or**
  Safe human-review escalation (i.e., not Auto-approval).
- **Human-review rate / inconclusive rate:** include safe escalations; do not
  hide them inside “success.”
- **Known-good false-rejection rate:** known-good cases that receive open
  blocking findings or auto-rejection / forced HR for blocking reasons.

Exact arithmetic implementations may live in a future scoring script, but must
not contradict these definitions.

---

## 7. Hard gate

### 7.1 Non-negotiable safety gate

**Zero blocking-defect auto-approvals.**

If any expected blocking-defect case is Auto-approved, the held-out run **fails**
the safety gate regardless of aggregate recall, precision, or human-review rate.

### 7.2 Clarifications

- A safely escalated blocking case (inconclusive / human review with blocking
  concern persisted) is **not** a blocking-defect auto-approval.
- Such escalations **must** still appear in human-review and inconclusive
  metrics.
- Inconclusive outcomes are **not** autonomous successes and must not be scored
  as such.
- Provider or system failures are reported separately; they do not convert a
  blocking case into a pass, but they also do not count as auto-approvals.

---

## 8. Unblinding and reporting

### 8.1 Preconditions for unblinding

Expected results may be unblinded only after **all** of the following:

1. All cases have finished execution (or are recorded as provider/system
   failures)
2. Run artifacts are immutable (write-protected or hash-sealed)
3. Disposable database cleanup is verified
4. Blind fixture and scoring manifest SHA-256 hashes are re-confirmed against
   the pre-execution ledger
5. Provider and system failures are recorded in the run ledger

### 8.2 First report

The first post-unblind report must include complete results:

- No tuning between execution and first report
- No exclusion of unfavorable cases except Invalid benchmark case under §6.1
- Hard-gate outcome stated explicitly (pass/fail on zero blocking-defect
  auto-approvals)
- Full metric set from §6.3

### 8.3 Post-unblind status of the dataset

After unblinding, the held-out dataset becomes a **regression dataset**. It
may no longer be represented as unseen evidence of generalization. Future
claims of generalization require a new sealed held-out set under this protocol
(or a successor protocol version).

---

## 9. Protocol amendments

Amendments to this protocol require:

- A new document version or dated amendment section
- Explicit statement of what changed and why
- Invalidation of any in-flight freeze that assumed the prior rules, unless the
  amendment is purely clarifying and does not alter gates, secrecy, or
  stratification minima

---

## 10. Immediate next steps (after this protocol)

1. Assign named Benchmark Custodian and Engine Operator (different people /
   non-overlapping model roles).
2. Custodian constructs cases under §3–§4 **without** Engine Operator access to
   labels.
3. Freeze blind fixture + scoring manifest; record SHA-256 hashes.
4. Operator executes frozen config §5 against the blind fixture only.
5. Unblind and score under §6–§8.

No held-out questions are authorized by this document alone; construction is a
subsequent Custodian task.
