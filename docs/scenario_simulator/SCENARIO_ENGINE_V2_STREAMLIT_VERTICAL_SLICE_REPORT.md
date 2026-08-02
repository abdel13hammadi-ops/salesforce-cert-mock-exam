# SIM-STREAMLIT-V2-01 — Engine V2 CB-SC-001 Streamlit Vertical Slice

Implementation report for one isolated learner-facing Streamlit vertical slice
using Engine V2 and the frozen Option B session-state contract.

**Correction status:** SIM-STREAMLIT-V2-01-CORRECTION-01 applied. See
`SCENARIO_ENGINE_V2_STREAMLIT_CORRECTION_REPORT.md` for closed review findings.

## 1. Scope

- Repository: `C:\Users\Abdel\Projects\salesforce-cert-mock-exam-latest`
- Branch: `main`
- Baseline HEAD: `7776b61` — Complete Engine V2 learner controller
- No commits in this task

### Files

| Path | Role |
|------|------|
| `scenario_content/business_analyst/cb_sc_001_onboarding_handoff_v1_1_0.json` | Canonical production CB-SC-001 asset |
| `scenario_content/business_analyst/catalog.json` | Catalog entry for CB-SC-001 |
| `utils/scenario_streamlit_v2.py` | Option B session + publication validation helpers |
| `pages/Scenario_Simulator_V2.py` | Isolated Streamlit page |
| `utils/navigation.py` | Hidden route `scenario_simulator_v2` |
| `tests/test_scenario_streamlit_v2.py` | Focused + full-terminal disposable tests |
| `tests/test_scenario_controller_v2.py` | Controller review regression gaps |
| `tests/test_scenario_catalog.py` | Catalog expectation update for CB-SC-001 |

Engine V1 page/controller untouched. No SQL/migration changes.

## 2. Canonical content identity

| Field | Value |
|-------|-------|
| Path | `scenario_content/business_analyst/cb_sc_001_onboarding_handoff_v1_1_0.json` |
| Simulation ID | `cb-sc-001-onboarding-handoff-vslice` |
| Version | `0.2.1-vslice-engine-v2` |
| Canonical SHA-256 | `c74d61c42dbb2b0c34e6b84f722815d42b4fa4fe6e3aabebce18d46d6b4db551` |

Runtime never loads from `tests/fixtures`.

## 3. Architecture

```
pages/Scenario_Simulator_V2.py
        │
        ▼
utils/scenario_streamlit_v2.py   # content load, diagnose/resolve, Option B
        │
        ▼
utils/scenario_controller_v2.py
        │
        ▼
orchestration + Supabase port
```

## 4. Authentication

- `require_paid_access` + `get_current_user_email` (server-side only)
- `get_supabase_admin_client` injected per call; never in session state
- Feature flag `CERTBOUND_ENABLE_SCENARIO_SIMULATOR`

## 5. Option B session keys

`cb_sc001_v2_attempt_id`, `cb_sc001_v2_scenario_version_id` (non-authoritative),
pending idempotency/option, UI message keys.

Every rerun with `attempt_id` calls `resume_learner_scenario_v2`.
Published version + content hash re-verified via
`resolve_cb_sc001_scenario_version_id` / `diagnose_cb_sc001_publication_readiness`.

## 6. Widget keys / progress

Stable keys: `cb_sc001_v2_widget_{form,choice,retry,return}` — no attempt UUID.
Progress: approved label or `"Scenario in progress"` — never expected sequence.

## 7. Full terminal disposable

Committed test
`test_real_streamlit_helper_full_terminal_flow` reaches actual terminal,
resumes completed attempt from `attempt_id` only, rejects terminal resubmit
pre-persistence, asserts decision count = 4, sanitizes learner output.

## 8. Owner-testing readiness

**CONDITIONALLY_READY_FOR_OWNER_LOCAL_TESTING** after:

1. Non-production Supabase target configured
2. CB-SC-001 published with matching version + hash
3. `diagnose_cb_sc001_publication_readiness(...).ready is True`
4. Feature flag enabled + premium auth

Remaining non-blocking risk: first-open multi-tab may create orphan attempts.

## 9. Recommended next task

Owner local walkthrough against staging/non-prod after publication seed, then
local milestone commit of the corrected slice.
