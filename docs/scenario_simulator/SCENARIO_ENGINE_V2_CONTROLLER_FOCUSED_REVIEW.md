# SIM-CONTROLLER-V2-01-REVIEW-01 — Engine V2 Learner Controller Focused Review

Review-only. No source, test, SQL, migration, or UI file was modified. No
database or production connection was made. Nothing was staged, committed,
pushed, or deployed.

## 1. Scope and pre-flight

- Repository: `C:\Users\Abdel\Projects\salesforce-cert-mock-exam-latest`
- Branch: `main`
- HEAD: `959647e` — "Complete Engine V2 Supabase persistence port"
- Starting `git status --short --branch`: `## main...origin/main [ahead 22]`,
  with only protected/unrelated untracked paths (`.local/`, `local_only/`,
  `scripts/v58_run_combined_policy_evaluation.py`,
  `structural_audit_state.json`, `tests/test_combined_policy_evaluator.py`,
  `workers/combined_policy_evaluator.py`, the three `v68_*_bundle/`
  directories) plus exactly the four expected new controller-task files:
  `utils/scenario_controller_v2.py`, `tests/test_scenario_controller_v2.py`,
  and `docs/scenario_simulator/SCENARIO_ENGINE_V2_CONTROLLER_IMPLEMENTATION_REPORT.md`.
- Ending `git status --short --branch`: identical, plus this review file.
- No protected path was opened, searched, or referenced.

### Required test command result

```
python -m pytest tests/test_scenario_controller_v2.py tests/test_scenario_supabase_port_v2.py \
  tests/test_scenario_orchestration_v2.py tests/test_scenario_persistence_v2.py \
  tests/test_scenario_engine_v2.py tests/test_scenario_persistence.py \
  tests/test_scenario_learner_controller.py -v -rs
```

- **Passed: 652**
- **Skipped: 2**
- **Subtests passed: 45**
- Exit code 0, no failures or errors.
- The real, Docker-backed `TestScenarioControllerV2DisposablePostgrestSmoke::test_real_controller_start_submit_resume_retry_and_stale_conflict`
  **PASSED** (Docker was available in this environment for this run), confirming
  the real-PostgREST controller integration path is exercised, not merely
  skipped.

**Exact skip reasons** (both from `tests/test_scenario_controller_v2.py`,
inside `TestScenarioControllerV2DisposablePostgrestSmoke`):

```
SKIPPED [1] tests\test_scenario_controller_v2.py:775: covered by TestSupabasePortDisposablePostgrestSmoke; this subclass exercises the controller instead
SKIPPED [1] tests\test_scenario_controller_v2.py:780: covered by TestSupabasePortDisposablePostgrestSmoke; this subclass exercises the controller instead
```

These are `self.skipTest(...)` calls on the two *inherited* test methods
(`test_real_postgrest_start_submit_resume_idempotency_and_conflict`,
`test_real_postgrest_unknown_function_error_is_sanitized`) from
`TestSupabasePortDisposablePostgrestSmoke`, deliberately overridden in the
controller's subclass. See §14.

## 2. Public API (§1)

`start_or_resume_learner_scenario_v2`, `resume_learner_scenario_v2`,
`submit_learner_scenario_choice_v2`, `serialize_learner_controller_result_v2`
each have one clear responsibility. Confirmed by reading
`utils/scenario_controller_v2.py` in full:

- No replay, RPC-parameter construction, CAS comparison, or canonical-decision
  loading logic is duplicated in this module — every one of those
  responsibilities is delegated to `start_or_resume_scenario_run_v2`,
  `resume_and_replay_scenario_run_v2`, and `submit_scenario_decision_v2`
  (the only orchestration internals imported directly,
  `_build_submission_context`/`_build_learner_view`, are used only inside
  `resume_learner_scenario_v2` to rebuild the exact same typed shapes the
  orchestration layer itself already produces for start/submit — not to
  reimplement replay or CAS logic).
- `ScenarioRunV2Snapshot`, `TrustedAttemptSnapshotV2`, raw RPC parameter
  dicts, and the Supabase port object never appear in any public return
  value — only `LearnerScenarioControllerResultV2` /
  `LearnerScenarioControllerStateV2` (both containing the internal
  `ScenarioOrchestrationSubmissionContextV2`, itself explicitly documented as
  server-side-only) or the plain `dict` from
  `serialize_learner_controller_result_v2`.
- The API shape (one call each for start/resume/submit, one call to
  serialize) is exactly what a Streamlit page needs: call once per rerun,
  keep the returned `state`, serialize once for rendering.

**Result: SAFE / SUITABLE.**

## 3. Trusted identity (§2)

- `LearnerIdentityContextV2` **must** be explicitly constructed; every entry
  point calls `_require_identity`, which raises
  `ScenarioControllerV2UnauthenticatedError` unless `isinstance(identity,
  LearnerIdentityContextV2)` — a raw string, dict, or list is structurally
  impossible to use (confirmed by `test_c_browser_supplied_email_not_accepted_as_identity`
  and independent probe 1, §18).
- Email normalization is deterministic and reuses the same,
  unmodified `utils.scenario_persistence.normalize_scenario_persistence_email`
  used by every V1/V2 RPC path (`lower(btrim(...))` + `"@"` check).
