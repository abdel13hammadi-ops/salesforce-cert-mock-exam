# SCENARIO_ENGINE_V2 Persistence Adapter — Correction Report

**Task ID:** SIM-PERSIST-V2-04B
**Model:** Sonnet High
**Baseline HEAD:** `6136673` — Complete Scenario Engine V2 vertical slice
**Corrects findings from:** `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_ADAPTER_FOCUSED_REVIEW.md` (SIM-PERSIST-V2-04-REVIEW-01)
**Scope:** Adapter code, focused tests, and implementation documentation only. No start/resume orchestration. No Supabase RPC calls. No database connection. No SQL/migration/RLS/policy/grant/UI changes. No Engine V1 behavior changes. Nothing staged, committed, pushed, or deployed.

---

## Disposition summary

| Metric | Before | After |
|---|---|---|
| Blockers | 2 | **0** |
| HIGH findings | 2 (same as blockers) | **0** |
| MEDIUM findings | 4 | **0** |
| LOW findings | 3 | **0** (all addressed opportunistically; none were release-blocking) |
| Envelope top-level key count | 19 | **17** |
| Focused test count | 447 | **471** (+24 net new persistence-v2 tests) |
| Focused test result | 447 passed | **471 passed, 9 subtests passed** |

**Readiness decision: BLOCKERS CLOSED.** All BLOCKER/HIGH findings from the focused review are corrected. The adapter's serialized envelope now matches the frozen SQL-compatible persistence contract exactly.

---

## Finding-by-finding disposition

### BLOCKER-01 — Envelope missing SQL-required `version` (used `scenarioVersion`) — **CLOSED**

- Renamed the envelope field from `scenarioVersion` to `version` in:
  - `serialize_run_snapshot_v2` (emits `version`)
  - `deserialize_run_snapshot_v2` (requires `version`; `scenarioVersion` is now rejected as an unexpected top-level key)
  - `_envelope_to_json_dict` (cache-comparison round trip)
  - `PersistedRunEnvelopeV2.version` (renamed dataclass attribute; was `scenario_version`)
  - `verify_persisted_attempt_identity_v2` (now compares trusted content identity against `envelope.version` via `pinned_version=envelope.version`)
- `_ENVELOPE_V1_KEYS` no longer contains `scenarioVersion`; contains `version`.
- No dual-key backward compatibility was introduced (the adapter has never been published or committed).
- Tests: `test_A_snapshot_serialization_succeeds`, `test_AH5_scenarioVersion_key_rejected`, `test_F_identity_mismatch_rejected` (now flips `version`), `test_AO2_start_rpc_uses_corrected_envelope_shape`, `test_AP3_submit_rpc_uses_corrected_envelope_shape`.

### BLOCKER-02 — Envelope missing SQL-required `isComplete` (used string `status`) — **CLOSED**

- Replaced the string `status` (`"in_progress"` / `"completed"`) field with a JSON-native boolean `isComplete` in:
  - `serialize_run_snapshot_v2` (emits `isComplete: run.is_complete`, with a defensive `isinstance(..., bool)` guard against a non-bool engine value)
  - `deserialize_run_snapshot_v2` (uses `_require_strict_bool` — an exact `bool`, never an `int`/`str` truthy value)
  - `_envelope_to_json_dict`
  - `PersistedRunEnvelopeV2.is_complete: bool` (renamed from `status: str`)
- Removed the now-unused `_ENGINE_RUN_STATUSES` constant (it existed only to validate the old string `status` field).
- Active-run / completed-run invariants are preserved exactly, now keyed off `isComplete`:
  - `isComplete = False` → `currentSceneId` must be non-null, `terminalResult` must be `null`.
  - `isComplete = True` → `currentSceneId` must be `null`, `terminalResult` must be present and valid.
