# SCENARIO_ENGINE_V2_NONPROD_PUBLICATION_REPORT

Task: SIM-STREAMLIT-V2-02
Model: Sonnet High
Baseline HEAD: `76e0cdd` — "Complete Engine V2 Streamlit BA scenario slice"

## 1. Purpose

Publish the exact committed CB-SC-001 scenario into one explicitly verified
non-production data store, validate its published identity through the same
read paths the Streamlit runtime uses, and document owner-walkthrough
prerequisites. No Git staging/commit/push and no Render deployment occurred.

## 2. Canonical identity published

| Field | Value |
|---|---|
| Simulation ID | `cb-sc-001-onboarding-handoff-vslice` |
| Semantic version | `0.2.1-vslice-engine-v2` |
| Canonical SHA-256 | `c74d61c42dbb2b0c34e6b84f722815d42b4fa4fe6e3aabebce18d46d6b4db551` |
| Canonical content file | `scenario_content/business_analyst/cb_sc_001_onboarding_handoff_v1_1_0.json` |

## 3. Target identification

The repository's configured Streamlit target (`.streamlit/secrets.toml`) contains
only placeholder, non-functional values:

```
SUPABASE_URL = "https://qa-local-placeholder.supabase.co"
SUPABASE_ANON_KEY = "qa-local-placeholder-anon-key"
SUPABASE_SERVICE_ROLE_KEY = "qa-local-placeholder-service-role-key"
```

No real `SUPABASE_URL` / service-role credential exists in the local environment
or in repository configuration. This configured target cannot be used for a
real publication and is **not** the live CertBound project by construction —
it is a literal placeholder hostname that has never resolved to a real
Supabase project.

Because no real staging/non-production project credential was supplied,
publication was performed against a **disposable, ephemeral local Docker
Postgres + PostgREST stack**, using the exact pattern already proven in
`tests/test_scenario_supabase_port_v2.py::TestSupabasePortDisposablePostgrestSmoke`
and `tests/test_scenario_streamlit_v2.py::TestScenarioStreamlitV2DisposableIntegration`.

### Non-production evidence (independent signals)

1. **Loopback-only host** — `127.0.0.1`, no routable/public hostname.
2. **Dedicated, uniquely-named containers/network** created solely for this
   run (`certbound-v2-nonprod-pub-pg`, `certbound-v2-nonprod-pub-postgrest`,
   `certbound-v2-nonprod-pub-net`), distinct from any persistent stack.
3. **No remote Supabase project link** — stack was started via bare
   `docker run` of `postgres:16` and `postgrest/postgrest:v14.16`; at no
   point was any remote Supabase API/URL contacted.
4. **Ephemeral by construction** — container lifetime is scoped to this one
   script execution; both containers and the network were torn down at the
   end of the run (verified below).
5. **Configured application target is a known non-functional placeholder**
   (`qa-local-placeholder.supabase.co`), ruling out any accidental production
   access via the app's own configuration.

All five signals independently indicate non-production. No production
Render environment, no production Supabase project, and no data used by
paying learners was accessed at any point.

## 4. Pre-flight

```
Write-Output "shell-ok"
git status --short --branch
git log -1 --oneline
```

Result:

```
shell-ok
## main...origin/main [ahead 24]
?? .local/
?? local_only/
?? scripts/v58_run_combined_policy_evaluation.py
?? structural_audit_state.json
?? tests/test_combined_policy_evaluator.py
?? v68_corrected_review_bundle/
?? v68_final_review_bundle/
?? v68_review_bundle/
?? workers/combined_policy_evaluator.py
76e0cdd Complete Engine V2 Streamlit BA scenario slice
```

Branch `main`, HEAD `76e0cdd` confirmed. Nothing staged. Only protected/unrelated
untracked paths present (all listed above are explicitly protected paths or
pre-existing unrelated untracked artifacts).

## 5. Focused test baseline

```
python -m pytest tests/test_scenario_streamlit_v2.py tests/test_scenario_controller_v2.py tests/test_scenario_supabase_port_v2.py tests/test_scenario_catalog.py -q -rs
```

Result: **189 passed, 4 skipped, 39 subtests passed**. Skips are the four
previously documented inherited disposable/port-smoke skips (no new skips, no
failures, no errors).

## 6. Publication tooling selected

**Approved existing repository publication path** — no ad hoc SQL script was
introduced into the repository. The runner used:

- `utils.scenario_streamlit_v2.load_cb_sc001_v2_content()` — canonical content
  load + hash computation (existing production code).
- `utils.scenario_streamlit_v2.diagnose_cb_sc001_publication_readiness()` —
  existing read-only diagnostic (pre- and post-publication).
- `utils.scenario_streamlit_v2.resolve_cb_sc001_scenario_version_id()` —
  existing runtime resolver (same path Streamlit uses).
- `public.publish_scenario_version_v1(...)` — existing repository-owned
  Postgres RPC (already used by `tests/test_scenario_supabase_port_v2.py` and
  `tests/test_scenario_orchestration_v2.py`) that sets `lifecycle_status`,
  stores the canonical content JSON + hash, and sets
  `scenarios.current_published_version_id` for the given version — no new
  RPC, table, or schema object was created.
- The four already-committed migrations
  (`supabase/migrations/*.sql`, including the V69 migration required for
  Engine V2 identity) were applied verbatim and unmodified to the disposable
  Postgres container, exactly as the existing disposable test suites already
  do. No migration file was authored, edited, or run against any persistent
  target.

A small orchestration script (outside the repository, under the OS temp
directory) sequenced these existing calls; it contains **no new SQL/schema
statements** beyond the two `INSERT`s (`scenarios`, `scenario_versions`) that
mirror the exact pattern already committed in
`tests/test_scenario_supabase_port_v2.py::_seed_scenario_fixture`, followed by
the existing `publish_scenario_version_v1` RPC call.

## 7. Read-only existing-state inspection (pre-publication)

Before any write, on the freshly-provisioned disposable stack:

| Check | Result |
|---|---|
| CB-SC-001 scenario records | **0** (no record — case A) |
| CB-SC-001 version records | **0** |
| `diagnose_cb_sc001_publication_readiness(...)` | `ready = false`, finding = `scenario_catalog_entry_missing` |
| Duplicate/ambiguous records | None |
| Conflicting semantic version with different hash | None |

Case **A — no CB-SC-001 record** confirmed. No STOP condition triggered (no
duplicates, no hash conflicts, no destructive overwrite required).

## 8. Dry-run / preview

| Field | Value |
|---|---|
| Target environment | `disposable_local_docker` |
| Simulation ID | `cb-sc-001-onboarding-handoff-vslice` |
| Semantic version | `0.2.1-vslice-engine-v2` |
| Canonical SHA-256 | `c74d61c42dbb2b0c34e6b84f722815d42b4fa4fe6e3aabebce18d46d6b4db551` |
| Scenario record action | `create` |
| Version record action | `create` |
| Publication-pointer action | `set` |
| Records expected to change | **3** (1 scenario, 1 version, 1 pointer update as part of version creation) |

Preview scope: exactly one CB-SC-001 scenario record and one exact
scenario-version record. No other scenario, no learner attempt, no learner
decision, and no schema object appeared in the preview.

## 9. Controlled publication

Executed only after the dry-run confirmed an unambiguous, non-destructive
`create`/`create`/`set` action set:

1. Inserted one row into `public.scenarios` (`simulation_id =
   cb-sc-001-onboarding-handoff-vslice`, `is_active = true`).
2. Inserted one row into `public.scenario_versions` with
   `source_repository_path =
   scenario_content/business_analyst/cb_sc_001_onboarding_handoff_v1_1_0.json`.
3. Called `public.publish_scenario_version_v1(version_id, canonical_json,
   canonical_sha256)`, which set `lifecycle_status = 'published'` and
   `scenarios.current_published_version_id` to the new version.

No other scenario, catalog record, learner attempt, or learner decision was
touched. No schema, RLS, grant, function, trigger, index, or RPC was created,
altered, or dropped — only pre-existing rows in pre-existing tables were
inserted via a pre-existing RPC.

## 10. Post-publication verification

| Check | Result |
|---|---|
| `diagnose_cb_sc001_publication_readiness(...).ready` | **true** |
| Findings | `[]` (empty) |
| Scenario active | **true** |
| Published semantic version | `0.2.1-vslice-engine-v2` (exact match) |
| Published canonical SHA-256 | `c74d61c42dbb2b0c34e6b84f722815d42b4fa4fe6e3aabebce18d46d6b4db551` (exact match) |
| `resolve_cb_sc001_scenario_version_id(...)` | returns the same published version id |
| Runtime resolver matches diagnostic | **true** |

