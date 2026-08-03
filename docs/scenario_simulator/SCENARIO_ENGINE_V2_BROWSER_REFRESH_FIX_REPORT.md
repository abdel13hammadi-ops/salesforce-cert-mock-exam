# SCENARIO ENGINE V2 — Browser Refresh + Terminal Refresh Fix Report

**Tasks:**
- SIM-STREAMLIT-V2-CB-SC-001-BROWSER-REFRESH-FIX-01
- SIM-STREAMLIT-V2-CB-SC-001-TERMINAL-REFRESH-FIX-02

**HEAD baseline:** `9cb1b96` (unchanged; work remains uncommitted)
**Date:** 2026-08-03

---

## A. Browser refresh false-stale (FIX-01)

### A.1 Reproduced symptoms (owner browser test)

1. Learner opened Scenario Simulator V2 against CertBound Staging and saw an active CB-SC-001 scene.
2. Browser refresh rendered:
   - heading: **Scenario unavailable**
   - message: **Your scenario session changed. Reloading the latest progress.**
   - button: **Return to Practice**
3. Clicking **Return to Practice** opened a blank page.

### A.2 Root cause

Frozen Option B stores only `cb_sc001_v2_attempt_id` (plus non-authoritative cosmetic keys). A full browser refresh can drop Streamlit session state.

On a load with **no** session `attempt_id`, `fetch_authoritative_cb_sc001_view` previously minted a **new** UUID and called `start_or_resume` with that id. V69 `start_or_resume_scenario_attempt_v1` then raised `attempt_id_conflict` against the learner’s existing `in_progress` row.

That RPC prefix maps to `ScenarioOrchestrationV2IdentityMismatchError` → `ScenarioControllerV2StaleSessionError` → UI text `MSG_STALE_SESSION`, which the page rendered inside the **Scenario unavailable** empty state.

`_render_unavailable` used `render_empty_state(..., action_href="pages/Practice.py")`, which emits a raw HTML `<a href="pages/Practice.py">`. With Streamlit multipage sidebar navigation disabled, that href is not a valid Streamlit route and yields a blank page.

### A.3 FIX-01 summary

| Area | Change |
|------|--------|
| `utils/scenario_orchestration_v2.py` | When caller omits `attempt_id`, mint for create-safe envelope binding; on `attempt_id_conflict`, perform **one** recovery RPC with `p_attempt_id=NULL` (V69 resume-existing), then reload/replay. |
| `utils/scenario_streamlit_v2.py` | Start path uses `attempt_id=None` (no client mint that fights an existing in-progress row). Adds `prepare_return_to_practice_navigation` / registered Practice path helpers. |
| `pages/Scenario_Simulator_V2.py` | Return to Practice uses `st.switch_page` after clearing only V2 keys. Load-path `StaleSessionError` no longer surfaces the submit-only stale banner as the unavailable body. |

---

## B. Terminal refresh starts a new attempt (FIX-02)

### B.1 Terminal-refresh symptom (owner browser test)

1. The terminal result displayed after completing CB-SC-001.
2. The browser was refreshed.
3. The application returned to the beginning of the scenario.
4. A new attempt was implicitly started.

### B.2 Exact root cause

V69 `start_or_resume_scenario_attempt_v1` resumes **only** `in_progress` attempts. Completed attempts are never resumed by that RPC.

After FIX-01, session-loss with no `attempt_id` called `start_or_resume` with `attempt_id=None`. That path:

1. found no in-progress row;
2. created a **new** attempt (because completed rows are invisible to start-or-resume resume logic).

So completed + full session loss incorrectly behaved like a first-ever create.

### B.3 Completed-attempt lookup behavior

No schema migration or new RPC was required. Service-role already has `SELECT` on `scenario_attempts` (V68).

Added repository-owned port method:

`list_learner_attempt_summaries_v2(user_email, scenario_version_id)`

- server-side PostgREST select via approved service-role client;
- filters by trusted learner email + scenario version id;
- returns only minimum fields: `id`, `status`, `started_at`, `completed_at`;
- mapped to `LearnerAttemptSummaryV2` (no raw DB row escapes to Streamlit).

### B.4 Active-versus-completed selection rules

`resolve_authoritative_attempt_ref_v2`:

| Priority | Condition | Action |
|----------|-----------|--------|
| A | Explicit trusted `attempt_id` in session | Resume that exact owned attempt (active → scene; completed → terminal). Fail closed on missing/foreign. |
| B | No id; exactly one `in_progress` | Resume that attempt. |
| C | No id; more than one `in_progress` | Fail closed (`multiple_in_progress` → learner-safe unavailable). |
| D | No id; no in-progress; completed exist | Resume **most recent completed** by `(completed_at DESC, started_at DESC, attempt_id DESC)`. |
| E | No id; no prior attempt | Create first attempt via `start_or_resume` with `attempt_id=None`. |
| F | Explicit **Start New Attempt** | Create exactly one new attempt (see below). Never on refresh/render. |

Abandoned attempts are never selected.

### B.5 Explicit Start New Attempt design

- Button appears **only** on the completed terminal view (primary), beside **Return to Practice**.
- Click path: `start_new_cb_sc001_attempt_v2`.
- If an in-progress attempt already exists → resume it (no second create).
- Else store `cb_sc001_v2_pending_new_attempt_id` once, call `start_or_resume` with that caller UUID (existing RPC supports explicit create id).
- Rerun/double-click: same pending id → resume that attempt if created, or reuse pending id for create — not a second UUID mint.
- Clear cosmetic widget state only after conclusive creation/resume.
- **Return to Practice** remains a separate action: clears only V2 keys + `st.switch_page("pages/Practice.py")`; creates no attempt.