- Identity consistency across start/resume/submit: `start`/`resume` bind
  `verified_identity.user_email` into `LearnerScenarioControllerStateV2.user_email`;
  `submit_learner_scenario_choice_v2` explicitly compares
  `verified_identity.user_email != state.user_email` and fails closed with
  `ScenarioControllerV2InvalidIdentityError` on any mismatch — confirmed by
  `test_identity_mismatch_on_submit_fails_closed` and independent probe 2.
  `resume_learner_scenario_v2` re-derives identity fresh from
  `resume_and_replay_scenario_run_v2`'s own trusted persisted row rather than
  trusting a caller belief, so a caller cannot resume someone else's attempt
  by supplying a different identity with a known attempt id (the underlying
  `attempt_not_found:`/identity-mismatch RPC contract still applies).
- No service-role token, bearer string, or client object is ever copied out
  of `identity.supabase_client` into `LearnerScenarioControllerStateV2` or
  `LearnerScenarioControllerResultV2` — only the client *reference* is used,
  once, to build a fresh `SupabaseScenarioOrchestrationV2Port` per call
  (confirmed by `test_ac_supabase_client_or_token_not_serialized` and
  independent probe 9 showing the client is never even reachable from the
  learner-safe output).
- The controller never treats `user_email` alone as authorization — it is
  forwarded to orchestration/RPCs exactly as the already-reviewed
  Supabase port (`SIM-PERSIST-V2-06`/`-06B`) requires; row ownership remains
  enforced server-side by the RPCs/service-role trust boundary, unchanged by
  this task.

**Result: SAFE.**

## 4. Controller state (§3) — classification

`LearnerScenarioControllerStateV2` fields:

| Field | Nature | Learner-visible? |
|---|---|---|
| `user_email` | Authoritative (identity-consistency check) | No — server-only |
| `attempt_id` | Authoritative (trusted attempt handle) | No — server-only, see §5 |
| `is_complete` | Authoritative flag, also reconstructable via resume | Indirectly — its value informs the approved `isComplete` output key, but the field itself is never serialized |
| `submission_context` | Authoritative CAS/replay material (full `ScenarioOrchestrationSubmissionContextV2`, including the entire `ScenarioRunV2Snapshot`); reconstructable via `resume_learner_scenario_v2` from persistence alone | No — server-only, `None` once complete |
| `learner_view` | Cache of the already-computed learner-safe projection | Its *content* is what `serialize_learner_controller_result_v2` exposes; the object itself is server-only |

**Independent serializability probes** (run against a live Python object,
outside the repository — see §18):

```
probe9_state_picklable: false
probe9_pickle_error: "TypeError: cannot pickle 'mappingproxy' object"
probe9_state_json_serializable: false
probe9_json_error: "TypeError: Object of type ScenarioOrchestrationSubmissionContextV2 is not JSON serializable"
```

`ScenarioContentV2` and `ScenarioRunV2Snapshot` (reached through
`submission_context.run` and `content`) use `types.MappingProxyType` for
`document`, `state`, and related nested maps. `MappingProxyType` is neither
`pickle`-able nor JSON-serializable by design (confirmed directly with a
bare `MappingProxyType` in this Python environment). This means
`LearnerScenarioControllerStateV2` **cannot** be written to a database
column, a cache, a cookie, a distributed session store, or restored across a
process restart today.

**Classification: `IN_PROCESS_ONLY` / `STREAMLIT_SESSION_SAFE` for a
single-worker deployment only. NOT `CROSS_PROCESS_SERIALIZABLE`.**

More precisely:

- The object is a frozen, non-mutating, no-alias-leaking Python value —
  safe to store as a live reference in `st.session_state` and reuse across
  reruns **within the same browser session, served by the same Streamlit
  worker process** (this is what `st.session_state` actually requires: it
  holds live objects in server memory per WebSocket session, and does not
  itself pickle/serialize them).
- It does **not** survive: a hard browser refresh (Streamlit allocates a new
  `session_state` for a new WebSocket connection unless the page separately
  persists something durable, e.g. a URL query parameter), a server process
  restart, or horizontal scaling across multiple worker processes/replicas
  without sticky sessions.
- **Independent probe 8** (process-loss simulation) proves the correct
  mitigation already works end-to-end today: discarding the entire in-memory
  `LearnerScenarioControllerResultV2`/`state` object and retaining **only**
  the plain `attempt_id` string, then calling `resume_learner_scenario_v2`
  with that string alone, fully and correctly reconstructs the next
  server-side state (`probe8_resume_from_attempt_id_only_succeeds: true`,
  correct `expected_sequence_number` recovered). This is the safety net the
  session-persistence recommendation below relies on.

## 5. Session persistence decision (§4)

**Recommendation: Option B** — store only the opaque `attempt_id` (plus any
purely cosmetic/non-authoritative display metadata the page wants, e.g. a
scenario title) in `st.session_state`, and call `resume_learner_scenario_v2`
on every rerun where the full `LearnerScenarioControllerStateV2` is not
already present as a live object from the immediately preceding call in the
same session.

Rationale:

- Option A (store the full controller-state object in `st.session_state`
  only, reload from the database after refresh/process loss) is unsafe as a
  *sole* strategy: §4 proves the object cannot itself be reloaded from
  anywhere after a refresh/restart — it must always be paired with a
  database-backed reload path anyway, so Option A degrades to Option B the
  moment persistence is lost, while additionally holding a much larger
  object (full engine snapshot + cached envelope) in server memory for the
  session's lifetime for no additional durability benefit.
- Option C (build a JSON-serializable envelope before UI integration) is
  unnecessary extra surface for the *first* vertical slice: the orchestration
  layer already treats the persisted database row as the sole durable source
  of truth and re-verifies it by replay on every resume (never trusts a
  cached envelope) — inventing a second, controller-owned serialization
  format would duplicate that authority and create a second thing to keep in
  sync with schema/engine changes, for a benefit (surviving a mid-flight
  in-memory-only submission before the DB write not yet observed) that does
  not apply here, since every submission that reaches the RPC is already
  durably persisted or safely idempotently retryable.
- Option B requires no new code in this module (the capability is already
  proven, §4/§18) and matches the existing Engine V1 controller's own
  documented pattern of treating `attempt_id` as an opaque server-side
  session handle (`utils/scenario_learner_controller.py`, line 153/558/610
  area) rather than caching full engine state client-side.

This is a recommendation only; no implementation change was made in this
review.

## 6. Attempt-ID exposure (§5)

- `attempt_id` is never included in `serialize_learner_controller_result_v2`'s
  output (`test_attempt_id_not_exposed_in_serialized_output`, independent
  probe 7: neither the raw value nor an `"attemptId"` key appears in the
  active or terminal serialized JSON).
- `attempt_id` is not read from, or written into, any query-parameter- or
  form-shaped structure anywhere in this module — the only place it appears
  is `LearnerScenarioControllerStateV2.attempt_id`, which the module
  docstring explicitly documents as staying in trusted server-side state.
- Given the Option B recommendation (§5), a future Streamlit page **will**
  need `attempt_id` in `st.session_state` (as a plain Python string held
  server-side) to call `resume_learner_scenario_v2` after a rerun/refresh —
  this is exactly the "trusted server-side session state" use the module
  docstring already anticipates, not a new client exposure. It must not be
  round-tripped through the browser (URL, cookie, hidden form field) without
  a deliberate follow-up threat-model decision, which this task correctly
  defers.

**Result: SAFE as implemented; deferred future decision correctly flagged.**

## 7. Start flow (§6)

- `_require_identity` and `_require_nonempty_str(scenario_version_id)` both
  run **before** `_build_port`/`start_or_resume_scenario_run_v2` is ever
  invoked.
- `test_e_start_calls_orchestration_once` proves exactly one call
  (`len(self.persistence.start_calls) == 1`).
- `test_f_start_uses_trusted_identity_email` proves the RPC parameter is the
  identity's own normalized email, not any other value.
- Active start (`test_g`) and terminal-attempt start/resume
  (`test_h_start_returns_terminal_learner_safe_result_where_applicable`, which
  drives the fixture to completion then calls `start_or_resume_learner_scenario_v2`
  again on the now-complete attempt id) both behave correctly:
  `submission_context` is forced to `None` locally whenever
  `result.run.is_complete`, independent of whatever the orchestration layer
  itself returned for that field, so a completed attempt can never carry a
  submittable context by construction.
- No hidden state leaks: `LearnerScenarioControllerStateV2` is built from
  exactly `result.attempt_id`, `result.run.is_complete`, the
  locally-nulled-or-passed-through `submission_context`, and
  `result.learner_view` — nothing else from `StartOrResumeScenarioRunResultV2`
  (e.g. `created`) is retained.

**Result: SAFE.**

## 8. Resume flow (§7)

- `trusted_attempt_id = _require_nonempty_str(attempt_id)` runs before any
  port/orchestration call; `None`/`""`/whitespace-only all raise
  `ScenarioControllerV2InvalidRequestError` (`test_i`).
- Delegates entirely to `resume_and_replay_scenario_run_v2`, which performs
  the canonical decision reload + authoritative replay (already reviewed and
  confirmed under `SIM-PERSIST-V2-05*`) — `test_j_resume_calls_canonical_replay_path`
  confirms exactly one `load_attempt_snapshot` call and that the recomputed
  `expected_sequence_number` matches the original start result.
- Identity mismatch (`test_k`, via `persistence.identity_override`) fails
  closed as `ScenarioControllerV2StaleSessionError`, with none of
  `_SENSITIVE_SUBSTRINGS` (RPC prefixes, `psycopg2`, `Traceback`,
  `postgresql://`, `service_role`, JWT-shaped text) present in the message.
- Malformed/corrupted persisted state (`test_x_corrupted_replay_maps_safely`,
  via `persistence.cache_corrupt_for`) fails closed as
  `ScenarioControllerV2CorruptedAttemptError` — the controller never attempts
  a repair or silent overwrite (it has no write path to the cached envelope
  at all).
- Determinism: independent probe 8 additionally proves resuming from a bare
  `attempt_id` (with all other in-memory objects discarded) reproduces the
  exact same `expected_sequence_number` the live object held.

**Result: SAFE.**

## 9. Submit flow (§8)

- `option_id = _require_nonempty_str(selected_option_id)` runs before any
  port/orchestration call, so `None`/`""`/`"   "`/non-`str` values never
  reach persistence (`test_m`).