### Before / after counts

| Metric | Before | After |
|---|---:|---:|
| CB-SC-001 scenario records | 0 | 1 |
| CB-SC-001 version records | 0 | 1 |
| Learner attempts (test owner identity) | 0 | 0 |
| Learner decisions (test owner identity) | 0 | 0 |

No learner attempt or decision was created by publication or by either
readiness-diagnostic call, confirming the diagnostic is genuinely read-only.

## 11. Cleanup

Both disposable containers and the disposable Docker network were removed at
the end of the run (`docker ps -a` / `docker network ls` filtered on the
run's container-name prefix returned no results afterward). The disposable
stack does not persist between sessions.

## 12. Owner walkthrough prerequisites

The disposable stack used for this publication is **ephemeral** — it was
torn down immediately after verification, per the read-only/no-persistent-write
posture of this task. It cannot be used for a live owner walkthrough because
it no longer exists after this task ends.

For the owner to actually perform the manual walkthrough, the owner must:

1. Provide (or create) a **real, explicitly non-production** Supabase project
   (local `supabase start` stack, a dedicated staging project, or an
   equivalent long-lived non-prod project) and replace the placeholder values
   in `.streamlit/secrets.toml` (`SUPABASE_URL`, `SUPABASE_ANON_KEY`,
   `SUPABASE_SERVICE_ROLE_KEY`) with that target's real values. **This report
   does not contain and will not contain those values.**
2. Re-run the same publication steps documented in this report (Phases 2–6)
   against that persistent target — the exact same read-only diagnostic,
   dry-run preview, and controlled-publication steps used here apply
   unchanged to any other non-production target.
3. Set the local environment variable `CERTBOUND_ENABLE_SCENARIO_SIMULATOR=1`
   so the `pages/Scenario_Simulator_V2.py` route is reachable.
4. Ensure the local test/owner account used for sign-in has premium access
   (`require_paid_access` gate on the V2 page) on that same non-production
   target.
5. Launch the app locally (no repository-documented Streamlit launch script
   was found; this is the standard entrypoint consistent with the
   `.streamlit/secrets.toml` `APP_BASE_URL = "http://localhost:8511"`
   convention already committed in this repository):

   ```
   streamlit run app.py --server.port 8511
   ```

6. No Render deployment is required or should be performed for this
   walkthrough.

## 13. Owner walkthrough checklist

To be performed manually by the owner (not automated in this task):

1. Sign in with a non-production premium learner account.
2. Open Scenario Simulator V2.
3. Confirm CB-SC-001 loads.
4. Confirm no technical identifier is displayed.
5. Select a response and submit.
6. Confirm the next scene loads.
7. Refresh the page.
8. Confirm the attempt resumes.
9. Continue through all decisions.
10. Reach terminal outcome.
11. Refresh again.
12. Confirm terminal outcome resumes.
13. Confirm no additional submit controls appear.
14. Confirm no raw database or RPC error appears.
15. Confirm "Return to Practice" clears the scenario session safely.

## 14. Files modified

**None.** No application source, test, or configuration file was created or
modified in this task other than this report. Publication was performed
entirely against a disposable, external (non-repository) Docker stack.

## 15. Protected paths

Not inspected, opened, searched, executed, modified, staged, or referenced:
`.local/`, `local_only/`, `scripts/v58_run_combined_policy_evaluation.py`,
`structural_audit_state.json`, `tests/test_combined_policy_evaluator.py`,
`workers/combined_policy_evaluator.py`, `v68_corrected_review_bundle/`,
`v68_final_review_bundle/`, `v68_review_bundle/`.

## 16. Owner-testing readiness decision

**PUBLICATION_LOGIC_VERIFIED_NONPROD — OWNER_ACTION_REQUIRED_FOR_LIVE_WALKTHROUGH**

The publication procedure, canonical identity, and read-only diagnostics are
fully verified end-to-end against a proven non-production target. A live,
persistent owner walkthrough still requires the owner to supply real
non-production Supabase credentials (Section 12) since the disposable stack
used here does not persist beyond this task.

## 17. Recommended next task

`SIM-STREAMLIT-V2-03` — once the owner supplies a persistent non-production
Supabase target's credentials (local `supabase start` stack or a dedicated
staging project), re-run Phases 2–6 of this same procedure against that
persistent target, then execute the manual owner walkthrough checklist above
and record results.