### B.6 Multiple-attempt semantics

| History | No-attempt-ID open | Start New Attempt |
|---------|--------------------|-------------------|
| One completed, no in-progress | Resume that completed (terminal) | Create one new in-progress |
| Completed + one in-progress | Resume in-progress | Resume in-progress (no duplicate) |
| Multiple completed | Resume latest completed (deterministic) | Create one new in-progress; history preserved |
| Unexpected multiple in-progress | Fail closed (unavailable) | Fail closed |

### B.7 Session-loss behavior

| Prior state | After full browser refresh / session loss |
|-------------|-------------------------------------------|
| Active in-progress | Resume same attempt + same scene/progress; no new attempt; no decision |
| Completed (latest) | Resume terminal; no new attempt; no decision; no submit controls |
| No prior attempt | Create exactly one first attempt |
| Explicit Start New Attempt after completed | One new in-progress; completed history unchanged |

---

## C. Option B compliance

Approved V2 session keys remain non-authoritative cosmetics + trusted attempt id markers only:

- `cb_sc001_v2_attempt_id`
- `cb_sc001_v2_scenario_version_id` (non-authoritative mirror)
- `cb_sc001_v2_pending_retry`
- `cb_sc001_v2_pending_option_id`
- `cb_sc001_v2_ui_message` / `cb_sc001_v2_ui_message_kind`
- `cb_sc001_v2_pending_new_attempt_id` (explicit new-attempt idempotency only)
- widget keys for radio / submit / return / start-new

Forbidden in session: controller objects, DB rows, auth identities, clients, authoritative engine state.

---

## D. Files changed (FIX-01 + FIX-02)

**Modified:**

- `pages/Scenario_Simulator_V2.py`
- `utils/scenario_streamlit_v2.py`
- `utils/scenario_orchestration_v2.py`
- `utils/scenario_supabase_port_v2.py` (FIX-02 lookup)
- `tests/test_scenario_streamlit_v2.py`
- `tests/test_scenario_orchestration_v2.py`

**Created / updated:**

- `docs/scenario_simulator/SCENARIO_ENGINE_V2_BROWSER_REFRESH_FIX_REPORT.md` (this file)

---

## E. Tests added

### FIX-01 — `TestBrowserRefreshAndReturnToPractice`

Active refresh resume; no false stale; no duplicate attempt/decision; stale banner cleared; real stale submit; terminal with retained id; Return to Practice navigation.

### FIX-02 — `TestTerminalRefreshAndNewAttempt`

1. Completed + full session loss resumes terminal
2. Completed refresh creates no new attempt / no decision
3. No-attempt-ID + in-progress resumes active
4. No-attempt-ID + completed-only resumes latest completed
5. Multiple completed resolves deterministically
6. Explicit Start New Attempt creates one attempt
7. Double invoke / rerun does not create two
8. Previous completed preserved
9. Existing in-progress prevents duplicate new attempt
10. Unexpected multiple in-progress fails closed
11. Terminal shows Start New Attempt widget key; Return to Practice separate
12. Ownership / Option B restrictions remain covered by suite

---

## F. Exact test results (FIX-02 suite run)

```
python -m pytest `
  tests/test_scenario_streamlit_v2.py `
  tests/test_scenario_controller_v2.py `
  tests/test_scenario_supabase_port_v2.py `
  tests/test_scenario_orchestration_v2.py `
  tests/test_scenario_persistence_v2.py `
  tests/test_scenario_engine_v2.py `
  tests/test_scenario_persistence.py `
  tests/test_scenario_learner_controller.py `
  tests/test_scenario_catalog.py `
  -q -rs
```

**Result:** `731 passed, 4 skipped, 48 subtests passed` — failures=0, errors=0.

---

## G. Remaining risks

- First-open multi-tab races can still create orphan attempts (pre-existing unique-index races).
- Staging may retain historical integration and owner-test attempts (do not delete).
- Lookup uses service-role table SELECT (sole approved exception; mutations remain RPC-only).
- Non-blocking review notes remain (overloaded multiple-in-progress exception type; no dedicated port parser unit tests; orphan session attempt_id recovers on next request).

---

## H. Owner retest and review (final)

**Owner retest date:** 2026-08-03
**Owner retest result:** PASS
**Independent Grok 4.5 review:** PASS (0 BLOCKER, 0 HIGH, 0 MEDIUM; no required corrections)

| Check | Result |
|-------|--------|
| Active-scene refresh | Resumed authoritative in-progress attempt |
| Terminal refresh | Remained terminal |
| Automatic new attempt on refresh | None |
| Explicit Start New Attempt | Worked; created one attempt only |
| Active new-attempt refresh | Resumed correctly |
| Navigate away and return | Resumed correctly |
| Return to Practice | Opened registered Practice page |
| Blank page | None |

**Final owner acceptance:** PASS
**Readiness for local milestone commit:** READY

---

## I. Recommended next task

1. Create the scoped local milestone commit (this task).
2. Keep Git push and Render deployment held.
3. Any future push/deploy requires a separate explicitly authorized release task.