- An option id that is syntactically valid but not present in the current
  scene's `visible_option_ids` is rejected by
  `submit_scenario_decision_v2` itself (a genuinely unknown/hidden option is
  an orchestration-level `invalid_selected_option_id`-class rejection,
  proven with zero calls reaching persistence in
  `test_n_unknown_option_rejected_before_persistence_call`, which explicitly
  asserts `len(self.persistence.submit_calls) == 0`).
- Idempotency: `idempotency_key=None` (first submission) yields a
  controller-visible UUIDv4 in `result.last_idempotency_key`
  (`test_o`); an explicit retry with that exact value returns the identical
  key back (`test_p`), and never mints a new one automatically. A malformed
  key (non-UUID, UUIDv1/v5-shaped, non-`str`) is rejected by
  `_validate_idempotency_key` before the call
  (`test_malformed_idempotency_key_rejected`).
- Stale/CAS failures (`test_t_stale_sequence_maps_to_stable_stale_session_error`,
  reusing an already-consumed `state` for a second live submission) map to
  `ScenarioControllerV2StaleSessionError` and are **not** automatically
  retried anywhere in this module — the caller must call
  `resume_learner_scenario_v2` again, per the module's own docstring.
- A completed attempt (`state.is_complete or state.submission_context is
  None`) is rejected **before** `_build_port`/orchestration is even reached
  (`test_terminal_attempt_rejected_before_persistence_call`, asserting zero
  submit calls) — `ScenarioControllerV2TerminalAttemptError`.
- Successful submission returns the next learner-safe scene
  (`test_r`, asserting `expectedSequenceNumber == 2`) or the terminal result
  (`test_s`).

**Result: SAFE.**

## 10. Error mapping (§9)

`_map_orchestration_error` is a single, closed `if/elif` chain over every
`ScenarioOrchestrationV2Error` subtype imported by this module, ending in an
unconditional `ScenarioControllerV2UnexpectedInternalError` fallback — no
`ScenarioOrchestrationV2Error` subtype can fall through unmapped, and
`_run_controller_step`'s own final `except Exception` guarantees no *other*
raw exception (including ones raised inside this controller module itself,
e.g. an `AttributeError` from unexpected data shape) can escape unmapped
either. Cross-checked against the exact
`_RPC_ERROR_PREFIX_MAP` in `utils/scenario_orchestration_v2.py` (30 prefixes,
7 target exception types):

| Orchestration exception | Controller mapping | Unit-tested? |
|---|---|---|
| `ScenarioOrchestrationV2InvalidRequestError` (generic invalid_* prefixes) | `ScenarioControllerV2InvalidRequestError` | **No dedicated RPC-level test** (see §17) — exercised only via controller-side pre-validation (`test_m`/`test_n`/malformed-key tests), not via an injected orchestration-level `invalid_*:` RPC response |
| ...`attempt_not_found:` | `ScenarioControllerV2AttemptNotFoundError` | Yes (`test_z`) |
| ...`scenario_version_not_found:` / `scenario_version_not_published:` | `ScenarioControllerV2ScenarioUnavailableError` | **No** (§17 finding) |
| `ScenarioOrchestrationV2StaleRunError` (`attempt_not_in_progress:` / "already complete") | `ScenarioControllerV2TerminalAttemptError` | Not directly at the RPC-prefix level (covered indirectly by the local pre-check in `test_terminal_attempt_rejected_before_persistence_call`, which never reaches this branch) — **gap** |
| `ScenarioOrchestrationV2StaleRunError` (other) | `ScenarioControllerV2StaleSessionError` | Indirectly via `test_w` (resume) |
| `ScenarioOrchestrationV2SequenceConflictError` | `ScenarioControllerV2StaleSessionError` | Yes (`test_t`, `test_y`) |
| `ScenarioOrchestrationV2SceneConflictError` | `ScenarioControllerV2DecisionConflictError` | Yes (`test_u`) |
| `ScenarioOrchestrationV2IdempotencyConflictError` | `ScenarioControllerV2DecisionConflictError` | Yes (`test_v`) |
| `ScenarioOrchestrationV2IdentityMismatchError` | `ScenarioControllerV2StaleSessionError` | Yes (`test_k`, via `identity_override`) |
| `ScenarioOrchestrationV2CanonicalDecisionSequenceError` | `ScenarioControllerV2CorruptedAttemptError` | Not directly isolated from `ScenarioOrchestrationV2ReplayMismatchError` (`test_x` triggers replay/cache corruption, likely landing on the mismatch branch, not this one specifically) — **gap** |
| `ScenarioOrchestrationV2ReplayMismatchError` / `ScenarioOrchestrationV2TerminalMismatchError` | `ScenarioControllerV2CorruptedAttemptError` | Yes (`test_x`, replay path) |
| `ScenarioOrchestrationV2MalformedPersistenceResponseError` | `ScenarioControllerV2PersistenceUnavailableError` | Not directly isolated from the generic dependency-error branch below — **gap**, low risk (identical target/message) |
| `ScenarioOrchestrationV2PersistenceDependencyError` (unknown RPC/transport failures) | `ScenarioControllerV2PersistenceUnavailableError` | Yes (`test_w` x2) |
| Anything else (`Exception`) | `ScenarioControllerV2UnexpectedInternalError` | Yes (`test_unexpected_internal_failure_wrapped_and_sanitized`) |

