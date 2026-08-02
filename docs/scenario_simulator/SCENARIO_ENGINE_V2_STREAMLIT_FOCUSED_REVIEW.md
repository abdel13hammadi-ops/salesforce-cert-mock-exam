# SIM-STREAMLIT-V2-01-REVIEW — Engine V2 CB-SC-001 Streamlit Vertical Slice

Independent production-readiness review. **No source, tests, SQL, or
migrations were modified.** No staging or production database was connected
for content verification. Nothing was staged, committed, pushed, or deployed.

## 0. Pre-flight

| Item | Result |
|------|--------|
| Branch | `main` |
| HEAD | `7776b61` — Complete Engine V2 learner controller |
| Starting `git status` | `## main...origin/main [ahead 23]`; nothing staged |
| In-scope delta | Exactly six expected files: `utils/scenario_streamlit_v2.py`, `pages/Scenario_Simulator_V2.py`, `tests/test_scenario_streamlit_v2.py`, `tests/test_scenario_controller_v2.py` (M), `utils/navigation.py` (M), `docs/...VERTICAL_SLICE_REPORT.md` |
| Protected paths | Untouched (not opened/searched/modified) |
| Ending HEAD | `7776b61` (unchanged) |
| Ending status | Same six in-scope paths + pre-existing protected/unrelated untracked; **plus this review file only** |

### Tests executed (review)

```
python -m pytest tests/test_scenario_streamlit_v2.py tests/test_scenario_controller_v2.py -q -rs
```

- **79 passed, 4 skipped, 3 subtests passed**, exit code 0
- Includes real disposable `TestScenarioStreamlitV2DisposableIntegration::test_real_streamlit_helper_start_resume_submit_flow` (Docker available)

### Out-of-repo full-terminal probe (no source changes)

Temporary script at `%TEMP%\sim_streamlit_v2_full_terminal_probe.py` drove the
**production Streamlit helpers** (`fetch_authoritative_cb_sc001_view` /
`submit_cb_sc001_v2_choice`) against a real disposable PostgREST v14.16 stack
seeded by the existing smoke harness:

- start → process-loss resume → 4 happy-path decisions →
  `isComplete=True` → terminal resume → terminal resubmit blocked
- Result: **`PROBE_RESULT=PASS_FULL_TERMINAL`**
- Containers torn down by `tearDownClass`

This proves the **helper + controller + port** stack can reach terminal without
code changes. It does **not** replace a committed page-level E2E, and it does
**not** clear the production content-source HIGH finding.

---

## Readiness decisions (mandatory)

### Milestone commit

**CORRECTIONS_REQUIRED**

Option B architecture, auth boundary, and helper contracts are sound enough
for an engineering checkpoint, but a production application module must not
load learner content from `tests/fixtures/`. That HIGH finding blocks a clean
local milestone commit of this vertical slice as shippable application code.

### Owner local / staging testing

**NOT_READY_FOR_OWNER_LOCAL_TESTING**

Fails explicit READY gates:

1. Production dependency on `tests/fixtures` (HIGH).
2. Staging/local Supabase published CB-SC-001 version not verified in this review
   (no non-production target was inspected; production was not accessed).
3. Implementation report’s owner-ready claim is premature relative to (1)–(2).

---

## Finding summary

| Severity | Count |
|----------|------:|
| BLOCKER | 1 |
| HIGH | 1 |
| MEDIUM | 7 |
| LOW | 3 |
| **Total** | **12** |