- Tests: `test_AG_active_attempt_with_terminal_result_rejected`, `test_AH_completed_attempt_without_terminal_result_rejected`, `test_AG2_isComplete_true_on_active_run_rejected`, `test_AH2_isComplete_false_on_completed_run_rejected`, `test_AH3_string_isComplete_rejected` (rejects `"in_progress"`, `"completed"`, `"active"`, `"true"`, `""`), `test_AH4_int_isComplete_rejected` (rejects `0`/`1`/`2`/`-1` — bool/int confusion is explicitly rejected even though Python's `bool` is an `int` subclass at runtime), `test_AH6_status_key_rejected`.

---

## Additional corrections (from the review's MEDIUM/LOW findings; not blockers, but closed opportunistically since the task authorized touching this file for the blocker fix)

### Attempt identity removed from the envelope (LOW-01 + design §7 alignment)

- `attemptId` is no longer part of the envelope at all (`_ENVELOPE_V1_KEYS` excludes it; `PersistedRunEnvelopeV2` has no `attempt_id` attribute).
- `verify_persisted_attempt_identity_v2(...)`: still accepts `attempt_row_id` explicitly (canonicalized/validated via `_require_uuid_str`), but no longer compares it against anything inside the envelope — there is nothing there to compare against. Only trusted-vs-trusted content identity (`canonicalContentSha256`, `engineVersion`) and content-vs-pinned identity (via `verify_replay_identity_v2`) are checked.
- `replay_serialized_run_v2(...)`: now calls `replay_scenario_run_v2(content, attempt_id=trusted_attempt_id, ...)` using the **trusted, canonicalized `attempt_row_id` parameter**, not `envelope.attempt_id` (which no longer exists). This closes the actual identity-forgery vector: previously, the reconstructed run's identity was taken from the untrusted envelope; now it is taken exclusively from the caller-supplied trusted parameter.
- `build_start_or_resume_rpc_params_v2(...)` is unaffected in shape — `p_attempt_id` was already, and remains, a separate top-level RPC parameter derived from `run.attempt_id`, never from the envelope.
- Tests: `test_G_trusted_attempt_row_id_not_compared_against_envelope`, `test_G2_forged_attempt_id_in_raw_envelope_rejected` (a raw payload with an injected `attemptId` key is rejected as an unexpected field, before any identity logic runs), `test_G3_replay_identity_always_matches_trusted_attempt_row_id`, `test_R_uuid_canonicalized_at_serialize_boundaries` (mixed-case `attempt_row_id` is canonicalized to lowercase at the replay boundary).

### `decisionCount` removed from the envelope (LOW-01)

- Not part of the authoritative SQL/schema contract; no CAS logic in this adapter (or in the validated V68/V69 SQL) depends on it. Any caller needing a count uses `len(decisionHistory)` (cache) or the canonical `scenario_decisions` row count (authoritative) — never a separately-trusted envelope field.
- `deserialize_run_snapshot_v2` no longer cross-checks `decisionCount` against `len(decisionHistory)` (that check is now moot; the field doesn't exist).
- Test: `test_AH7_decisionCount_key_rejected`.

### MEDIUM-01 — RPC parser nested aliasing — **CLOSED**

- `_row_json_object_field` and `_row_nullable_json_object_field` (used by both `parse_start_or_resume_rpc_response_v2` and `parse_submit_decision_rpc_response_v2` for `serialized_engine_state` and `terminal_result_snapshot`) now return `copy.deepcopy(dict(value))` instead of a shallow `dict(value)`.
- This severs aliasing on nested dicts/lists (`state`, `counters`, `flags`, `optionDisplayOrderByScene`, etc.) in both directions: mutating the raw RPC response after parsing no longer changes the parsed result, and mutating a parsed result's mutable output no longer changes the raw response.
- Tests: `test_AT2_start_parser_deep_copy_isolation`, `test_AT3_submit_parser_deep_copy_isolation` (including the nullable `terminal_result_snapshot` case), `test_AT4_parsed_result_mutation_does_not_affect_raw_response`.

### MEDIUM-02 — `attemptId` UUID canonicalization on serialize — **MOOT (closed by removal)**

- Since `attemptId` no longer exists inside the envelope, there is nothing left to canonicalize there. UUID canonicalization is still exercised and verified at the two remaining trusted-attempt-identity boundaries: `build_start_or_resume_rpc_params_v2`'s `p_attempt_id` (pre-existing, unchanged) and `replay_serialized_run_v2`'s `attempt_row_id` parameter (new coverage — see `test_R_uuid_canonicalized_at_serialize_boundaries`).

### MEDIUM-03 — Public serializers leaked raw `AttributeError` — **CLOSED**

- Added `_wrap_serialization_boundary_errors`, a decorator that catches exactly `AttributeError` / `TypeError` / `KeyError` / `ValueError` / `IndexError` raised while reading a malformed input object's shape, and re-raises `ScenarioPersistenceV2SerializationError`. It:
  - Never catches this module's own `ScenarioPersistenceV2Error` subclasses (an already-domain validation failure — e.g. a `bool` sequence number — passes through completely unchanged).
  - Never catches `BaseException` subclasses (`SystemExit`, `KeyboardInterrupt`, `GeneratorExit`) — those exception types are not listed in the `except` clause, so Python never routes them there.
- Applied to all four object-shape-sensitive public serializers: `serialize_run_snapshot_v2`, `serialize_decision_input_v2`, `serialize_learner_scene_view_v2`, `serialize_learner_terminal_view_v2`.
- Tests: `test_AT5`–`test_AT9` (malformed-object and `None` inputs for all four wrapped functions, plus confirmation that valid inputs still serialize correctly), `test_AT10_wrapper_does_not_swallow_domain_errors` (a deliberate `ScenarioPersistenceV2ValidationError` from a `bool` sequence number is not re-wrapped), `test_AT11`/`test_AT12` (confirm `KeyboardInterrupt`/`SystemExit` raised from inside a wrapped function still propagate unchanged).

### MEDIUM-04 — Test suite encoded the non-authoritative field names — **CLOSED**

- All envelope-shape assertions were inverted to the frozen contract (`version`/`isComplete` present and correctly typed; `scenarioVersion`/`status`/`attemptId`/`decisionCount` absent).
- Added explicit "SQL seven-key compatibility" assertions on both the start-RPC and submit-RPC embedded envelopes (`test_AO2_start_rpc_uses_corrected_envelope_shape`, `test_AP3_submit_rpc_uses_corrected_envelope_shape`), and a full frozen-17-key-set assertion (`_FROZEN_ENVELOPE_KEYS`) reused across multiple tests.

### LOW-01 — Extra envelope keys vs Slice A — **CLOSED** (see `attemptId`/`decisionCount` removal above)

### LOW-02 — Docstring inaccuracy about `MappingProxyType` on deserialize — **CLOSED**

- Corrected the module docstring: `_require_json_object` deliberately thaws an incoming `MappingProxyType` (rather than rejecting it) so a caller re-feeding one of this module's own already-thawed structures never fails spuriously; only a genuinely non-`Mapping` value is rejected.

### LOW-03 — Unused `_CONTENT_HASH_PATTERN_SOURCE` constant — **CLOSED**

- Removed (dead code; no behavioral reference anywhere in the module).

---

## Frozen envelope shape (final, exact 17 keys)

```text
envelopeVersion
simulationId
version                          # NOT scenarioVersion
schemaVersion
engineVersion
canonicalContentSha256
currentSceneId
expectedSequenceNumber
isComplete                       # boolean; NOT status
state
counters
flags
decisionHistory                  # exactly {sequenceNumber, sceneId, optionId}[]
optionDisplayOrderByScene
selectedVariantIdByScene         # mandatory; may be {}
routingResolutions               # mandatory; may be []
terminalResult
```

Excluded (never present, always rejected as unexpected on deserialize): `attemptId`, `decisionCount`, `scenarioVersion`, `status`.

Verified programmatically (see "SQL-shape smoke result" below):

```python
>>> sorted(_ENVELOPE_V1_KEYS)
['canonicalContentSha256', 'counters', 'currentSceneId', 'decisionHistory',
 'engineVersion', 'envelopeVersion', 'expectedSequenceNumber', 'flags',
 'isComplete', 'optionDisplayOrderByScene', 'routingResolutions',
 'schemaVersion', 'selectedVariantIdByScene', 'simulationId', 'state',
 'terminalResult', 'version']
>>> len(_ENVELOPE_V1_KEYS)
17
```

---

## Files changed

### Modified

- **`utils/scenario_persistence_v2.py`**
  - Module docstring: rewrote "Snapshot envelope contract" section for the frozen 17-key shape; rewrote "Authoritative-data boundary" and "Domain errors" sections; corrected the `MappingProxyType` deserialize claim.
  - `_ENVELOPE_V1_KEYS`: 19 → 17 keys (`version`/`isComplete` in; `attemptId`/`decisionCount`/`scenarioVersion`/`status` out).
  - `_NON_TERMINAL_CACHE_KEYS`: replaced `status`/`decisionCount` with `isComplete`; no attempt-identity key (never was one, but the accompanying comment was corrected).
  - Removed unused `_ENGINE_RUN_STATUSES` and `_CONTENT_HASH_PATTERN_SOURCE` constants.
  - Added `_wrap_serialization_boundary_errors` decorator + `_F`/`Callable`/`TypeVar` typing imports, plus `copy`/`functools` imports.
  - `PersistedRunEnvelopeV2`: removed `attempt_id`, `decision_count`; renamed `scenario_version` → `version`, `status: str` → `is_complete: bool`.
  - `serialize_run_snapshot_v2`: decorated with the error-wrapping boundary; emits the corrected 17-key shape; added a defensive `isinstance(run.is_complete, bool)` guard.
  - `deserialize_run_snapshot_v2`: validates the corrected 17-key shape; `isComplete` via `_require_strict_bool`; removed `decisionCount` cross-check.
  - `_envelope_to_json_dict`: emits the corrected shape for cache comparison.
  - `serialize_decision_input_v2`, `serialize_learner_scene_view_v2`, `serialize_learner_terminal_view_v2`: decorated with the error-wrapping boundary.
  - `verify_persisted_attempt_identity_v2`: compares `pinned_version=envelope.version` (was `envelope.scenario_version`); no longer compares `attempt_row_id` against an envelope attempt-id copy (removed); docstring updated to explain the trusted-identity model.
  - `replay_serialized_run_v2`: canonicalizes `attempt_row_id` up front and drives `replay_scenario_run_v2(attempt_id=trusted_attempt_id, ...)` from it (was `envelope.attempt_id`).
  - `_row_json_object_field` / `_row_nullable_json_object_field`: deep-copy nested JSON on parse.
- **`tests/test_scenario_persistence_v2.py`**
  - Added `ScenarioPersistenceV2SerializationError` import; added the frozen `_FROZEN_ENVELOPE_KEYS` constant.
  - Rewrote envelope-shape assertions in `test_A`, `test_B`, `test_F`, `test_R` (renamed to `test_R_uuid_canonicalized_at_serialize_boundaries`), `test_AG`, `test_AH` for the frozen contract.
  - Replaced `test_G_attempt_id_mismatch_rejected` with `test_G_trusted_attempt_row_id_not_compared_against_envelope`, and added `test_G2_forged_attempt_id_in_raw_envelope_rejected`, `test_G3_replay_identity_always_matches_trusted_attempt_row_id`.
  - Added `test_AG2`–`test_AH9` (isComplete true/false-on-wrong-lifecycle rejection, string/int isComplete rejection, `scenarioVersion`/`status`/`decisionCount` key rejection, routingResolutions/selectedVariantIdByScene mandatory-presence checks).
  - Added `test_AO2_start_rpc_uses_corrected_envelope_shape`, `test_AP3_submit_rpc_uses_corrected_envelope_shape`.
  - Added `test_AT2`–`test_AT4` (RPC parser deep-copy isolation, both directions, both RPCs, including the nullable terminal-result-snapshot field).
  - Added a new `TestSerializerErrorWrapping` class (`test_AT5`–`test_AT12`): malformed-input wrapping for all four serializers, confirmation that valid inputs still work, confirmation that domain errors pass through unwrapped, and confirmation that `KeyboardInterrupt`/`SystemExit` are never caught.
  - Net effect: 60 → 84 tests in this file (+24); 447 → 471 in the combined focused suite.

### Created

- **`docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_ADAPTER_CORRECTION_REPORT.md`** (this file).

### Also updated (documentation only, per task's explicit file list)

- **`docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_ADAPTER_IMPLEMENTATION_REPORT.md`** — added a correction addendum pointing at this report and the corrected envelope shape (see that file for details).

---

## Test execution

```text
python -m pytest tests/test_scenario_persistence_v2.py \
  tests/test_scenario_engine_v2.py \
  tests/test_scenario_persistence.py \
  tests/test_scenario_learner_controller.py -q
```

**Result:** `471 passed, 9 subtests passed` (baseline was `447 passed`; net +24 tests, all in `tests/test_scenario_persistence_v2.py`; zero regressions in Engine V2 core, Engine V1 persistence, or the learner controller suites — those three files' own test counts are unchanged from baseline).

### SQL-shape smoke result (Python-only; no database connection)

Constructed the seven start-RPC parameters from a real `ScenarioRunV2Snapshot` built off the fixture content, and asserted on the resulting `p_initial_serialized_state`:

```python
params = build_start_or_resume_rpc_params_v2(run, user_email="learner@example.com", scenario_version_id="...")
envelope = params["p_initial_serialized_state"]
assert "version" in envelope        # PASS
assert "isComplete" in envelope     # PASS
assert "scenarioVersion" not in envelope  # PASS
assert "status" not in envelope           # PASS
assert "attemptId" not in envelope        # PASS
assert "decisionCount" not in envelope    # PASS
```

Output: `SQL-SHAPE SMOKE: PASS`. No Supabase client, RPC call, or database connection was used or created.

---

## Verification against acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Both blocker/high findings closed | **Yes** |
| 2 | Remaining blockers | **0** |
| 3 | Remaining HIGH findings | **0** |
| 4 | Envelope uses `version` | **Yes** |
| 5 | Envelope uses boolean `isComplete` | **Yes** |
| 6 | Envelope excludes `attemptId` | **Yes** |
| 7 | Envelope excludes `decisionCount` | **Yes** |
| 8 | Trusted attempt row ID is authoritative | **Yes** — `verify_persisted_attempt_identity_v2`/`replay_serialized_run_v2` take `attempt_row_id` explicitly; envelope carries none |
| 9 | Mandatory cache fields are frozen | **Yes** — exact 17-key set enforced both directions |
| 10 | RPC parser results are deep-copy isolated | **Yes** |
| 11 | Public serializers wrap malformed-object errors | **Yes** |
| 12 | Start RPC shape remains exactly seven keys | **Yes** (unchanged) |
| 13 | Submit RPC shape remains exactly 13 keys | **Yes** (unchanged) |
| 14 | Replay remains authoritative | **Yes** — canonical decision rows drive reconstruction; envelope cache is verify-only |
| 15 | Engine V1 remains unchanged | **Yes** — `git status` confirms `utils/scenario_persistence.py` untouched |
| 16 | All focused tests pass | **Yes** — 471 passed, 9 subtests passed |
| 17 | No SQL, migration, database, UI, or controller work occurred | **Yes** |
| 18 | Protected paths remain untouched | **Yes** |
| 19 | Nothing staged, committed, pushed, or deployed | **Yes** |

---

## Remaining risks

1. End-to-end adapter↔PostgREST↔SQL path is still unexercised beyond the Python-only SQL-shape smoke check above; a disposable-database smoke test (one real start + one real submit through the actual V68/V69 RPCs) remains the responsibility of the next orchestration task.
2. Whole-envelope JSONB CAS (used by the V68/V69 SQL) is sensitive to any future non-canonical float/key formatting drift; `serialize_run_snapshot_v2` remains the single source of truth and should not be duplicated elsewhere.
3. Start/resume orchestration (wiring this adapter to a real Supabase client) is still out of scope and unimplemented, as required by this task.

---

## Recommended next task

**SIM-PERSIST-V2-05 — Start/resume orchestration** against the disposable database validated in SIM-PERSIST-V2-03, using this corrected adapter. A disposable-database smoke test (one real `start_or_resume_scenario_attempt_v1` call and one real `submit_scenario_decision_v1` call, built entirely from this adapter's `build_*_rpc_params_v2` output) should be the first validation step of that task, to close the "Remaining risks" item above.

---

## Task hygiene

| Check | Result |
|---|---|
| Source/tests/docs modified | `utils/scenario_persistence_v2.py`, `tests/test_scenario_persistence_v2.py`, `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_ADAPTER_IMPLEMENTATION_REPORT.md` |
| Files created | `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_ADAPTER_CORRECTION_REPORT.md` |
| SQL/migration files modified | No |
| Controller/UI modified | No |
| Engine V1 modified | No |
| Protected paths inspected or modified | No |
| Database connection made | No |
| Staged / committed / pushed / deployed | No |
| Branch / HEAD at start | `main` / `6136673` |