No mapping produces a raw public exception. Every branch's target is one of
the 11 required stable controller error types. The two genuinely-missing
direct-RPC-prefix tests (`scenario_version_not_found`/`not_published`, and
isolating `CanonicalDecisionSequenceError` from `ReplayMismatchError`) are
**test-quality gaps, not implementation defects** — the mapping code itself
is simple, explicit, 1:1, and was independently confirmed correct by manual
inspection above. See §17/§discretionary corrections.

**Result: CLOSED for implementation; two untested-but-simple mapping
branches noted as MEDIUM test-quality gaps (not blockers).**

## 11. Error message safety (§10)

`_SENSITIVE_SUBSTRINGS` in the test file already probes for RPC prefixes,
`psycopg2`, `Traceback`, `postgresql://`, `service_role`, and JWT-like
(`eyJ`) text across the identity-mismatch and corrupted-replay paths.
Independent probe 6 (§18) additionally injected a raw `ValueError` from
`load_attempt_snapshot` containing a fabricated
`host=db.internal:5432 token=eyJhbGciOiJIUzI1NiJ9.secret` payload:

```
probe6_raw_exception_sanitized: true
probe6_exception_type: "ScenarioControllerV2PersistenceUnavailableError"
probe6_cause_preserved: true
```

None of the raw content, database URL, or JWT-like fragment appeared in the
raised exception's message; `__cause__` retained the original exception for
server-side logging. Every `ScenarioControllerV2*Error` message is one of
the 11 fixed module-level constants (`_UNAUTHENTICATED_MESSAGE`, ...,
`_UNEXPECTED_MESSAGE`) — the code contains no `str(exc)`-derived text in any
raised message, confirmed by direct reading of every `raise
ScenarioControllerV2*Error(...)` call site.

**Result: SAFE.**

## 12. Learner-safe serialization (§11)

Exact key sets, read directly from `serialize_learner_controller_result_v2`:

- **Active**: `isComplete` (`False`), `currentScene` (`sceneId`, `title`,
  `setting`, `dialogueExchanges`, `charactersPresent`, `learnerPresent`,
  `decisionPrompt`, `options` (`id`/`title`/`text` only per option),
  `progressMetadata`, `accessibility`, `mobilePresentation`),
  `expectedSequenceNumber`.
- **Terminal**: `isComplete` (`True`), `terminalResult` (`outcomeId`,
  `outcomeTitle`, `narrative`, `displayScore`). No `currentScene`, no
  `expectedSequenceNumber` (`test_h`, `test_s` both assert
  `assertNotIn`).

Confirmed absent from every code path in this module: attempt id (§6),
Supabase client/service-role credentials (§3/§11, `test_ac`), content hash
(`test_aa`, independent probe check), raw `state`/counters/flags/routing/
evaluation-tier/formula/debrief-seed fields (`test_ab`, using the exact
literal substrings the task specified), raw
`ScenarioOrchestrationLearnerViewV2`/`ScenarioRunV2Snapshot`/RPC/DB rows
(structurally impossible — `_serialize_scene_view`/the terminal branch only
ever read named, individually-picked fields off `LearnerSceneView` /
`LearnerTerminalView`, both already-reviewed learner-safe Engine V2 view
types).

**Result: SAFE — matches the approved field list exactly.**

## 13. Aliasing and immutability (§12)

- `test_ad_learner_output_mutation_cannot_change_controller_state` and
  independent probe 4 both mutate a serialized dict in place (appending a
  fake option, changing the title) and then re-serialize the *same*
  `result` object, proving the second call is unaffected — `_plain_json_value`
  rebuilds every nested `Mapping`/`list`/`tuple` fresh on every call, so no
  mutable structure is shared between calls or with `result.state.learner_view`.
- `test_ae_controller_state_is_frozen` / independent probe 3 confirm both
  field reassignment (`state.attempt_id = ...`) and whole-attribute
  reassignment (`result.state = ...`) raise `dataclasses.FrozenInstanceError`
  on the frozen dataclasses.
- `test_af_identity_context_is_not_mutated` / independent probe 10 confirm
  `LearnerIdentityContextV2.user_email`/`.supabase_client` are unchanged
  after being used in a real start call, and that direct field reassignment
  also raises `FrozenInstanceError`.
- Independent probe 9 confirms the Supabase client string reference used to
  build `LearnerIdentityContextV2` never appears in serialized output
  (already covered by `test_ac`, reconfirmed independently).
- `test_ag_orchestration_result_not_mutated_by_serialization` confirms
  repeated `serialize_learner_controller_result_v2(result)` calls are
  idempotent/pure (`before == after`, byte-for-byte).

Note: frozen dataclasses in Python prevent *field reassignment*, not
*mutation of a mutable field's own contents* — but every field this module
actually exposes through a public return value is itself either an
immutable scalar, a `MappingProxyType`/`tuple`-backed frozen Engine V2 value
object (already reviewed under prior tasks as immutable-by-construction), or
freshly rebuilt plain `dict`/`list` (`_plain_json_value`) with no shared
identity to anything internal — so there is no reachable in-place-mutation
vector even though `FrozenInstanceError` alone would not by itself prevent
one for an ordinary mutable field.