(BLOCKER and HIGH refer to the same content-source defect counted once each in
the severity tables below for gate clarity: it is both a mandatory HIGH under
finding #1 and a readiness BLOCKER under owner/staging criteria.)

---

## Mandatory findings

### 1. PRODUCTION CONTENT SOURCE — BLOCKER / HIGH

**Verdict: FAIL — production runtime loads `tests/fixtures/...`.**

Evidence:

```59:59:utils/scenario_streamlit_v2.py
CB_SC001_CONTENT_PATH = REPO_ROOT / "tests" / "fixtures" / "scenario_engine_v2_vslice_1_1_0.json"
```

- `load_cb_sc001_v2_content()` is called from **production page**
  `pages/Scenario_Simulator_V2.py` on every load and submit.
- No CB-SC-001 entry exists under `scenario_content/business_analyst/catalog.json`
  (catalog still only lists V1 `ba201-sim-meridian-health-01`).
- No `scenario_content/.../cb-sc-001...` artifact exists.

**Recommended production content location (in priority order):**

1. Approved artifact under `scenario_content/business_analyst/...` plus catalog
   entry (same pattern as BA-201), loaded via existing catalog/content helpers; or
2. Database-published scenario document/content hash as the sole runtime authority
   once a published version is guaranteed, with local content used only for
   offline validation/tests.

Tests may continue to use fixtures. Application modules under `utils/` and
`pages/` must not.

---

### 2. COMPLETE END-TO-END FLOW — CONDITIONAL

| Path | Result |
|------|--------|
| Committed disposable integration | Stops after first successful advancement (~scene 2). Does **not** prove terminal. |
| Unit tests with `FakeOrchestrationPersistence` | Full happy path → terminal resume + blocked resubmit (`test_u` / `test_v`) |
| Out-of-repo real PostgREST probe via Streamlit helpers | **PASS_FULL_TERMINAL** (start, 4 decisions, intermediate resumes, terminal, blocked resubmit) |
| Real Streamlit page UI browser path | Not proven (page tested via import/exec fakes only) |

**Classification:** technically capable through the application helper layer;
**not** proven by the committed disposable Streamlit integration test; page UI
E2E incomplete. Treat full-scenario readiness as **conditional** until the
committed real integration asserts terminal completion (and content is moved
out of `tests/`).

---

### 3. SCENARIO VERSION AVAILABILITY — UNVERIFIED FOR STAGING

`resolve_cb_sc001_scenario_version_id()`:

1. Looks up `scenarios` by `simulation_id` (`cb-sc-001-onboarding-handoff-vslice`)
2. Requires `is_active` and `current_published_version_id`
3. Loads `scenario_versions` row and requires `version == content.version`
4. On any failure → generic `MSG_SCENARIO_UNAVAILABLE` (safe UI; **no owner-specific diagnostic** such as “not published” vs “version mismatch”)

**Not verified in this review:**

- Whether CB-SC-001 exists in any owner local/staging Supabase project
- Whether it is published
- Whether content hash matches the approved artifact (resolver checks **version
  string only**, not `canonical_content_sha256`)

Disposable smoke seeds a published version for Docker only. Production was not
accessed.

---

### 4. SESSION-STATE CONTRACT — Option B COMPLIANT (with notes)

Exact V2 keys:

| Key | Role |
|-----|------|
| `cb_sc001_v2_attempt_id` | Authoritative handle for resume |
| `cb_sc001_v2_scenario_version_id` | Cosmetic/navigation metadata written at start; **not** used as DB authority on resume |
| `cb_sc001_v2_pending_idempotency_key` | One uncertain submission retry |
| `cb_sc001_v2_pending_option_id` | Paired with pending key |
| `cb_sc001_v2_ui_message` / `_kind` | Cosmetic messages |

Confirmed:

- No controller state, client, identity, content hash, or orchestration result
  stored in session helpers (`assert_option_b_session_state_compliant`)
- Keys prefixed `cb_sc001_v2_*` — no collision with V1 `ba201_*`
- Every authoritative load with `attempt_id` calls `resume_learner_scenario_v2`

**Truly Option B: YES**, for the frozen contract scope.

Notes:

- `scenario_version_id` is re-resolved from DB on each page/submit path; session
  copy is not trusted over DB for resume (resume uses attempt_id + identity).
- Pending keys are cleared on success and stale; retained only on
  `PersistenceUnavailableError`.

---

### 5. AUTHENTICATION AND ADMIN CLIENT — PASS (boundary)

- Page: `require_paid_access("Scenario Simulator V2")` then
  `get_current_user_email()` — no form/query/URL email path.
- `build_trusted_identity_v2` rejects empty email.
- `get_supabase_admin_client()` constructed per script run / submit; never written
  to `st.session_state`.
- UI messages use `log_and_get_user_message` (returns fixed user string; logs
  server-side).
- Cross-user attempt access relies on existing RPC/ownership contract in
  orchestration/port (not reimplemented in Streamlit layer) — consistent with
  Engine V2 design; not re-proven at page layer here.

---

### 6. START / RERUN DUPLICATION — MOSTLY PASS; RACE RESIDUAL

| Case | Behavior |
|------|----------|
| Normal Streamlit rerun with stored `attempt_id` | Resume only — no second start (unit-proven) |
| Start succeeds then `attempt_id` written | Same function body — write is immediate after start return |
| `attempt_id` present, `scenario_version_id` absent | Resume path ignores missing version id — OK |
| Stale / foreign / deleted attempt | Maps to attempt-not-found / ownership errors → safe unavailable message |
| Start succeeds but process dies before session write | **Residual:** next visit generates a **new** client UUID and starts again → orphan attempt possible |
| Two tabs both without `attempt_id` | **Residual:** two independent starts (MEDIUM concurrency gap) |

---

### 7. SUBMISSION SAFETY — PASS (designed correctly)

Confirmed in helpers + unit/disposable tests:

- Resume before submit
- Visible-option validation before orchestration
- One idempotency key retained only for uncertain retry
- Success clears pending metadata
- Stale conflict clears cosmetic state, resumes, **never auto-resubmits**
- Form submit + pending-retry UI reduce double-submit; DB idempotency is the
  hard guarantee for uncertain retry

Multi-tab concurrent submit of **different** options still depends on CAS/stale
handling (resume + stale path) — acceptable, but not explicitly integration-tested
for two tabs.

---

### 8. LEARNER OUTPUT SAFETY — MOSTLY PASS; MEDIUM LEAKS

Serialized learner blob (controller): no `attemptId`, no engine version, no
content hash — unit-proven.

Page rendering:

- Does not print attempt ID, scenario version UUID, content hash, RPC/SQL, or
  stack traces in learner-facing strings.
- **MEDIUM:** Streamlit widget keys embed `attempt_id`
  (`cb_sc001_v2_form_{attempt_id}`, radio/retry keys) — can appear in DOM.
- **MEDIUM:** `extract_progress_label` falls back to `Decision {expectedSequenceNumber}`
  when progress metadata missing — exposes sequence concept.
- **LOW:** Option IDs are radio values (labels via `format_func`); `sceneId`
  exists in serialized scene dict but is not rendered as page copy.
- Terminal `outcomeId` is in serialized terminal dict; page does not print it
  (renders title/narrative/score only).

---

### 9. NAVIGATION AND FEATURE FLAG — PASS

- Route `scenario_simulator_v2` is `NAV_GROUP_HIDDEN`, `requires_premium=True`,
  same feature flag as V1.
- Flag off → early `st.stop()` with unavailable info (before paid gate).
- Flag on + direct URL → still hits `require_paid_access` + email check.
- V1 route `scenario_simulator` / `pages/Scenario_Simulator.py` unchanged.
- No public primary-nav entry for V2.

Gap: feature-flag-off and direct-route premium denial are not covered by
focused page tests (exec harness always forces flag on).

---

### 10. TEST QUALITY — ADEQUATE HELPERS; GAPS REMAIN

Strengths: broad helper coverage (A–Y intent), Option B assertions, controller
regression gaps folded in, real disposable start/resume/retry/stale.

Gaps / over-reliance on fakes:

| Gap | Severity |
|-----|----------|
| Committed disposable does not assert full terminal | MEDIUM |
| Duplicate-start / multi-tab races | MEDIUM |
| Target scenario unpublished/missing UI path | LOW–MEDIUM (mapped, lightly tested) |
| Feature flag off | LOW |
| Identity change between reruns with leftover `attempt_id` | MEDIUM |
| Corrupted session values (non-UUID attempt_id) | LOW |
| Real Streamlit browser refresh / widget behavior | LOW (helper-level process-loss covers persistence resume) |
| Page exec harness stubs fetch; little real page submit coverage | MEDIUM |

---

### 11. REAL DISPOSABLE VERIFICATION — PARTIAL IN-REPO; FULL VIA OUT-OF-REPO PROBE

| Check | Result |
|-------|--------|
| Existing disposable Streamlit integration | PASSED (start, attempt_id-only resume, uncertain submit + retry, stale mapping, sanitization) — **not terminal** |
| Full terminal without source changes | **Possible** — out-of-repo probe `PASS_FULL_TERMINAL` |
| Claim full scenario readiness from committed tests alone | **No** |

---

### 12. READINESS (restated)

| Decision | Value |
|----------|-------|
| Milestone | **CORRECTIONS_REQUIRED** |
| Owner local testing | **NOT_READY_FOR_OWNER_LOCAL_TESTING** |
| Staging Supabase testing | **NOT READY** until content relocated + published version confirmed in staging |

---

## Detailed findings list

### BLOCKER-01 / HIGH-01 — Production loads tests/fixtures content

- **Where:** `utils/scenario_streamlit_v2.py` → `pages/Scenario_Simulator_V2.py`
- **Impact:** Application ships with a hard dependency on test tree; fails READY
  gates; packaging/deploy layouts that omit `tests/` break the page.
- **Correction:** Relocate approved CB-SC-001 to `scenario_content/` (+ catalog)
  or DB-published content path; update loader; keep fixture for tests only.

### MEDIUM-01 — Committed disposable integration incomplete vs full scenario

- Stops after first advancement; report text implied stronger terminal proof.
- **Correction:** Extend disposable Streamlit integration to happy-path terminal
  + terminal resume + blocked resubmit (probe already shows it is feasible).

### MEDIUM-02 — Duplicate attempt races (pre-store crash / multi-tab start)

- New UUID generated client-side whenever session lacks `attempt_id`.
- **Correction:** Document owner limitation; consider server-side
  start-or-resume-by-active-attempt if product requires single active attempt.

### MEDIUM-03 — Attempt ID in Streamlit widget keys

- Violates spirit of “attempt ID never appears in learner-facing surfaces.”
- **Correction:** Use stable non-secret widget keys (e.g. fixed page keys +
  scene index from learner-safe progress label).

### MEDIUM-04 — Sequence number in progress fallback

- `extract_progress_label` → `Decision {expectedSequenceNumber}`.
- **Correction:** Prefer only approved `progressMetadata`; else generic
  “In progress”.

### MEDIUM-05 — Version resolve ignores content hash

- Version string match only; drift possible if republished under same version.
- **Correction:** Compare published row hash to `content.canonical_content_sha256`
  when column available.

### MEDIUM-06 — Staging published CB-SC-001 unverified

- Owner must seed/publish before staging tests; current UI error is generic.
- **Correction:** Owner ops checklist + optional safer owner-facing diagnostic
  in non-prod.

### MEDIUM-07 — Test / page harness gaps

- Flag-off, identity swap, concurrent tabs, page-level submit not covered.
- **Correction:** Add focused tests before owner/staging claims.

### LOW-01 — Option IDs as radio values

- Labels shown; IDs may exist in DOM. Acceptable if intentional; document.

### LOW-02 — `sceneId` / `outcomeId` in serialized dict

- Not printed by page; still present in in-memory structure.

### LOW-03 — Missing-file exception embeds fixture path

- User message sanitized; server logs may include path via `logger.exception`.

---

## Engine V1 isolation

- V1 controller and V1 page not modified.
- Separate hidden route and `cb_sc001_v2_*` keys.
- Streamlit V2 helper does not import `utils.scenario_learner_controller`.

**PASS.**

---

## Required correction sequence

1. **Move CB-SC-001** out of `tests/fixtures` into approved `scenario_content/`
   (and catalog) or define DB-published load path; update
   `utils/scenario_streamlit_v2.py` + page imports accordingly.
2. **Seed/publish** matching version (+ hash) in the chosen non-production
   Supabase; verify `resolve_cb_sc001_scenario_version_id` succeeds.
3. **Extend committed disposable integration** to full terminal + terminal
   resume + blocked resubmit.
4. **Remove attempt_id from widget keys**; soften progress fallback.
5. Optionally harden content-hash check and multi-tab start semantics.
6. Add missing page/flag/identity tests.
7. Re-run review gates → then milestone commit → then owner local testing.

---

## Remaining risks

- Owner staging environment may lack published CB-SC-001 entirely.
- Orphan attempts under multi-tab first visit.
- DOM exposure of attempt UUID via widget keys until corrected.
- Packaging without `tests/` directory breaks current page.
- Implementation report currently overstates owner-testing readiness.

---

## Recommended next task

**SIM-STREAMLIT-V2-01-CORRECTION-01** — Relocate CB-SC-001 to production content
path, wire catalog/DB resolution, extend real disposable to terminal, and close
MEDIUM DOM/progress leaks. Re-review before any owner/staging testing claim.
