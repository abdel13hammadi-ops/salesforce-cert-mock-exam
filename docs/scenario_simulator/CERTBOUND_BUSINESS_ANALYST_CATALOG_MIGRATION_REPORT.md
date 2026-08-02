# CertBound Business Analyst Catalog Migration Report

**Task ID:** CERTBOUND-BA-CATALOG-01A
**Task status:** COMPLETE — uncommitted, ready for independent review
**Date:** 2026-08-02
**Branch:** `main`
**Starting HEAD:** `ef1c828ba5036b281a5b33ad64955cf59ef27835` ("Add reproducible CertBound base schema migration")
**Ending HEAD:** `ef1c828ba5036b281a5b33ad64955cf59ef27835` (unchanged — no commit was made)

---

## 1. Starting / ending git status

**Starting `git status --short --branch`:**

```
## main...origin/main [ahead 26]
 M docs/scenario_simulator/SCENARIO_ENGINE_V2_STAGING_BOOTSTRAP_REPORT.md
?? .local/
?? local_only/
?? scripts/v58_run_combined_policy_evaluation.py
?? structural_audit_state.json
?? tests/test_combined_policy_evaluator.py
?? v68_corrected_review_bundle/
?? v68_final_review_bundle/
?? v68_review_bundle/
?? workers/combined_policy_evaluator.py
```

**Ending `git status --short --branch`:**

```
## main...origin/main [ahead 26]
 M docs/scenario_simulator/SCENARIO_ENGINE_V2_STAGING_BOOTSTRAP_REPORT.md
?? .local/
?? local_only/
?? scripts/v58_run_combined_policy_evaluation.py
?? structural_audit_state.json
?? supabase/.temp/
?? supabase/migrations/20260802175000_v70_business_analyst_catalog.sql
?? supabase/tests/v70_business_analyst_catalog_verification.sql
?? tests/test_add_business_analyst_catalog_migration.py
?? tests/test_combined_policy_evaluator.py
?? v68_corrected_review_bundle/
?? v68_final_review_bundle/
?? v68_review_bundle/
?? workers/combined_policy_evaluator.py
```

