# CertBound Quality Benchmark — SME Review Guide (V58-QUALITY-03D)

This guide is for a qualified Salesforce SME reviewing the 40-case AI-drafted
pilot benchmark (`workers/fixtures/quality_benchmark_v1.json`, exported to a
CSV for review). It is not a training document about Salesforce content —
it explains how to use the review packet efficiently and consistently.

**Every case in this packet was drafted by an AI model.** Nothing in it is
approved ground truth. Your review is the step that makes these cases
trustworthy.

## Goal: ~2–4 minutes per normal case

Everything you need to judge a case is in that case's row: the question,
all four options, the AI's stored (keyed) answer, the AI's separately
stated "evidence-supported answer," the AI's own rationale, and the exact
evidence excerpt(s) it drew on — including the source title and canonical
URL. You should not need to leave the spreadsheet or look anything up in
the repository to judge a normal case. Cases that genuinely require outside
lookup or a second opinion are exactly what `needs_second_review` is for —
flag and move on rather than spending extra time in this pass.

## What's in each row

| Column | What it is |
|---|---|
| `case_id` | Stable case identifier. Never edit. |
| `certification`, `domain` | What this case is tagged as testing. |
| `question_text` | The question stem as drafted. |
| `option_a_text` .. `option_d_text` | The four answer options as drafted. |
| `stored_correct_answer` | The option label(s) currently flagged `is_correct` in the drafted question — i.e., what a candidate would actually be told is correct today. For intentionally defective cases, this is sometimes wrong on purpose (it's a planted defect, not a typo). |
| `expected_evidence_supported_answer` | The AI drafter's own claim about what the evidence *actually* supports — which may differ from `stored_correct_answer` when the case is designed to test defect detection. |
| `known_good` | `true`/`false` — whether the AI drafter believes this case has no defect. |
| `expected_finding_codes`, `expected_materiality` | The defect code(s) and severity the AI drafter believes apply (blank for known-good cases). |
| `reviewer_rationale` | The AI drafter's own explanation for why it labeled the case the way it did. |
| `official_source_title`, `canonical_url` | The real Salesforce resource(s) this case is grounded in. |
| `evidence_excerpt` | The exact, verified excerpt(s) from that resource, frozen at authoring time. This is the only evidence you should treat as given — do not assume the case is grounded in anything not shown here. |
| `ai_drafted_label` | A compact one-line summary of the AI's own self-assessment, for quick scanning. |
| `source_fixture_sha256` | An internal integrity fingerprint of the source benchmark file this packet was exported from. Not meant to be read as content — just don't touch it (see below). |

Everything above is AI-derived context. **You do not need to trust any of
it** — treat `stored_correct_answer`, `expected_evidence_supported_answer`,
`expected_finding_codes`, and `reviewer_rationale` as claims to verify
against `evidence_excerpt`, not as facts.

**Do not edit any column above this line, for any case.** The importer
recomputes every one of these values from the current source benchmark file
and compares them exactly against what's in your CSV. If anything doesn't
match — a retyped word in `question_text`, an autocorrected option, a
reformatted excerpt, a row's content pasted under the wrong `case_id`, or
even `source_fixture_sha256` itself — the entire import is rejected with an
error naming the field and case. This is intentional: it stops a corrupted
or hand-edited spreadsheet (or a benchmark file that changed after you
started reviewing) from silently contaminating the ground truth. If you
need to correct something in the question itself, use `sme_decision` +
`sme_notes` to flag it — don't edit the question text directly in the CSV.

## Columns you fill in

These start **blank**. The importer enforces the "Required?" column below as
hard validation rules — a row that doesn't meet them will be rejected, so
it's worth knowing them up front rather than discovering them later:

| Column | Allowed values | Required? |
|---|---|---|
| `sme_decision` | `approve`, `correct_label`, `reject_case`, `needs_second_review` | Yes, for every case you consider reviewed |
| `sme_correct_answer` | One or more option labels (`A`–`D`), separated by `\|` if more than one | Required for `correct_label` (together with, or instead of, `sme_finding_codes`); **must be blank for `approve`** |
| `sme_finding_codes` | One or more canonical codes (see *sme_finding_codes values* below), separated by `\|`; or the reserved token `CLEAR` to remove all findings | Required for `correct_label` (together with, or instead of, `sme_correct_answer`); **must be blank for `approve`** |
| `sme_notes` | Short free text | **Required** whenever `sme_decision` is `correct_label`, `reject_case`, or `needs_second_review`, and whenever `needs_second_review` is `true`. Optional only for a plain `approve` with `needs_second_review` blank/false. |
| `confidence` | `high`, `medium`, `low` | **Required for every case with a non-blank `sme_decision`** — a decision without a confidence level is rejected |
| `needs_second_review` | `true` or `false` (blank = false) | Set `true` when you want another qualified reviewer's opinion, independent of your `sme_decision`. Any case marked this way (or decided `needs_second_review`) is held out of the finalized reviewed fixture until it's resolved (re-reviewed with a different decision, or the flag cleared). |

In short: `approve` needs at minimum a decision and a confidence, and must
leave `sme_correct_answer`/`sme_finding_codes` blank (if the case needs a
correction, it isn't an `approve`). `correct_label`, `reject_case`, and
`needs_second_review` also need `sme_notes`, and `correct_label`
additionally needs a real correction (`sme_correct_answer` and/or
`sme_finding_codes`) that **actually changes** at least one of the answer
label(s) or finding code(s) from what the AI drafted — re-typing the same
answer/codes the AI already had is treated as a no-op and rejected, not as
a confirmation (use `approve` for that).

### sme_finding_codes values

The `sme_finding_codes` cell has exactly three meanings depending on what you enter:

| Value | Meaning |
|---|---|
| *(blank)* | **Inherit** — keep the AI-drafted finding codes unchanged. |
| One or more canonical codes from `workers/finding_policy.py`, separated by `\|` | **Replace** — use these codes instead of the AI-drafted ones. |
| `CLEAR` | **Explicit clear** — replace the AI-drafted findings with an empty list, making the case effectively `known_good`. |

`CLEAR` is a reserved control token, **not** a finding code. Rules:

- `CLEAR` is valid **only** for `sme_decision=correct_label`.
- `CLEAR` must appear **alone** — `CLEAR|WRONG_ANSWER_KEY` and `WEAK_DISTRACTORS|CLEAR` are both invalid.
- `CLEAR` on a case where the AI-drafted findings are already empty, with no other field change, is a no-op and will be rejected.
- `correct_label` rows using `CLEAR` still require `sme_notes` and `confidence`.
- Lowercase `clear` or mixed-case `Clear` are **not** accepted — the token must be exactly `CLEAR`.
- A blank `sme_finding_codes` field always means **inherit**, never clear to nothing.

**When to use `CLEAR`:** when you are correcting `sme_correct_answer` to a different option *and* you believe the AI-drafted defect finding no longer applies to that corrected answer — so the corrected case has no defect at all. For example, if the AI labelled a case as `MULTIPLE_DEFENSIBLE_ANSWERS` with evidence-supported answer `A|B`, but you conclude only `A` is correct and the defect does not exist, set `sme_correct_answer=A` and `sme_finding_codes=CLEAR`. The finalized fixture will record `effective_answer=["A"]`, `effective_finding_codes=[]`, `known_good=true`.

## The review rubric (apply in this order)

For each case, in order:

1. **Confirm or reject the proposed correct answer.** Read the question and
   all four options, then read `evidence_excerpt`. Does the evidence
   actually support `stored_correct_answer`? If yes and nothing else is
   wrong, this is heading toward `approve`. If the evidence supports a
   different option, this is heading toward `correct_label`.
2. **Identify ambiguity or multiple defensible answers.** Could a
   well-prepared candidate reasonably pick more than one option based on
   the evidence shown? If so, note which labels, even if the AI already
   flagged this — confirm or correct its guess.
3. **Judge distractor plausibility.** Are the wrong options at least
   somewhat plausible to someone who half-knows the material, or are one or
   more of them obviously unrelated filler? Weak/unrelated distractors are
   a `WEAK_DISTRACTORS` finding, not a reason to reject the case outright.
4. **Judge explanation correctness and completeness.** Is `question_text`'s
   explanation (visible in the source fixture, summarized via
   `reviewer_rationale` here) actually correct, and does it justify the
   answer rather than just restating it? Missing entirely →
   `EXPLANATION_MISSING`. Present but thin/non-justifying →
   `EXPLANATION_INCOMPLETE`.
5. **Verify the domain assignment.** Does `domain` reasonably describe what
   this question is testing? Mis-tagged domain alone is usually a
   `correct_label` fix (correct the metadata), not a rejection.
6. **Verify the evidence actually supports the conclusion.** This is the
   most important check. `evidence_excerpt` is the *only* evidence frozen
   for this case — if the claimed answer requires something not actually
   present in that excerpt (even if it's true Salesforce knowledge you
   happen to know), that is a real defect (`UNSUPPORTED_ANSWER`, or
   `SOURCE_SUPPORT_WEAK` if it's a weak-but-plausible title/heading-only
   match), not something to wave through because you know the real answer.
7. **Distinguish blocking defects from warnings.** A wrong answer key,
   unsupported answer, multiple defensible answers, or missing explanation
   should block use of the case as-is (`correct_label` or `reject_case`).
   Ambiguity, weak distractors, incomplete explanations, and weak source
   support are quality issues worth fixing but don't necessarily make the
   case unusable.
8. **Mark uncertain cases for second review.** If you are genuinely unsure
   — not just "would like someone to double check," but actually unsure —
   set `needs_second_review` to `true`, use `confidence: low`, and add a
   short `sme_notes` explaining what's unresolved. This is independent of
   whatever `sme_decision` you record; you can approve with low confidence
   and still flag for a second look. Any case flagged this way (or decided
   `needs_second_review`) will hold up finalization of the reviewed fixture
   until someone resolves it with a different decision or clears the flag —
   that's intentional, so an uncertain case can never silently slip into a
   "fully reviewed" benchmark.

## Decision definitions

- **`approve`** — The case is correct as drafted: the stored answer is
  right, defect labeling (or lack of one) is right, and quality is
  acceptable. No changes needed. Still requires a `confidence` level.
- **`correct_label`** — The case is usable, but something about the AI's
  labeling is wrong: the wrong option is keyed, the finding code(s) are
  wrong or incomplete, the materiality is wrong, or similar. You must
  provide your corrected `sme_correct_answer` and/or `sme_finding_codes`
  (at least one), and `sme_notes` explaining the correction — a decision of
  `correct_label` with no correction and no explanation is not accepted, and
  neither is a correction that just repeats the AI's original answer/codes.
  You never need to (and cannot) supply materiality directly — it's always
  recalculated from your corrected finding code(s), the same way for every
  case, using the repository's canonical severity policy.
- **`reject_case`** — The case should not be used at all, even after a
  label correction (e.g., the evidence doesn't actually support any clean
  question here, or the case is fundamentally broken). Requires `sme_notes`
  explaining why — a phrase is enough, but it can't be blank. Marking a
  case `reject_case` does not remove it from the 40-case benchmark (that
  would silently shrink an intended-size benchmark) — instead, it holds the
  *entire benchmark* out of "finalized ground truth" status until someone
  replaces or corrects that case. A review where every case has a decision,
  including some `reject_case` decisions, is a *completed* review — but the
  benchmark is not yet *finalizable* until every rejected case is resolved.
- **`needs_second_review`** — You are not confident enough to approve,
  correct, or reject on your own and want another qualified reviewer's
  opinion before this case is finalized. Requires `sme_notes` describing
  what's unresolved. This decision (and the `needs_second_review=true` flag
  under any other decision) holds the whole review back from being
  finalized until it's resolved.

## What NOT to do

- Don't rewrite the question or explanation text yourself in this pass —
  flag what's wrong via `sme_decision` and `sme_notes`; content rewrites are
  a separate follow-up step.
- Don't approve a case just because you personally know the "real" answer
  from experience — always verify against `evidence_excerpt`, since that's
  what candidates would actually be shown as supporting evidence.
- Don't leave `sme_decision` blank if you've reviewed a case — a blank
  decision means "not yet reviewed" and will keep the case out of the
  finalized reviewed fixture.
- Don't leave `confidence` blank once you've entered a decision — every
  completed decision needs one.
- Don't record `correct_label` without actually filling in the corrected
  answer/finding codes and a short note — the importer treats an
  unexplained "wrong" as invalid, not as a usable correction.

## Provenance (handled by whoever runs the import, not by you in the spreadsheet)

You don't need to do anything for this section — it's here so you know what
happens after you hand back the completed CSV. When someone runs the import
tool to turn your completed review into a finalized fixture, they must
supply an internal reviewer identifier (`--reviewer-id`, e.g. a username —
not a personal name or email) and the CSV's `source_fixture_sha256` must
still match the benchmark file currently in the repository. The finalized
fixture records that reviewer identifier, the source-fixture hash, and the
exact UTC timestamp the import happened, so every finalized benchmark can
be traced back to who reviewed it, against what exact source content, and
when.

## How your decision becomes the benchmark's ground truth

You don't need to do anything for this section either — it explains what
the import tooling does with your decision, so you understand what "ground
truth" means once a case is finalized.

- For `approve`, the case's effective answer label(s), finding code(s),
  materiality, and `known_good` status in the finalized fixture are exactly
  the AI-drafted values — nothing changes.
- For `correct_label`, the finalized fixture's effective answer label(s)
  and/or finding code(s) become **your** corrected value(s). Specifically:
  - **`sme_correct_answer` blank** → inherits the AI-drafted answer labels.
  - **`sme_correct_answer` filled** → replaces the AI-drafted answer labels.
  - **`sme_finding_codes` blank** → inherits the AI-drafted finding codes ("inherit" only, never clear to nothing).
  - **`sme_finding_codes` = one or more canonical codes** → replaces the AI-drafted finding codes with your list.
  - **`sme_finding_codes` = `CLEAR`** → replaces the AI-drafted finding codes with an empty list (effective `known_good=true`).

  `known_good` and materiality are then *recalculated* from the resulting
  finding codes: a case ends up effectively known-good only if its
  resolved finding-code list is empty (including when `CLEAR` was used),
  and each remaining code's materiality comes from the repository's
  canonical finding-code policy, never from anything typed into the CSV.
- The finalized fixture keeps the original AI-drafted label too (in a
  separate, clearly-named field), so nothing is discarded — but the
  benchmark harness and its scoring only ever read the resolved,
  SME-adjudicated value described above.

## A note on "AI–SME agreement"

The import tooling reports an **AI–SME agreement rate**: the fraction of
your decided cases where you chose `approve` (i.e., agreed with the AI
drafter's original label). This is a comparison between the one AI drafter
and the one SME reviewer — it is **not** a human inter-rater reliability
metric, and it should not be read or reported as one. Measuring genuine
inter-rater agreement would require a second, independent qualified human
reviewer, which is out of scope here.

## Timing

Most cases (single evidence chunk, single clear judgment) should take
roughly 2–4 minutes once you're used to the layout: read question + options
(30–60s), read evidence excerpt (30–45s), decide (30–60s), fill in columns
(15–30s). Cases with two evidence chunks, or ambiguity/multiple-defensible-
answer cases, may run closer to 4–6 minutes — that's expected and fine.