**Result: SAFE.**

## 14. Terminal flow (§13)

- `LearnerScenarioControllerStateV2.submission_context` is forced to `None`
  in all three entry points exactly when `run.is_complete` is `True` — never
  left populated by accident.
- `submit_learner_scenario_choice_v2` checks `state.is_complete or
  state.submission_context is None` and raises
  `ScenarioControllerV2TerminalAttemptError` **before** building a port or
  calling orchestration (`test_terminal_attempt_rejected_before_persistence_call`,
  zero submit calls asserted) — a completed attempt structurally cannot
  reach persistence a second time through this module.
- No `currentScene` is present in the terminal serialized shape (§12).
- Terminal learner output contains only the four approved fields
  (`outcomeId`, `outcomeTitle`, `narrative`, `displayScore`) — no internal
  trace, no state, no routing.
- Resuming a completed attempt is deterministic: `test_h` drives a fresh
  `start_or_resume_learner_scenario_v2` call against the now-complete
  attempt id and asserts `resumed.state.is_complete` and
  `resumed.state.submission_context is None` again.

**Result: SAFE.**

## 15. Skipped tests (§14)

Both skips are in `TestScenarioControllerV2DisposablePostgrestSmoke`
(`tests/test_scenario_controller_v2.py:775` and `:780`):

1. `test_real_postgrest_start_submit_resume_idempotency_and_conflict`
2. `test_real_postgrest_unknown_function_error_is_sanitized`

**Reason**: both are inherited, unmodified test methods from the base class
`TestSupabasePortDisposablePostgrestSmoke` (already-reviewed and passing
under `SIM-PERSIST-V2-06`/`-06B`). They reference `self.port`, which this
subclass's own `setUp` deliberately does not create (it creates
`self.identity` instead, to exercise the *controller* API rather than the
raw port). Rather than let them fail with an `AttributeError` for the wrong
reason, they are explicitly overridden to call `self.skipTest(...)` with a
reason string naming exactly why.

- **Not core-requirement gaps**: the exact scenarios those two methods cover
  (real start/submit/resume/idempotency/conflict at the port+RPC level, and
  real "unknown function" PostgREST error sanitization at the port level)
  are still executed and passing today, in `TestSupabasePortDisposablePostgrestSmoke`
  itself (part of the same required test command, confirmed passing above).
- **Not environmental**: this is a deliberate design/duplication-avoidance
  skip, not a Docker-unavailable skip (the class-level
  `@unittest.skipUnless(_docker_available(), ...)` decorator is the only
  thing that would produce an environmental skip, and it did **not** fire in
  this run — confirmed by `test_real_controller_start_submit_resume_retry_and_stale_conflict`
  actually running and passing in the same class, in the same test session).
- **Milestone readiness does not depend on these two skips**: the
  controller's own equivalent coverage
  (`test_real_controller_start_submit_resume_retry_and_stale_conflict`)
  independently exercises start, serialize, submit, resume, retry
  (idempotency), and one stale-conflict path through the real disposable
  PostgREST environment using the controller's own public API, and asserts
  the stale-conflict message contains none of `_SENSITIVE_SUBSTRINGS`.

**Classification: LOW. Deferred-by-design duplication-avoidance skip, not a
missing-coverage or environmental gap.**

## 16. Integration test (§15)

`TestScenarioControllerV2DisposablePostgrestSmoke` (subclass of the
already-reviewed `TestSupabasePortDisposablePostgrestSmoke`):

- Reuses the exact pinned PostgREST image and disposable Docker/Postgres
  bootstrap already validated under `SIM-PERSIST-V2-06B`.
- Uses a disposable-only identity: freshly minted
  `controller-smoke-<random hex>@example.com` email and a freshly minted
  `uuid.uuid4()` attempt id per test run — no fixed/reusable identity.
- Uses the real `SupabaseScenarioOrchestrationV2Port` (via a real
  `postgrest.SyncPostgrestClient` against the disposable container) and the
  **controller's own public functions** (`start_or_resume_learner_scenario_v2`,
  `serialize_learner_controller_result_v2`, `submit_learner_scenario_choice_v2`,
  `resume_learner_scenario_v2`) end-to-end — not the orchestration/port
  layer directly.
- Exercises, in one real test: start → serialize → submit → serialize →
  resume (equivalence check on `is_complete`/`expected_sequence_number`) →
  retry with the same idempotency key (equivalence check) → one stale
  conflict (asserting `ScenarioControllerV2StaleSessionError` and that no
  `_SENSITIVE_SUBSTRINGS` leak).
- This run (in this review session) confirmed the test actually executed
  against real Docker containers and passed — not skipped.
- Cleanup: inherited from `TestSupabasePortDisposablePostgrestSmoke`'s own
  `tearDownClass`/`finally`-based container/network teardown (already
  reviewed and confirmed under `SIM-PERSIST-V2-06-REVIEW-01`); this review
  did not need to re-verify container cleanup mechanics since the base class
  is unmodified.

**Result: PASSING, genuinely real-backed, cleanup already validated
upstream.**

## 17. Engine V1 isolation (§16)

