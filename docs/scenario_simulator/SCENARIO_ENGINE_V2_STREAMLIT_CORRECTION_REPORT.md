# SIM-STREAMLIT-V2-01-CORRECTION-01 — Streamlit Vertical Slice Corrections

Closes findings from `SCENARIO_ENGINE_V2_STREAMLIT_FOCUSED_REVIEW.md`.

No SQL, migrations, schema, RLS, grants, RPCs, or Engine V1 changes.
Nothing staged, committed, pushed, or deployed. Production was not accessed.

## 1. Canonical production content

| Field | Value |
|-------|-------|
| Path | `scenario_content/business_analyst/cb_sc_001_onboarding_handoff_v1_1_0.json` |
| Catalog entry | `scenario_content/business_analyst/catalog.json` (simulationId `cb-sc-001-onboarding-handoff-vslice`) |
| Simulation ID | `cb-sc-001-onboarding-handoff-vslice` |
| Semantic version | `0.2.1-vslice-engine-v2` |
| Schema version | `1.1.0` |
| Canonical SHA-256 | `c74d61c42dbb2b0c34e6b84f722815d42b4fa4fe6e3aabebce18d46d6b4db551` |
| Ownership | Application runtime asset under `scenario_content/`; packaged with the app |

Runtime loader: `utils.scenario_streamlit_v2.load_cb_sc001_v2_content()` →
`build_scenario_content_v2()` against the canonical path only.

**Production modules never construct a path under `tests/`.**

The historical engineering fixture
`tests/fixtures/scenario_engine_v2_vslice_1_1_0.json` remains for out-of-scope
Engine V2 unit suites and is byte-identical to the canonical asset at correction
time. Streamlit production/runtime tests load only the canonical path.

## 2. Database publication identity checks

`diagnose_cb_sc001_publication_readiness(client, ...)` verifies without starting
an attempt:

- scenario row exists for simulation ID
- scenario is active
- `current_published_version_id` present
- version row lifecycle is published (when present)
- semantic version matches canonical content
- `canonical_content_sha256` matches exactly

`resolve_cb_sc001_scenario_version_id` fails closed with the learner-safe
`MSG_SCENARIO_UNAVAILABLE` on any learner-blocking finding.

Owner findings (codes such as `canonical_content_hash_mismatch`) are returned
only on the diagnostic result object — never rendered to learners.

Optional `supabase_url` heuristic flags `target_appears_production` for owners;
that finding does not block the learner resolve path.

## 3. Session-state contract (Option B)

| Key | Role |
|-----|------|
| `cb_sc001_v2_attempt_id` | Authoritative resume handle |
| `cb_sc001_v2_scenario_version_id` | Non-authoritative metadata only; overwritten each load from freshly resolved DB id |
| pending idempotency/option | One uncertain retry |
| UI message keys | Cosmetic |

Tampered/missing session `scenario_version_id` cannot select content: every
fetch/submit receives a freshly resolved, hash-verified version id and syncs
session metadata to that value. Resume authorizes via attempt ownership in
persistence, not via session version id.

Identity-change / attempt-not-found clears all V2 session keys
(`clear_v2_session_keys`).

## 4. Widget-key strategy

Stable page-local keys only:

- `cb_sc001_v2_widget_form`
- `cb_sc001_v2_widget_choice`
- `cb_sc001_v2_widget_retry`
- `cb_sc001_v2_widget_return`

No attempt UUID and no hash of the attempt id.

## 5. Progress-display strategy

- Prefer approved `progressMetadata.progressLabel` / `label`
- Else `"Scenario in progress"`
- Complete → `"Scenario complete"`
- Never `Decision {expectedSequenceNumber}`

## 6. Feature-flag / auth / direct route

- Flag off → early stop; no admin client; no start/resume
- Flag on → still requires `require_paid_access` + server email
- Direct URL is the same page module; cannot bypass the flag

## 7. Identity-change behavior

Attempt owned by learner A cannot resume or submit under learner B.
Session attempt id is cleared; no decision is persisted; no attempt details leak
into remaining session state.

## 8. Concurrency behavior

| Case | Behavior |
|------|----------|
| Same option after other tab advanced | Pre-persistence rejection (option no longer visible) |
| Different options / CAS conflict | Stale-session message; no auto-retry; pending cleared |
| Two tabs open before `attempt_id` stored | **Remaining non-blocking risk:** two orphan starts possible (no DB locking/schema change in this task) |

Duplicate decisions cannot advance twice under CAS/idempotency; conflicting
submissions fail safely.

## 9. Full terminal disposable integration

`TestScenarioStreamlitV2DisposableIntegration::test_real_streamlit_helper_full_terminal_flow`

Verified against real disposable Postgres + PostgREST v14.16:

1. start → store attempt_id
2. discard in-memory view
3. resume from attempt_id
4. uncertain submit + idempotent retry
5. stale conflict without auto-resubmit
6. all happy-path decisions through **actual terminal**
7. terminal resume from attempt_id only
8. terminal resubmit rejected before persistence
9. decision row count == 4 (no duplicates)
10. learner blob sanitization + Option B compliance
11. container/network cleanup via base-class teardown

Independent run: **1 passed**.

## 10. Owner local-testing prerequisites

1. Enable `CERTBOUND_ENABLE_SCENARIO_SIMULATOR=1`
2. Point the app at an **explicit non-production** Supabase project
3. Publish CB-SC-001 with matching simulation id, version
   `0.2.1-vslice-engine-v2`, and hash
   `c74d61c42dbb2b0c34e6b84f722815d42b4fa4fe6e3aabebce18d46d6b4db551`
4. Run `diagnose_cb_sc001_publication_readiness(client, supabase_url=...)`
   until `ready is True`
5. Authenticate as a premium learner and open Scenario Simulator V2

## 11. Test results (correction suite)

```
python -m pytest tests/test_scenario_streamlit_v2.py tests/test_scenario_controller_v2.py \
  tests/test_scenario_supabase_port_v2.py tests/test_scenario_orchestration_v2.py \
  tests/test_scenario_persistence_v2.py tests/test_scenario_engine_v2.py \
  tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py \
  tests/test_scenario_catalog.py -q -rs
```

- **713 passed, 4 skipped, 48 subtests passed**, exit code 0
- Full-terminal disposable (independent): **1 passed**

## 12. Remaining risks

- First-open multi-tab race can still create orphan attempts (documented; no
  schema change in this task)
- Historical `tests/fixtures/...` copy may drift from canonical if edited
  independently (Streamlit runtime does not use it)
- Staging/owner target must be published before live testing