Only three new untracked files were added by this task: the V70 migration, its SQL verification, and one new Python test file. `supabase/.temp/` is a pre-existing local Supabase CLI cache directory (timestamps confirm it dates from the earlier `SIM-STREAMLIT-V2-03B` staging-bootstrap task's `supabase link`, not from this task); it was not created, modified, or removed by this task, and this task's own disposable rebuild ran entirely in a separate directory outside the repository (`%TEMP%\ba_cat_disposable_20260802`), never touching this repo's `supabase/` folder at all. Nothing is staged. All other untracked/modified paths pre-date this task and were left untouched.

The pre-existing modification to `docs/scenario_simulator/SCENARIO_ENGINE_V2_STAGING_BOOTSTRAP_REPORT.md` was verified **unchanged byte-for-byte** via SHA-256 hash comparison at task start and task end:
`2E69CFD3A8A209E5C8EAADAC3492CE1A63D6D22B802263779C90B4FF5766ACD9` (match).

**`git diff --check`:** exit code 0, no output (no whitespace errors).

**`git diff -- <new file>`** for each of the three new files: no output, because `git diff` (without `--no-index`/`-N`) does not diff untracked files. This is expected; the files are new and untracked, not modified-and-tracked. Their full content is included in this repository as new files, available for direct review via the files themselves.

---

## 2. Preflight

- Shell responsive: **yes** (`Write-Output "shell-ok"` printed `shell-ok`).
- Branch: **main** (confirmed).
- HEAD: **exactly `ef1c828`** (confirmed, matches expected).
- Nothing staged: **confirmed**.
- Only expected tracked modification: **`docs/scenario_simulator/SCENARIO_ENGINE_V2_STAGING_BOOTSTRAP_REPORT.md`** — confirmed, no other tracked file was modified.
- No unexpected tracked modification: **confirmed**.
- Supabase CLI available: **yes, v2.109.1**.
- No staging/production credential selected: a **stale cached `CERTBOUND_PROD_DATABASE_URL`** was found in this terminal's process environment (a carry-over from an earlier session in this same terminal, not a newly-set credential). It was explicitly scrubbed via `Remove-Item Env:\CERTBOUND_PROD_DATABASE_URL` and `Remove-Item Env:\PGOPTIONS` before any database work, and verified absent. No staging or production credential was used at any point during this task.
- No linked-database command was used: **confirmed** — every `supabase` CLI invocation in this task targeted the disposable local project (`ba_cat_disposable_20260802`) only; `supabase db push`, `supabase db pull`, `supabase db dump --linked`, and any remote/linked-project command were never run.

---

## 3. Phase 1 — Authoritative catalog discovery

### Source matrix

| Field | Canonical value | Authoritative source | Exact location | Corroborating source(s) | Conflicts | Resolution basis |
|---|---|---|---|---|---|---|
| Certification display name | `Salesforce Certified Business Analyst` | `workers/certification_registry.py` | `BA_EXAM_NAME` constant, line 49 | `scenario_content/business_analyst/catalog.json` (`certificationExamName`); `cb_sc_001_onboarding_handoff_v1_1_0.json` document (`certificationExamName`); `workers/official_evidence_seed.py` (imports `BA_EXAM_NAME`); `tests/test_certification_registry.py` | None | Source-precedence #1 (executable engine registry); exact string match confirmed against three independent repository artifacts |
| Certification code | `BA-201` | `workers/certification_registry.py` | `CERTIFICATION_CODES[BA_EXAM_NAME]`, line 61 | `workers/official_evidence_seed.py`, `CERTIFICATION_CODES[BA_EXAM_NAME]` (line 84, verbatim `"BA-201"`); `scenario_content/business_analyst/catalog.json`, both scenario entries' `examCode: "BA-201"`; `tests/test_certification_registry.py::test_ba_201_internal_code_resolves_to_canonical_certification` | None | Source-precedence #1, corroborated by #2 (a separate frozen evidence-seed module) and #3 (test fixture) |
| Certification identifier / stable key | `exam_name` (natural key, `UNIQUE` constraint `certifications_exam_name_key`) | `supabase/migrations/20260101000000_v00_certbound_base_schema.sql`, lines 130–142 | — | Every prior catalog migration (V61/V64/V65) uses the identical convention: no hardcoded `certifications.id`, `exam_name` is the stable lookup key | None | Matches the schema's own design and 100% of precedent catalog migrations; no certification in this catalog uses a hardcoded UUID |
| Certification active state | `false` (inactive at insert time) | Established convention in `20260713224500_v61_...`, `20260714110000_v64_...`, `20260714120000_v65_...` (PAB/SCC/SVC all inserted `is_active = false`) | V61 lines 62–76; V64 lines 53–68; V65 lines 64–79 | `public.questions` contains 0 rows for any certification in a from-scratch migrated database (verified in the disposable rebuild) | None | Same rationale as precedent: no human-reviewed Business Analyst question exists in `public.questions` in this repository-owned database state |
| Domain names (×6), in order | Customer Discovery; Collaboration with Stakeholders; Business Process Mapping; Requirements; User Stories; User Acceptance | `workers/certification_registry.py` | `_ba_definition()`, lines 185–215 | `supabase/migrations/20260629120000_v46_free_mock_curation_foundation.sql`, `public.free_mock_blueprint_v1()`, `'Salesforce Certified Business Analyst'` branch, lines 231–238 (identical 6 names, same order, already committed/unmodifiable); `tests/test_certification_registry.py::test_exact_domain_names` | None | Source-precedence #1, corroborated verbatim by an independent, already-committed migration (source-precedence #2) and by an existing test fixture (source-precedence #3) |
| Domain weights | 17, 17, 17, 17, 16, 16 | `workers/certification_registry.py` | `_ba_definition()`, lines 199–212 | `tests/test_certification_registry.py::test_domain_weight_total_equals_100` | None | Source-precedence #1, corroborated by test fixture |
| Domain order | 1–6 in the order listed above | `workers/certification_registry.py` | `_ba_definition().domains` tuple order, lines 198–213 | `public.free_mock_blueprint_v1()` lists the same 6 names in the same order | None | Same as above |
| Domain identifier pattern | Engine-side only (`domain_id`, e.g. `customer_discovery`); **not** a persisted database column | `workers/certification_registry.py`, `CertificationDomain.domain_id` field | `public.certification_domains` schema (`20260101000000_v00_certbound_base_schema.sql`, lines 158–171) has no domain-code/domain-id column | Every prior catalog migration (V61/V64/V65) follows this same convention | None | The schema was verified against live production in CERTBOUND-DB-BASELINE-01 and genuinely has no domain-code column; this is not an omission introduced by this task |
| Total domain weight | 100 | Computed from the six weights above | — | `tests/test_certification_registry.py::test_domain_weight_total_equals_100` asserts this independently | None | Arithmetic identity: 17+17+17+17+16+16 = 100 |
| `passing_score` / `time_limit_minutes` | 65 / 105 | `supabase/migrations/20260101000000_v00_certbound_base_schema.sql`, lines 135–136 (`certifications.passing_score DEFAULT 65`, `time_limit_minutes DEFAULT 105`) | — | None found beyond the schema's own column defaults | See discussion below | No Business-Analyst-specific override exists anywhere in the repository; the generic `app.py` fallback (`PASSING_SCORE_DEFAULT = 68`) is explicitly documented in the V61/PAB-EXP-03 migration as "not an official value" and was therefore not used |

### Conflict/ambiguity discussion: `passing_score` / `time_limit_minutes`

`workers/certification_registry.py` (the highest-precedence source) does not model exam-timing metadata at all — only `certification_code`, `aliases`, and `domains`/`weights`. No other repository file defines a Business-Analyst-specific `passing_score` or `time_limit_minutes`. The only generic fallback found (`app.py`'s `PASSING_SCORE_DEFAULT = 68`) is explicitly and directly documented, in the already-committed `20260713224500_v61_add_platform_app_builder_certification_catalog.sql` migration's own header, as a "generic app-wide fallback... not an official value" and is excluded from use by that migration's own reasoning.

Given no invented or externally-researched value was permissible, this migration uses the `public.certifications` table's own column `DEFAULT` values (`passing_score DEFAULT 65`, `time_limit_minutes DEFAULT 105`), defined in `20260101000000_v00_certbound_base_schema.sql`. That base-schema migration is itself a repository-owned artifact whose column defaults were reconstructed from, and verified against, live production schema metadata during the prior `CERTBOUND-DB-BASELINE-01` task (schema-only, read-only production inspection). This is therefore an existing repository-owned value (source-precedence #2, "existing repository migration or seed definition"), not an invented one — it was inserted explicitly in the migration (rather than left to apply implicitly) so the intended value is visible in the file and enforced by the Case 2 exact-match check.

This did not rise to a STOP CONDITION because the task's own Phase 1 "Required fields" list does not include `passing_score`/`time_limit_minutes` (only certification name/code/identifier/active-state and domain name/code/weight/order/total are listed as required), and no repository source materially disagreed with the schema-default value — there was simply an absence of a certification-specific override, which is a normal, addressable gap rather than a genuine multi-source conflict.

### Important discovery: `certification_registry.py` domains vs. free_mock_curation cross-check

An independent, already-committed, unmodifiable migration — `20260629120000_v46_free_mock_curation_foundation.sql` — contains a `public.free_mock_blueprint_v1()` SQL function with a hardcoded `CASE WHEN 'Salesforce Certified Business Analyst'` branch listing the exact same six domain names, in the exact same order, as `certification_registry.py`'s `_ba_definition()`. This is strong, independent, pre-existing corroboration (not something this task added or could have influenced) that the six domain names are correct, stable, and already relied upon elsewhere in the repository — not a definition invented for this task.

### Important discovery: pre-existing verification-SQL assumptions about Business Analyst

`supabase/tests/v64_sales_cloud_consultant_certification_catalog_verification.sql` (an already-existing, unmodified file, not touched by this task) contains an "S6" check that asserts:

```sql
SELECT count(*) INTO v_ba_count
FROM public.certifications
WHERE exam_name = 'Salesforce Certified Business Analyst'
  AND is_active = true;
ASSERT v_ba_count = 1, ...
```

This assumes Business Analyst is **already present and `is_active = true`** — the same assumption that check makes about `Administrator` (which is definitively known, from the prior `CERTBOUND-DB-BASELINE-01` task, to be real, pre-`V44` production content that was never captured in any migration). This is corroborating evidence that Business Analyst was very likely one of the original, pre-migration-system, human-content-bearing certifications in real production (alongside Administrator) — not a "new" certification comparable to Platform App Builder/Sales Cloud Consultant/Service Cloud Consultant.

This is **not** a stop condition: no migration anywhere in the repository actually `INSERT`s a `Salesforce Certified Business Analyst` row (only this task's new V70 migration does), so there is no existing migration-created row to conflict with, and adding it fresh (Case 1) does not change any existing row. It is, however, an important, honest disclosure: after V70 applies, `v64`'s and `v65`'s own S6 checks will continue to fail on Business Analyst — previously because the row was entirely absent (`found 0`), now because the row exists but with `is_active = false` (the value this task's migration inserts, following the PAB/SCC/SVC precedent for a certification with zero real `questions` rows in this repository-owned database state) rather than `true`. This is a **pre-existing test-fixture assumption**, not a regression introduced by V70, and this task's authorized scope does not include editing V61/V64/V65's own verification files. See §11 below (SQL results by file) for the exact behavior observed.

### Cross-check: does anything else already validate/depend on this Business Analyst definition?

- **Scenario catalog validation** (`utils/scenario_catalog.py`): resolves `certificationExamName` purely from `scenario_content/business_analyst/catalog.json` (a repository JSON file), never from `public.certifications`. Confirmed exact match: `"Salesforce Certified Business Analyst"` in both.
- **`diagnose_cb_sc001_publication_readiness`** (`utils/scenario_streamlit_v2.py`): only queries `public.scenarios` / `public.scenario_versions`. It **never** queries `public.certifications` or `public.certification_domains`. This is an important, precise finding — see §9 (Phase 6) below.
- **Certification registry tests** (`tests/test_certification_registry.py`): already assert the exact 6-domain/100-total shape, the `BA-201` code, and alias resolution — none of these tests touch a database; they exercise the pure-Python registry module directly.
- **Application routing** (`app.py`, `pages/*.py`): filters `public.certifications` on `is_active = true` for the traditional mock-exam UI; Business Analyst is inserted `is_active = false`, matching the same withholding behavior already used for Platform App Builder/Sales Cloud Consultant/Service Cloud Consultant.

No STOP CONDITION was triggered: an authoritative definition exists, is complete, has no material disagreement across sources, and requires no external research.

---

## 4. Phase 2 — Migration design

**File created:** `supabase/migrations/20260802175000_v70_business_analyst_catalog.sql`

Design summary:

- Follows the established `V61`/`V64`/`V65` catalog-migration convention exactly (single `DO $$ ... $$` block; `v_exam_name`/`v_cert_code`/`v_passing_score`/`v_time_limit_minutes` locals; Case 1 fresh-insert / Case 2 exact-match no-op / Case 3 fail-closed structure).
- Inserts exactly one `public.certifications` row (`exam_name`, `display_name`, `certification_code`, `passing_score`, `time_limit_minutes`, `question_count = 0`, `is_active = false`) and exactly six `public.certification_domains` rows (`domain_name`, `weight`, `question_count = 0`, `display_order`, `is_active = true`).
- **Adds one guard beyond the V61/V64/V65 precedent**: before doing anything else, it checks whether the canonical `certification_code = 'BA-201'` has already been claimed by a *different* `exam_name` (the schema has no `UNIQUE` constraint on `certification_code`, so nothing else would catch this), and `RAISE EXCEPTION`s immediately if so. This directly satisfies the task's explicit "canonical certification code assigned to another certification name" / "certification ID assigned to another certification" conflict-safety requirement, which the precedent migrations did not need to check (none of PAB/SCC/SVC's codes overlap with anything) but which is exercised for Business Analyst in disposable Test D (§6).
- Uses `ON CONFLICT` nowhere; every conflict path is an explicit `RAISE EXCEPTION` (12 total exception sites).
- Never `UPDATE`s or `DELETE`s any row; never touches `public.questions`, `public.scenarios`, `public.scenario_versions`, RLS, policies, grants, indexes, triggers, or any other table/certification.
- Deterministic: identical `v_exam_name`/`v_cert_code`/domain literals every time; no external input, no `now()`/`gen_random_uuid()` dependency in any comparison.

**Conflict-detection behavior:**

1. Cross-certification-code guard (new, see above) — fails closed if `BA-201` belongs to another `exam_name`.
2. Duplicate-certifications-row guard (`v_cert_count > 1`) — fails closed.
3. Case 1 (no existing row): guards against orphaned `certification_domains` rows with no parent, then inserts.
4. Case 2 (existing row): exact-match verification on `certification_code`, `passing_score`, `time_limit_minutes`, `is_active`, `question_count`, exact domain count (6), no duplicate domain names, and an exact name+weight+display_order join match across all 6 domains — anything less than a full match is Case 3.
5. Case 3: `RAISE EXCEPTION` with an actionable message identifying exactly which field(s) diverged.

**Postcondition checks:** enforced both inside the migration itself (Case 2's exact-match re-verification logic, which is effectively also a self-check any time the migration is re-run) and, separately and more completely, by the dedicated SQL verification file (§5), which independently re-derives and asserts every item on the task's postcondition list.

**Existing migrations modified:** **none.** Only one new file was created.

**Schema objects changed:** **none** beyond row-level `INSERT`s into two already-existing tables. No `RLS`, `GRANT`/`REVOKE`, `CREATE INDEX`/`DROP INDEX`, trigger, function, or column change of any kind.

---

## 5. Phase 3 — SQL verification

**File created:** `supabase/tests/v70_business_analyst_catalog_verification.sql`

Eight independent `DO $$ ... $$ / ASSERT` blocks (S1–S8), covering:

- S1: exactly one certification row; exact `exam_name`, `certification_code = 'BA-201'`, `passing_score = 65`, `time_limit_minutes = 105`, `is_active = false`, `question_count = 0`.
- S2: exactly six domain rows; exact name/weight/`display_order` match against the engine profile; all `is_active = true`.
- S3: no duplicate domain names; no domain names outside the canonical six.
- S4: weights total exactly 100; three explicit spot-checks (17/16/16) confirming no rounding/truncation.
- S5: no orphaned `certification_domains` rows (every BA domain row references an existing `certifications` row).
- S6: Platform App Builder, Sales Cloud Consultant, and Service Cloud Consultant certification rows remain present, and their own domain weight totals are unchanged (100 / ≈99.9 / 100 respectively) — proving V70 did not touch unrelated certifications.
- S7: `public.scenarios`, `scenario_versions`, `scenario_attempts`, `scenario_decisions` all still have 0 rows.
- S8: RLS remains enabled (`relrowsecurity = true`) on both `public.certifications` and `public.certification_domains`.

All 18+ individual `ASSERT` statements are designed to fail loudly (raise a PL/pgSQL exception with a descriptive message) on any mismatch; none are soft/silent checks.

---

## 6. Phase 4 — Disposable rebuild

**Disposable environment identity:** a brand-new local Supabase/Postgres stack, `project_id = ba_cat_disposable_20260802`, initialized via `supabase init` in `C:\Users\Abdel\AppData\Local\Temp\ba_cat_disposable_20260802` (entirely outside the repository working tree), running via Docker Desktop (server v29.6.1). All 55 repository migration files (`V00`→`V70`) were copied into this disposable project's `supabase/migrations/`; the repository's own `supabase/migrations/` directory was never targeted by any `supabase start`/`reset` command.

**Full migration apply result:** `supabase db reset` applied **all 55 migrations with zero errors**. Every `NOTICE` printed during application was an already-known, pre-existing, benign notice from earlier migrations (extension-already-exists, trigger-does-not-exist-skipping, repair-migration-skipped-because-content-absent) — none newly introduced by V70. V70 itself printed exactly:

```
NOTICE (00000): BA-CAT-01: inserted certifications row and 6 certification_domains rows for Salesforce Certified Business Analyst.
```

**Migration history (`supabase migration list --local`):**

- Count: **55** (exact expected count).
- Earliest: **`20260101000000`** (V00).
- Latest: **`20260802175000`** (V70).
- Duplicates: **none**.
- `local`/`remote` mismatches: **none** (0 of 55).
- Unexpected versions: **none** — every version present is an already-known repository migration filename.

**V70 SQL verification result:** all 8 `DO` blocks (S1–S8) completed successfully with **zero `ASSERT` failures** against the clean rebuild.

**Base/final catalog row counts (clean rebuild, before any conflict testing):**

| Table | Count |
|---|---|
| `public.certifications` | 4 (Platform App Builder, Sales Cloud Consultant, Service Cloud Consultant, **Business Analyst**) |
| `public.certification_domains` | 24 (5 + 5 + 8 + **6**) |
| `public.questions` | 0 |
| `public.scenarios` | 0 |
| `public.scenario_versions` | 0 |
| `public.scenario_attempts` | 0 |
| `public.scenario_decisions` | 0 |
| `public.app_users` | 0 |
| `public.billing_checkout_claims` | 0 |
| `public.billing_events` | 0 |
| `public.exam_attempts` | 0 |
| `public.question_attempts` | 0 |
| `public.support_tickets` | 0 |
| `public.user_certification_access` | 0 |

("Administrator" is correctly absent from this migration-only-built database, consistent with the already-known finding from `CERTBOUND-DB-BASELINE-01` that Administrator's original catalog row was never captured by any migration.)

### Disposable migration safety tests (A–G)

All tests were run by directly editing rows in the running disposable database (via `docker exec ... psql`) to synthesize each conflict state, re-running the exact V70 migration file, observing the result, and then reverting the edit before the next test. No test left the disposable database in a corrupted state at the end (confirmed by a final full V70 SQL verification pass and a final row-count check after all seven tests completed).

| Test | Setup | Result | Verdict |
|---|---|---|---|
| **A. Clean V69 state, no BA rows** | (the initial full V00→V70 rebuild itself) | Case 1 fired; inserted 1 certification + 6 domains; `NOTICE` printed | **PASS** |
| **B. Exact canonical rows already present** | Re-ran V70 migration file unchanged against its own just-inserted output | Case 2 fired; `NOTICE: ...already exist in the expected shape; no changes made.`; row counts unchanged (4 certs / 24 domains) — **no duplicates created** | **PASS** |
| **C. Conflicting certification code** | `UPDATE certifications SET certification_code = 'BA-999-CONFLICT' WHERE exam_name = 'Salesforce Certified Business Analyst'` | `ERROR: BA-CAT-01: existing certifications row for Salesforce Certified Business Analyst has certification_code=BA-999-CONFLICT (expected BA-201)...` — migration aborted, nothing written | **PASS (fails closed)** |
| **D. Conflicting certification identifier** (interpreted as: `certification_code = 'BA-201'` already claimed by a different `exam_name`) | Deleted the BA cert+domains, then `UPDATE certifications SET certification_code = 'BA-201' WHERE exam_name = 'Salesforce Certified Platform App Builder'` | `ERROR: BA-CAT-01: certification_code=BA-201 is already assigned to a different certification (exam_name=Salesforce Certified Platform App Builder)...` — migration aborted before Case 1/2/3 logic even ran | **PASS (fails closed)** |
| **E. Conflicting domain weight** | `UPDATE certification_domains SET weight = 99 WHERE exam_name = '...Business Analyst' AND domain_name = 'Customer Discovery'` | `ERROR: BA-CAT-01: certification_domains rows for ... do not exactly match the 6 expected domain names/weights/display_orders (5/6 matched)...` — migration aborted | **PASS (fails closed)** |
| **F. Incomplete existing domain set** | `DELETE FROM certification_domains WHERE exam_name = '...Business Analyst' AND domain_name = 'User Acceptance'` (5 of 6 remain) | `ERROR: BA-CAT-01: expected exactly 6 certification_domains rows for ..., found 5...` — migration aborted | **PASS (fails closed)** |
| **G. Domain weights not totaling 100** | Re-applied the Test E weight corruption (99 instead of 17 → total 182) and ran the **V70 SQL verification** (not the migration) | `S2` failed first (`5 matched` instead of 6) and, independently, `S4` failed (`expected ... to total exactly 100, found 182.0`) — verification script raised loudly on both | **PASS (verification fails)** |

After Test G, the corrupted weight was reverted and the full V70 SQL verification was re-run, confirming all 8 blocks passed again (database restored to the exact canonical state).

**Final state after all conflict testing:** re-confirmed via a fresh, complete `supabase db reset` (all 55 migrations re-applied from an empty database with the finalized migration file) — see the "Base/final catalog row counts" table above, which reflects this final clean state, not the intermediate conflict-test state.

---

## 7. Phase 5 — Python tests

**Command 1 (exact, as specified in the task):**

```
python -m pytest tests/test_scenario_catalog.py tests/test_scenario_streamlit_v2.py tests/test_scenario_controller_v2.py tests/test_scenario_supabase_port_v2.py tests/test_scenario_orchestration_v2.py tests/test_scenario_persistence_v2.py tests/test_scenario_engine_v2.py tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py -q -rs
```

**Result:** `713 passed, 4 skipped, 48 subtests passed` in 29.40s. Zero failures, zero errors.

Skipped (all 4 are pre-existing, intentional skips unrelated to this task):
- `tests/test_scenario_streamlit_v2.py:1195`, `:1198` — "covered by port smoke; streamlit integration uses dedicated full-terminal test"
- `tests/test_scenario_controller_v2.py:889`, `:894` — "covered by TestSupabasePortDisposablePostgrestSmoke; this subclass exercises the controller instead"

**Command 2 (certification-registry and catalog-migration contract tests, directly affected by this migration):**

```
python -m pytest tests/test_certification_registry.py tests/test_add_business_analyst_catalog_migration.py tests/test_add_sales_cloud_consultant_catalog_migration.py -q -rs
```

**Result:** `168 passed, 92 subtests passed` in 0.20s. Zero failures, zero errors.

**New test file added:** `tests/test_add_business_analyst_catalog_migration.py` (37 test methods, 26 subtests), mirroring the existing `tests/test_add_sales_cloud_consultant_catalog_migration.py` convention exactly (static, text-based SQL-contract checks — no live database connection). It includes one test (`test_registry_domain_names_match_certification_registry_module`) that directly imports `workers/certification_registry.py` and cross-checks the migration's literal domain names/weights/order against the live registry, so any future drift between the two would be caught immediately.

No SQL test file in this task's scope was claimed to "pass" when it was actually sequence-dependent or fixture-dependent — see §11 for the explicit classification of every SQL file executed.

---

## 8. Phase 6 — CB-SC-001 readiness recheck

All checks below were performed against the **disposable local database only** (`http://127.0.0.1:54321`, the same instance used for the Phase 4 rebuild), using the service-role key printed by `supabase start` for that disposable project. No staging or production instance was contacted.

- **Certification catalog resolution:** `public.certifications` now contains exactly one `Salesforce Certified Business Analyst` row (`certification_code = BA-201`, `passing_score = 65`, `time_limit_minutes = 105`, `question_count = 0`, `is_active = false`), and `public.certification_domains` contains exactly the 6 expected rows in the expected order/weights — confirmed by direct API query.
- **Scenario schema validation:** `load_cb_sc001_v2_content()` (which internally calls schema validation) succeeded without exception.
- **Scenario catalog validation:** `certificationExamName` in both `scenario_content/business_analyst/catalog.json` and the CB-SC-001 document itself resolve to exactly `"Salesforce Certified Business Analyst"`, matching the newly-inserted certification row's `exam_name` byte-for-byte.
- **`diagnose_cb_sc001_publication_readiness(client, content=content, supabase_url=API_URL)`** against the disposable database returned:
  ```
  ready = False
  findings = ('scenario_catalog_entry_missing',)
  target_appears_non_production = True
  ```

**Important, precise finding:** `diagnose_cb_sc001_publication_readiness` (in `utils/scenario_streamlit_v2.py`) queries **only** `public.scenarios` and `public.scenario_versions` — it never queries `public.certifications` or `public.certification_domains` at all, and `scenarios.certification_exam_name` has no foreign key to `certifications.exam_name` in the schema. This means the function's `ready`/`findings` result is **not, and was never, technically gated** by the presence or absence of the Business Analyst catalog rows this task adds. Before and after V70, this diagnostic returns the identical single finding, `scenario_catalog_entry_missing` — because no `public.scenarios` row exists yet for `cb-sc-001-onboarding-handoff-vslice` (creating one is explicitly out of scope for this task: "do not create scenario records").

This does not mean V70 was unnecessary: it closes a real, separate, and previously-undocumented catalog-completeness gap (Business Analyst was the only fully-specified, test-covered, cross-referenced engine-profile certification with **zero** rows in `public.certifications`/`public.certification_domains`), which matters for admin/dashboard display, `certification_domain_exists()`-gated candidate generation, `list_supported_exam_names()` consistency, and general soundness of the catalog — but it is reported here precisely and honestly rather than overstated: **missing Business Analyst catalog rows were never the technical blocker inside `diagnose_cb_sc001_publication_readiness`; the missing `public.scenarios` row is, and that remains out of this task's scope.**

No scenario publication occurred (`public.scenarios` and `public.scenario_attempts` were re-confirmed at 0 rows immediately after running the diagnostic). No scenario attempt was started. No publication RPC was called.

---

## 9. Files created / modified

**Files created (3):**

1. `supabase/migrations/20260802175000_v70_business_analyst_catalog.sql`
2. `supabase/tests/v70_business_analyst_catalog_verification.sql`
3. `tests/test_add_business_analyst_catalog_migration.py`
4. `docs/scenario_simulator/CERTBOUND_BUSINESS_ANALYST_CATALOG_MIGRATION_REPORT.md` (this file)

**Files modified:** none (the one pre-existing unstaged modification, `SCENARIO_ENGINE_V2_STAGING_BOOTSTRAP_REPORT.md`, was left completely untouched and verified byte-identical).

**Existing migrations modified:** none.

**Protected paths touched:** none. `.local/`, `local_only/`, `scripts/v58_run_combined_policy_evaluation.py`, `structural_audit_state.json`, `tests/test_combined_policy_evaluator.py`, `workers/combined_policy_evaluator.py`, and the `v68_*_review_bundle/` directories were never inspected, opened, searched, executed, modified, staged, or referenced.

**Temporary artifacts:** the disposable Supabase/Postgres stack, its Docker containers/volumes, and all scratch SQL/Python scripts under `%TEMP%` were fully torn down and removed (`supabase stop --no-backup`, followed by directory/file removal and verification that no `ba_cat_disposable*`/`ba_*` artifacts remain on disk or in `docker ps -a`).

---

## 10. Database/schema objects changed

**None**, other than the two `INSERT` operations (1 certification row + 6 domain rows) performed *by the migration when applied* — and that migration was only ever applied to the disposable local database, never to staging or production. No table, column, constraint, index, RLS policy, grant, function, or trigger was added, altered, or dropped by this task.

---

## 11. SQL files executed and results (Phase 4/5 classification)

| File | Executed against | Result | Classification |
|---|---|---|---|
| `supabase/tests/v70_business_analyst_catalog_verification.sql` (new, this task) | Disposable DB, clean rebuild | **8/8 blocks passed** | Applicable to final V70 state — designed for, and passes against, a fresh bootstrap |
| `supabase/tests/v61_platform_app_builder_certification_catalog_verification.sql` (pre-existing, unmodified) | Disposable DB, post-V70 | First 3 blocks passed; 4th (`S4`, expects an active Administrator row) failed: `found 0` | **Not applicable to a from-scratch migration-only rebuild** — Administrator's original catalog row was never captured by any migration (pre-existing condition, unrelated to and unchanged by V70; consistent with the finding already documented in `CERTBOUND-DB-BASELINE-01`) |
| `supabase/tests/v64_sales_cloud_consultant_certification_catalog_verification.sql` (pre-existing, unmodified) | Disposable DB, post-V70 | First 5 blocks passed; `S6` failed: `expected exactly 1 active Administrator certifications row, found 0` | **Not applicable to a from-scratch migration-only rebuild**, for the same Administrator-related reason as above. (This file's `S6` also separately asserts `Business Analyst is_active = true`, which the query never reached because the `Administrator` assertion inside the same `DO` block aborted first — see §3's discussion of this pre-existing fixture assumption.) |
| `supabase/tests/v65_service_cloud_consultant_certification_catalog_verification.sql` (pre-existing, unmodified) | Disposable DB, post-V70 | First 5 blocks passed; `S6` failed: `expected exactly 1 active Administrator certifications row, found 0` | **Not applicable**, same reason as V64 |

No SQL file in this list was claimed to pass in full when it did not; each partial result and its precise cause is stated explicitly above.

---

## 12. CB-SC-001 local readiness result (summary)

| Item | Result |
|---|---|
| Schema validation | Pass |
| Catalog validation | Pass |
| Certification resolution | Pass — exact match, `Salesforce Certified Business Analyst` |
| Domain reference resolution | Pass — CB-SC-001's content does not reference `certification_domains` rows directly (its own schema is domain-agnostic at the persistence layer); the standalone Business Analyst domain catalog now exists and is internally consistent |
| Local publication-prerequisite (missing BA catalog rows) | **No longer a gap** — the catalog rows now exist, are internally consistent, and total 100 |
| `diagnose_cb_sc001_publication_readiness` gating result | Unchanged by this task (`ready=False`, `findings=('scenario_catalog_entry_missing',)`) — this function was never gated by `certifications`/`certification_domains` rows; the sole remaining finding is the (out-of-scope) missing `public.scenarios` row |
| Scenario publication performed | No |
| Scenario attempt started | No |

---

## 13. Staging / production access

- **Staging database touched:** No. No connection of any kind was made to the CertBound Staging project (`oohxenhwzcjzagwsrrvq`) during this task.
- **Production database touched:** No. No connection of any kind was made to production during this task. The only production-adjacent event was discovering, and immediately scrubbing, a stale cached production connection-string environment variable left over in this terminal's process environment from an earlier, unrelated session; it was never used.

---

## 14. Errors encountered

- A stale `CERTBOUND_PROD_DATABASE_URL` environment variable was found cached in the terminal's process environment during preflight (same category of issue previously seen and resolved in `SIM-STREAMLIT-V2-03B`). Resolved by explicit scrub-and-verify before any database work.
- Minor Windows/PowerShell tooling friction: `psql` is not on `PATH` (worked around via `docker exec ... psql` against the disposable stack's own `db` container); `supabase migration list --local`'s JSON output needed to be captured via `Out-String` rather than direct stdout redirection due to PowerShell's handling of the CLI's interleaved stderr "Connecting to local database..." notice; a transient Windows file-lock briefly prevented removing the disposable temp directory immediately after `supabase stop` (resolved after a short wait). None of these affected the correctness of any result; all were tooling-level, not data-level, issues.

No SQL, Python, or migration-logic error occurred at any point.

---

## 15. Stop conditions encountered

**None.** No stop condition listed in the task was triggered:

- An authoritative, complete, non-conflicting Business Analyst definition was located (§3).
- No repository sources materially disagreed.
- Certification code/identifier were fully determined.
- Domain names/codes/order/weights were complete and total exactly 100.
- No external research was required.
- The schema safely represents the definition (verified via a full disposable rebuild and 7 conflict-scenario tests).
- No existing migration seeds Business Analyst under another identity (confirmed via repository-wide search — only a `CASE WHEN` blueprint function references it, never an `INSERT`).
- Adding Business Analyst required no changes to any existing row (Case 1 fresh insert only).
- No existing migration required modification.
- No staging/production connection became necessary.
- No staging/production credential was (intentionally) selected.
- Starting branch/HEAD matched exactly.
- No unexpected tracked changes or staged files existed.
- No protected-path access became necessary.

---

## 16. Remaining risks

1. **`certifications.is_active = false` vs. pre-existing verification-fixture assumptions.** `v64`'s/`v65`'s own S6 checks assume Business Analyst is `is_active = true`; this migration inserts it as `false` (matching the PAB/SCC/SVC precedent for zero-question-content certifications). If a future task activates Business Analyst without also reconciling this, the existing S6 assumption gap simply changes shape (missing → inactive) rather than resolving; this is disclosed, not hidden, but is worth flagging to the next reviewer.
2. **`passing_score`/`time_limit_minutes` provenance is a schema default, not a certification-specific source.** If a future, higher-authority repository source for Business-Analyst-specific exam timing metadata is discovered, these two values (currently 65/105, inherited from the base-schema column defaults) may need a follow-up migration to correct them — this migration's own Case 2 exact-match check would then correctly flag that as Case 3 (conflicting), which is the intended fail-closed behavior, not a defect.
3. **Real production content status for Business Analyst is still unknown from this task's vantage point.** Consistent with the `Administrator` precedent, production may hold real, human-authored Business Analyst exam content with different domain/weight/metadata values than what this migration inserts. This task strictly avoided any staging/production connection, so this cannot be confirmed or refuted here; a dedicated schema-only, read-only production catalog-content inspection (mirroring the discipline already used in `CERTBOUND-DB-BASELINE-01`) would be the appropriate way to resolve this before ever applying V70 to staging or production.
4. **`v61`/`v64`/`v65` verification files were not modified**, per this task's authorized scope — a future task may want to explicitly reconcile them (e.g., splitting the Administrator/Business-Analyst "pre-existing content" assumptions into their own documented, skippable check) so their partial-failure behavior is self-explanatory without needing this report for context.

---

## 17. Independent-review readiness decision

**READY FOR INDEPENDENT REVIEW.**

- The migration, its SQL verification, and this report are complete, uncommitted, and untracked.
- Every migrated value is traceable to an authoritative, non-invented, non-external repository source, cross-referenced against at least one independent corroborating source.
- The migration was validated against a complete, from-scratch V00→V70 disposable rebuild (55/55 migrations, zero errors) and against all seven required conflict-safety scenarios (A–G), each behaving exactly as specified (fail-closed on every conflict, safe no-op on exact-match re-apply, correct fresh-insert on a clean state).
- 713 + 168 = 881 Python tests pass across the required focused suites and the certification-registry/catalog-migration contract suites, with zero failures/errors.
- No staging or production connection occurred at any point.
- No scenario publication or attempt occurred.
- Nothing is staged, committed, pushed, or deployed.

---

## 18. Recommended next task

**CERTBOUND-BA-CATALOG-01B** — focused independent review of the V70 migration, SQL verification, authoritative catalog mapping, and disposable rebuild evidence.

Do not apply V70 to staging until that review passes and the migration is committed locally.