- `git status`/`git diff --stat` confirm `utils/scenario_learner_controller.py`
  is untouched (not present in either pre-flight or post-review `git
  status` output).
- `test_aj_v1_controller_module_not_imported_or_modified` directly asserts
  `"scenario_learner_controller"` is not a name bound in
  `utils.scenario_controller_v2`'s own module globals, and
  `"scenario_controller_v2"` is not a name bound in
  `utils.scenario_learner_controller`'s own module globals — this is a
  stronger, executable check than a manual `import` grep.
- Direct read of `utils/scenario_controller_v2.py`'s imports (top of file)
  confirms it imports only from `utils.scenario_engine_v2`,
  `utils.scenario_orchestration_v2`, `utils.scenario_persistence` (only the
  already-shared `normalize_scenario_persistence_email`/
  `ScenarioPersistenceValidationError`, not any V1 controller symbol), and
  `utils.scenario_supabase_port_v2`.
- No shared Streamlit session-state key collision is possible today: neither
  controller module touches `st.session_state` directly (V1's own docstrings
  merely *describe* how a future page layer should use `st.session_state`
  around it; V2's controller does the same). Session-key naming is entirely
  a future page-layer concern for both, not decided by either controller
  module.
- `tests/test_scenario_learner_controller.py`'s full suite passed unchanged
  as part of the required combined test run (652 passed includes its own
  tests; no V1 test was modified in this task).

**Result: SAFE — isolation is complete and independently machine-checked.**

## 18. Test quality (§17) and independent probes

### Test-quality findings

- **MEDIUM-CTRL-01 (test gap)**: `_map_orchestration_error`'s
  `scenario_version_not_found:`/`scenario_version_not_published:` branch
  (target: `ScenarioControllerV2ScenarioUnavailableError`) has **no** unit
  test anywhere in `tests/test_scenario_controller_v2.py`. Confirmed by
  direct search — zero matches for `ScenarioUnavailable`,
  `scenario_version_not_found`, or `scenario_version_not_published` in the
  test file. The mapping logic itself was manually verified correct by
  reading the code (§10), but it is currently unverified by any executable
  test, and `FakeOrchestrationPersistence` has no injection point for a
  start-RPC-level error at all (only `submit_raise`/`load_raise` exist, no
  `start_raise`) — a start-flow orchestration error can currently only be
  tested via ad hoc subclassing (as already done for the
  `KeyboardInterrupt`/`SystemExit`/raw-`RuntimeError` tests).
- **LOW-CTRL-01 (test gap)**: `ScenarioOrchestrationV2CanonicalDecisionSequenceError`
  and `ScenarioOrchestrationV2MalformedPersistenceResponseError` are not
  individually isolated from their sibling branches
  (`ScenarioOrchestrationV2ReplayMismatchError`/`...TerminalMismatchError`,
  and `ScenarioOrchestrationV2PersistenceDependencyError`, respectively) by
  a dedicated test — both share the same target
  `ScenarioControllerV2*Error` as a sibling branch that *is* tested, so
  residual risk is low, but the specific branch is unverified.
- **LOW-CTRL-02 (test gap)**: no permanent regression test encodes the
  "resume from `attempt_id` alone after discarding the full in-memory
  state" scenario (independent probe 8, §4) — this is arguably the single
  most important behavior underpinning the Option B session-persistence
  recommendation (§5) and the eventual Streamlit refresh story, and it
  currently exists only as a one-off probe in this review, not as a
  committed test.
- **LOW-CTRL-03 (test gap)**: `serialize_learner_controller_result_v2`'s own
  input-type guard (`if not isinstance(result, LearnerScenarioControllerResultV2)`)
  has no dedicated test (e.g. passing `None`, a bare `state`, or a plain
  dict).
- **LOW-CTRL-04 (documentation/regression gap)**: no permanent test encodes
  the non-serializability finding in §4 (`pickle`/`json` both fail on
  `LearnerScenarioControllerStateV2`). Without a regression test, a future
  refactor could accidentally make the state serializable (fine) or could
  be assumed serializable by a future Streamlit integration without anyone
  re-verifying — a one-line regression test would lock in today's evidence
  either way.
- **No test mirrors implementation instead of behavior**: every test that
  matters (`test_e`/`test_q`/`test_j`/`test_n`/`test_terminal_attempt_rejected...`)
  asserts against `FakeOrchestrationPersistence`'s own call-count lists
  (`start_calls`/`submit_calls`/`load_calls`), which is state independent of
  the controller's implementation details — these are strong, not weak,
  assertions, and none of them could pass if orchestration were silently
  skipped.
- Substring-based assertions (`test_ab`, `_SENSITIVE_SUBSTRINGS` checks) are
  inherently a little coarse (a coincidental substring match in unrelated
  scenario content could theoretically produce a false failure), but this is
  a standard, low-risk pattern already used consistently across this
  codebase's other V2 test suites, and it did not produce any false result
  in this review's runs.

### Independent probes executed (temporary, outside the repository)

A single temporary script,
`%TEMP%\sim_controller_v2_review_probe.py` (`C:\Users\Abdel\AppData\Local\Temp\`),
imported the repository's `utils`/`tests` packages via `sys.path` insertion
only (no repository file was created or modified) and exercised:

1. Identity object rejection (raw email string, dict, int, `None`) — **all
   rejected** with `ScenarioControllerV2UnauthenticatedError`.
2. Identity swap across submit (different learner email against a state
   built under the original identity) — **rejected**.
3. Controller-state mutation attempt (`attempt_id`/`submission_context`
   reassignment) — **both raise `FrozenInstanceError`**.
4. Learner-output mutation (mutating a serialized dict, appending a fake
   option, changing the title) followed by re-serializing the same result —
   **fully isolated**, second serialization unaffected.
5. Terminal re-submit — **rejected** with
   `ScenarioControllerV2TerminalAttemptError`, with **zero** additional
   `submit_calls` recorded (confirms the pre-check runs before any
   persistence call, matching §14).
6. Raw persistence exception leakage (`ValueError` containing a fabricated
   host/port and JWT-like token) via a custom
   `load_attempt_snapshot` override — **sanitized**
   (`ScenarioControllerV2PersistenceUnavailableError`, generic message, no
   leaked substring), with `__cause__` preserved.
7. Attempt-ID exposure — **not present**, neither as raw value nor as an
   `"attemptId"` key, in either active or terminal serialized JSON.
8. Process-loss simulation: discard the entire in-memory
   `LearnerScenarioControllerResultV2` after one start + one submit,
   retaining only the plain `attempt_id` string, then call
   `resume_learner_scenario_v2` with that string alone — **fully succeeds**,
   correctly recovers `expected_sequence_number == 2` (the post-submit
   value). This is the concrete evidence behind §4/§5.
9. State/content picklability and JSON-serializability — **both fail**
   (`MappingProxyType` is neither picklable nor JSON-serializable), directly
   supporting the §4 classification.
10. Identity-context mutation after use in a real call — **unchanged**
    (`user_email`/`supabase_client` both identical before/after).

All results are quoted verbatim above in their relevant sections. The
temporary probe file was deleted immediately after use; no artifact remains
outside or inside the repository.

## Readiness standard

### Milestone readiness

**READY_FOR_LOCAL_MILESTONE_COMMIT**

- Blockers: **0**
- Unresolved HIGH findings: **0**
- New HIGH findings: **0**
- Learner output: safe (§12)
- Error mapping: closed for implementation, with 4 low/medium test-quality
  gaps noted (§10/§18) — none change the mapping's correctness, only its
  verification coverage
- Identity boundary: safe (§3)
- Skip reasons: acceptable, non-environmental, non-blocking (§15)
- Tests: 652 passed, 2 skipped (both acceptable), 45 subtests passed, 0
  failures
- Engine V1: isolated, independently machine-checked (§17)

### Streamlit readiness

**READY_WITH_SESSION_STATE_CONSTRAINT**

The controller API itself is ready to be called from a Streamlit page today.
The constraint is the one already identified and resolved by this review's
own recommendation (§4/§5): a future V2 page must adopt Option B (store only
`attempt_id` + cosmetic metadata in `st.session_state`, always resume/replay
the full controller state from persistence rather than assuming the
in-memory `LearnerScenarioControllerStateV2` object survives a browser
refresh, process restart, or multi-worker deployment). This is a design
constraint for the *next* task to follow, not a defect in the controller
delivered by this task.

## Required correction sequence (non-blocking; recommended before or during
the next Streamlit-integration task, not required to commit this milestone)

1. Add a `start_raise` injection point to `FakeOrchestrationPersistence` (or
   an equivalent ad hoc subclass, matching the pattern already used for
   `KeyboardInterrupt`/`SystemExit`/raw-`RuntimeError`), and add a direct
   unit test proving `scenario_version_not_found:`/`scenario_version_not_published:`
   RPC errors map to `ScenarioControllerV2ScenarioUnavailableError` (closes
   MEDIUM-CTRL-01).
2. Add one regression test locking in the §4/§18 finding: constructing a
   `LearnerScenarioControllerStateV2` (or its `submission_context`) is
   neither `pickle`-able nor JSON-serializable today — so a future change
   that silently breaks or silently "fixes" this is caught explicitly
   (closes LOW-CTRL-04).
3. Add one permanent regression test for the process-loss/resume-from-
   attempt-id-only scenario proven ad hoc in §18 probe 8, since it is the
   load-bearing evidence for the Option B recommendation (closes
   LOW-CTRL-02).
4. Optionally isolate `ScenarioOrchestrationV2CanonicalDecisionSequenceError`
   and `ScenarioOrchestrationV2MalformedPersistenceResponseError` from their
   currently-conflated sibling test paths, and add a direct
   `serialize_learner_controller_result_v2(<not a result>)` rejection test
   (closes LOW-CTRL-01/LOW-CTRL-03).

None of these four items block a local milestone commit; they are
recommended hardening for the controller test suite before or alongside the
first real Streamlit V2 page task.

## Recommended next task

Implement the Streamlit V2 learner page for one complete BA scenario
vertical slice, explicitly adopting the Option B session-persistence pattern
recommended in §5 (store `attempt_id` only in `st.session_state`; call
`resume_learner_scenario_v2` to rebuild full controller state on every
rerun where the live object is not already present in that same session).
Optionally fold in the four non-blocking test-quality corrections from the
"Required correction sequence" above as a small preparatory pass.
