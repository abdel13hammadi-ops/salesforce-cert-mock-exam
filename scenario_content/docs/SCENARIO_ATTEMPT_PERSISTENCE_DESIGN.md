# Scenario Attempt and Decision Persistence Design (SIM-PERSIST-04A)

Status: **Superseded in specific, enumerated ways by the implemented V1
("SIM-PERSIST-04B"), then further corrected by a line-by-line security and
integrity review ("SIM-PERSIST-04C"), then further corrected by a final
independent review ("SIM-PERSIST-04E"), then further corrected by a
concurrency-and-idempotency closure review ("SIM-PERSIST-04F").** This
document is preserved as-written
for its evidence and rationale (§1–§16 below are unchanged from the original
architecture-only draft), but §0 immediately below is the authoritative
summary of where the *actually implemented* schema/RPCs/adapter diverge from
what this document originally proposed. Where §0 and the rest of this
document disagree, §0 (and the implementation it describes —
`supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql`,
`utils/scenario_persistence.py`) is authoritative. `public.scenario_attempts`
and `public.scenario_decisions` now exist locally (unexecuted; not yet applied
to any database). `V66`/`V67` (already applied to production and verified)
remain unmodified and are the fixed, load-bearing foundation this document and
the implementation both build on.

## 0. Implementation Addendum (SIM-PERSIST-04B — final V1 decisions)

A set of explicit V1 decisions, made at implementation time, superseded the
open questions and some proposals below. Each is a deliberate choice, not an
oversight — this section exists so a future reader does not need to diff the
migration against this document by hand to find them.

1. **No `complete_scenario_attempt_v1`.** §9.3/§9.5 below proposed keeping a
   narrow reconciliation RPC alongside terminal-completion-inside-`submit_
   scenario_decision_v1`. The implemented V1 has **only** the terminal branch
   inside `submit_scenario_decision_v1` — no standalone or reconciliation
   completion RPC exists. Because the terminal decision `INSERT` and the
   attempt's `completed` transition are one `UPDATE` in the same transaction
   (§9.2/§9.5's own reasoning), the failure window a reconciliation RPC would
   repair is judged not to have a real, observed use case yet; one can be
   added later without any migration to existing rows.
2. **Reads go through an RPC, not a direct `service_role SELECT`.** §9.6/§10.3
   proposed direct table reads and explicitly named the resulting "no
   database-enforced read ownership boundary" as the single largest residual
   risk (§16). The implemented V1 closes that gap with `get_scenario_attempt_v1`
   — a `SECURITY INVOKER` RPC that performs a single combined
   `(id, owner)` lookup so an unknown attempt id and an attempt owned by a
   different learner are structurally indistinguishable to a caller. §10.3's
   named risk is therefore resolved, not merely accepted.
3. **`scenario_attempts` stores one authoritative `serialized_engine_state`
   JSONB column, not a set of narrow terminal-only cache columns.** §5.2
   proposed `current_scene_id` / `decision_count` / `ending_id` / `score_band`
   / `final_state_snapshot` / `domain_performance_snapshot` as separate cache
   columns. The implemented V1 instead persists exactly one JSONB column,
   `serialized_engine_state` — the literal output of
   `utils.scenario_engine.serialize_run_snapshot(...)` — plus `current_scene_id`,
   `next_sequence_number` (renamed from `decision_count`; see point 5), and the
   terminal pair `terminal_ending_id`/`terminal_result_snapshot`. This keeps
   exactly one JSON snapshot as the resumable state of record instead of
   several separately-named scalar caches, and is what
   `utils/scenario_persistence.py`'s `validate_serialized_engine_state(...)`
   structurally validates before every write.
4. **`scenario_decisions` stores `state_before`/`state_after` per row.** §6.2
   explicitly proposed *not* storing per-decision state ("fully derivable by
   replaying decisions 1..N"). The implemented V1 stores both JSONB snapshots
   on every decision row, because `submit_scenario_decision_v1` uses the
   supplied `state_before` as a direct equality check against the attempt's
   persisted `serialized_engine_state` (detecting a stale/incorrect caller
   view without SQL ever recomputing engine logic) — a check this document's
   original model could not perform without a full Python replay call, on
   every submission, from inside the validation path. `domain_id`/`is_correct`
   /`next_scene` (§6.2's audit columns) are dropped entirely: persisting them
   would mean SQL recording engine-derived judgments this task's explicit
   decision (see EXPLICIT V1 DECISIONS §3 of the SIM-PERSIST-04B task brief)
   restricts to Python alone.
5. **RPC/column naming differs from §9's draft.** `p_simulation_id` →
   `p_scenario_version_id` (attempts pin a *version*, not a scenario, per the
   task's explicit "permanently pinned to `scenario_versions.id`" decision);
   `decision_count` → `next_sequence_number` (stores the *next* value to
   insert, matching `submit_scenario_decision_v1`'s own
   `p_expected_sequence_number` contract, rather than a count requiring
   `+ 1` at every call site); `ending_id`/`score_band` → `terminal_ending_id`
   /`terminal_result_snapshot` (one JSONB snapshot replaces two scalar
   columns, matching point 3).
6. **The request-fingerprint formula covers more fields than §7.2 proposed.**
   §7.2 proposed `sha256(sequence_number || scene_id || option_id)`. Because
   the implemented decision-submission contract also carries `state_before`,
   `state_after`, `resulting_scene_id`, `is_terminal`, and `terminal_ending_id`
   as caller-supplied (not SQL-recomputed) values, the fingerprint must cover
   all of them to actually detect "conflicting reuse of the same key with
   different input" for those fields too — see
   `utils.scenario_persistence.compute_request_fingerprint(...)`'s docstring
   for the exact, single, deterministic formula
   (`sha256(json.dumps(..., sort_keys=True, separators=(",", ":")))` over all
   nine covered fields).
7. **`abandon_scenario_attempt_v1` is idempotent on an already-abandoned
   attempt** (§9.4's own proposal is followed exactly here: "re-calling on an
   already-abandoned attempt is a safe no-op"), returning the existing final
   state rather than raising — the implemented behavior matches this
   document's §9.4 unchanged.
8. **Read ownership caveat is resolved, not merely called out** — see point 2;
   §16's first bullet ("Read-boundary ownership … largest residual risk") no
   longer applies to `get_scenario_attempt_v1` specifically. It would still
   apply to any future ad hoc direct-table read added outside that RPC.

## 0-B. Implementation Addendum (SIM-PERSIST-04C — security & integrity corrections)

A completed line-by-line security and integrity review of the SIM-PERSIST-04B
implementation found six defect classes, all corrected in place (same
migration file, same timestamp/filename, no new migration). Each correction
below narrows §0 above; nothing in §0 points 1–8 was reversed.

9. **Table grants are now revoked explicitly from every relevant role, not
   only from `PUBLIC`.** §10's original model (and §0's unchanged carry-over
   of it) revoked table privileges only from `PUBLIC`, then granted to
   `service_role` — silently relying on `anon`/`authenticated`/`service_role`
   never having been otherwise granted a privilege to stay at zero. This is
   the *exact* production defect SIM-PERSIST-04C's review found already
   existed once for `V66` (`scenarios`/`scenario_versions`). The migration's
   grants section now issues an explicit `REVOKE ALL` from `PUBLIC`, `anon`,
   `authenticated`, **and** `service_role` on both `public.scenario_attempts`
   and `public.scenario_decisions` FIRST, and only THEN re-grants the exact
   minimum: `service_role` gets `SELECT, INSERT, UPDATE` on
   `scenario_attempts` and `SELECT, INSERT` on `scenario_decisions` — never
   `DELETE`, `TRUNCATE`, `REFERENCES`, or `TRIGGER` on either. Object
   ownership (the `postgres` owner) is never targeted by any `REVOKE`/`GRANT`
   and is therefore unaffected.
10. **Both tables now also guard `INSERT`, not only `UPDATE`/`DELETE`.** §11's
    immutability model (unchanged, still true for `UPDATE`/`DELETE`) did not,
    by itself, stop a direct `service_role` `INSERT` from bypassing all four
    RPCs. Both trigger functions now additionally fire `BEFORE INSERT`:
    `start_or_resume_scenario_attempt_v1` generates the new attempt's `uuid`
    itself, calls
    `set_config('certbound.scenario_attempt_insert_guard', <uuid>, true)`
    immediately before its own `INSERT`, and inserts that exact id explicitly
    (never relying on the column `DEFAULT`); `submit_scenario_decision_v1`
    does the identical thing via `certbound.scenario_decision_insert_guard`
    for the new decision's `uuid`. `guard_scenario_attempt_mutation_v1`/
    `guard_scenario_decision_immutability_v1` each reject a `BEFORE INSERT`
    firing unless the matching guard names `NEW.id` exactly
    (`attempt_insert_guard_violation`/`decision_insert_guard_violation`).
    Documented honestly, exactly like every other guard in this migration: an
    application/RPC mutation-boundary safeguard for normal `service_role` API
    usage, not a defense against a database administrator or any other actor
    able to run arbitrary trusted SQL in the same transaction as a legitimate
    RPC call (such an actor could `set_config(...)` themselves).
11. **Idempotent replay is now stable across later decisions.** The
    SIM-PERSIST-04B safe-retry branch returned the *attempt's current* state
    on a matching `(idempotency_key, request_fingerprint)` retry — correct
    only until a later decision on the same attempt advanced or completed it,
    at which point an older retry would incorrectly start reporting that
    later state. `submit_scenario_decision_v1`'s replay branch now derives
    the result entirely from *that decision's own* immutable
    `scenario_decisions` row: for a non-terminal decision,
    `attempt_status='in_progress'`, `current_scene_id` = the decision's own
    stored `resulting_scene_id`, `next_sequence_number` = the decision's own
    stored `sequence_number + 1`, `serialized_engine_state` = the decision's
    own stored `state_after`, terminal fields `NULL` — never read from
    `scenario_attempts` at all. For a terminal decision, `attempt_status`
    is hardcoded `'completed'`, `current_scene_id` is `NULL`, and
    `terminal_ending_id`/`terminal_result_snapshot`/`completed_at` are read
    from `scenario_attempts` specifically because a completed attempt is
    permanently immutable (§11) — reading them from there is exactly as
    stable as duplicating them.
12. **Snapshot IDENTITY/LIFECYCLE consistency is now a validated integrity
    boundary, not an unchecked assumption.** Neither this document nor
    SIM-PERSIST-04B previously required the RPCs to check that a caller's
    supplied serialized-state snapshot was internally self-consistent.
    `start_or_resume_scenario_attempt_v1` now validates that
    `p_initial_serialized_state.engineVersion`/`.canonicalContentSha256`
    equal the pinned, published `scenario_versions` row's values,
    `.currentSceneId` equals `p_initial_current_scene_id`, `.isComplete` is
    `false`, `.terminalResult` is `null`, and `.simulationId`/`.version` are
    normalized non-empty strings
    (`invalid_initial_state_identity`/`invalid_initial_state_lifecycle`).
    `submit_scenario_decision_v1` now validates that `state_before` and
    `state_after` agree on every immutable identity field
    (`simulationId`/`version`/`canonicalContentSha256`/`engineVersion`), that
    `state_before.currentSceneId` equals `p_expected_scene_id` and
    `.isComplete` is `false`, and — branching on `p_is_terminal` — that
    `state_after` is fully consistent with either the non-terminal
    (`currentSceneId` = `p_resulting_scene_id`, `isComplete` = `false`,
    `terminalResult` = `null`) or terminal (`currentSceneId` = `null`,
    `isComplete` = `true`, `terminalResult` a JSON object equal to
    `p_terminal_result_snapshot`) shape
    (`state_identity_mismatch`/`state_lifecycle_mismatch`/
    `terminal_result_mismatch`). Every one of these is a pure equality/shape
    check against values the caller already supplied — SQL still never
    calculates which scene, score, or ending is correct (§1's boundary is
    unchanged). `utils/scenario_persistence.py` performs the identical
    checks client-side before ever calling the RPC, and maps every one of
    the new SQL exception prefixes to a focused
    `ScenarioSnapshotConsistencyError`/`ScenarioInsertGuardViolationError`.
13. **The request fingerprint now explicitly covers `terminal_result_snapshot`
    too**, on top of the nine fields point 6 above already listed — ten
    covered fields in total. `compute_request_fingerprint(...)` gained a
    keyword-only `terminal_result_snapshot` parameter for this; a *supplied*
    `request_fingerprint` must already be stripped and exactly 64 lowercase
    hexadecimal characters (uppercase is rejected, not silently
    lowercased) before it is sent to the RPC in that same normalized form.
14. **Python input validation is now strict, not permissive-by-coercion.**
    `is_terminal` must be an actual `bool` (a string or integer is rejected
    outright, never passed through `bool(...)`); `expected_sequence_number`
    must be an actual `int` that is *not* a `bool` (Python's `bool` is an
    `int` subclass, so `True` no longer silently becomes `1`) and must be
    `>= 1`; a caller-*supplied* `idempotency_key` must be a valid UUID
    **version 4** specifically, not merely any well-formed UUID (a
    Python-*generated* key, when the caller omits one, remains UUIDv4 as
    before).

## 0-C. Implementation Addendum (SIM-PERSIST-04E — final integrity corrections)

A final independent review of the SIM-PERSIST-04C implementation found four
further SQL/verification defect classes and three further Python-adapter
defect classes, all corrected in place (same migration file, same
timestamp/filename, no new migration; `V66`/`V67` untouched). Each correction
below narrows §0-B above; nothing in §0/§0-B was reversed.

15. **`p_initial_serialized_state.simulationId`/`.version` are now pinned
    EXACTLY to the database row, not merely checked for "some" normalized
    non-empty string.** Point 12 (§0-B) required `simulationId`/`version` to
    be normalized non-empty strings, but never actually compared them
    against anything — a caller could supply any well-formed string for
    either field and `start_or_resume_scenario_attempt_v1` would accept it.
    The RPC's scenario-version lookup now additionally selects
    `scenarios.simulation_id` and `scenario_versions.version` (via the same
    `scenario_versions JOIN scenarios` already required for the FK), and
    requires `p_initial_serialized_state.simulationId`/`.version` to be a
    JSON string, non-empty, already `BTRIM`-normalized, and **exactly equal**
    to those two fetched, pinned values — using the focused
    `invalid_initial_state_identity` exception, the same prefix point 12
    already established. This remains identity validation only: no graph
    transition or score is computed.
16. **Every snapshot identity/lifecycle field's JSON TYPE is now checked
    BEFORE any `->>` textual comparison is trusted.** Point 12 (§0-B)'s
    equality checks relied on `->>`, which silently coerces a JSON number or
    boolean to its text representation (`5 ->> ` = `"5"`) — so a caller could
    previously have smuggled e.g. a JSON number where a string was required,
    as long as its textual form happened to match. Both
    `start_or_resume_scenario_attempt_v1` (for
    `p_initial_serialized_state.simulationId`/`.version`/`.engineVersion`/
    `.canonicalContentSha256`/`.currentSceneId`/`.isComplete`) and
    `submit_scenario_decision_v1` (for `p_state_before`/`p_state_after`'s
    identical field set, on **both** snapshots independently) now call
    `jsonb_typeof(...)` and require the exact expected type
    (`string`/`boolean`/`null`/`object` as appropriate) before ever reading
    the value with `->>`. Violations use the same `invalid_initial_state_
    identity`/`invalid_initial_state_lifecycle`/`state_identity_mismatch`/
    `state_lifecycle_mismatch` prefixes point 12 already established — this
    is a stricter *type* gate on the identical *equality* checks, not a new
    validation class.
17. **Terminal-ending identity can no longer silently disagree across its
    three duplicated locations.** The engine snapshot and the attempt
    duplicate a terminal decision's ending identity across
    `p_terminal_ending_id`, `p_terminal_result_snapshot.endingId`, and (via
    the full-object equality point 12 already required)
    `p_state_after.terminalResult.endingId` — but nothing previously required
    the first two to agree with each other. `submit_scenario_decision_v1`
    now requires, for a terminal decision, that
    `p_terminal_result_snapshot.endingId` is a normalized, non-empty JSON
    string EXACTLY equal to `p_terminal_ending_id`, using the new, focused
    `terminal_ending_mismatch` exception prefix — checked during scalar
    validation, before the snapshot IDENTITY/LIFECYCLE block, so it never
    depends on (and is never masked by) point 12's `terminal_result_mismatch`
    check. `utils/scenario_persistence.py`'s
    `_validate_decision_snapshot_consistency` mirrors this exact check
    locally, before any RPC call, and `terminal_ending_mismatch:` is mapped
    to `ScenarioSnapshotConsistencyError` in `_ERROR_PREFIX_MAP`, the same
    exception class every other snapshot-consistency prefix already maps to.
    This still never calculates *which* ending is correct — only that the
    two caller-supplied identities agree.
18. **`v68_scenario_attempt_persistence_verification.sql`'s V40 case
    (renumbered V49) now actually reaches the check it claims to prove.**
    The original case supplied `p_expected_scene_id = NULL` and a completed
    (`isComplete = true`) `state_before` while asserting the outcome would be
    `attempt_not_in_progress` — but scalar validation (`invalid_expected_
    scene_id`) or snapshot validation (`state_lifecycle_mismatch`) would
    raise first, so the locked attempt-status check was never actually
    exercised. The corrected case supplies mutually consistent, fully valid
    non-terminal inputs (`expected_sequence = 3`, `expected_scene =
    'scene-2'`, `state_before` = the attempt's own real post-decision-1
    state, `state_after` = a self-consistent continuation to `'scene-3'`, all
    terminal fields `NULL`) against an attempt that has since been completed
    by a later decision — so every earlier validation stage passes and
    `attempt_not_in_progress` is what actually fires. Every other
    expected-failure verification case was independently re-reviewed for the
    same defect class (input that fails an earlier stage than the one it
    claims to test); none had it. The verification script's total check
    count grew from 51 (V1–V51) to 60 (V1–V60): six new cases for point 15
    above (V20–V25: mismatched/whitespace-padded `simulationId`/`version`,
    plus numeric-`simulationId`/numeric-`currentSceneId` type-check proofs),
    three new cases for point 16 above (V37–V39: numeric-`simulationId` in
    `state_after`, numeric-`currentSceneId` in `state_before`, and the V40
    fix's own prerequisite — a passing terminal-ending-identity case is
    already exercised as part of the corrected V40/V49), and the V40 fix
    itself (V40 became V49, everything after it shifted accordingly). Every
    check from V17 onward was renumbered sequentially; the header comment,
    section-range comments, and the final summary NOTICE were all updated to
    match.
19. **`compute_request_fingerprint(...)`'s scalar inputs are now validated
    with this module's own strict helpers, never permissive coercions.** This
    is a *public* Python helper (unlike the already-strict private
    validation inside `submit_decision(...)` itself), and it previously did
    `int(expected_sequence_number)`/`bool(is_terminal)`/`str(attempt_id)` —
    `int("1")` and `int(True)` would silently accept a string or a `bool` as
    a valid sequence number, and a non-UUID `attempt_id` would silently be
    hashed as whatever string it happened to be. It now uses the identical
    strict helpers `submit_decision(...)` already used internally
    (`_require_strict_int` with `minimum=1`, `_require_strict_bool`,
    `_require_uuid_str`, `_require_nonempty_str`) — a malformed input to this
    helper is now rejected with `ScenarioPersistenceValidationError`, never
    silently coerced into *some* fingerprint.
20. **Every RPC response field this module depends on is now parsed with a
    focused, strict helper.** The four `_parse_*_row` functions previously
    used `bool(...)` (`created`, `idempotent_replay`), permissive `int(...)`
    (`sequence_number`, `next_sequence_number`), plain `str(...)` (every UUID
    identity field, `status`/`attempt_status`, `scenario_content_sha256`),
    and — most seriously — `dict(value or {})` for `serialized_engine_state`,
    which would silently coerce a malformed, falsy-but-non-object response
    value like `[]` into an empty object instead of rejecting it. New helpers
    (`_require_strict_bool_field`, `_require_strict_int_field`,
    `_require_uuid_field`, `_require_json_object_field`,
    `_require_nullable_json_object_field`, `_require_lifecycle_status_field`
    against `{'in_progress', 'completed', 'abandoned'}`,
    `_require_content_hash_field`) now enforce the exact required type/shape
    for every one of those fields, raising `ScenarioPersistenceBackendError`
    — never `ScenarioPersistenceValidationError`, which remains reserved for
    caller input — for a response that does not already have it.
21. **`validate_serialized_engine_state(...)` no longer silently normalizes
    any field it validates.** It previously accepted (and passed through
    unchanged) an already-non-normalized value by checking a *normalized
    copy* of it — `canonicalContentSha256` was matched against the hash
    pattern only after `.strip().lower()`, and `currentSceneId` was accepted
    as long as it was *any* string, including one that was empty or
    whitespace-only or padded. This function validates shape; it must never
    rewrite a caller's value to make it pass. It now requires
    `simulationId`/`version`/`engineVersion` to already equal their own
    `.strip()`ped form (not merely be non-empty once stripped),
    `canonicalContentSha256` to already be exactly 64 **lowercase**
    hexadecimal characters (an uppercase-containing value is rejected, never
    lowercased for the comparison), and `currentSceneId`, when non-null, to
    already be a non-empty string equal to its own `.strip()`ped form.

## 0-D. Implementation Addendum (SIM-PERSIST-04F — concurrency and idempotency closure)

A further independent review of the SIM-PERSIST-04E release-candidate bundle
found four final integrity defects, all corrected in place (same migration
file, same timestamp/filename, no new migration; `V66`/`V67` untouched). Each
correction below narrows §0/§0-B/§0-C above; nothing in them was reversed.

22. **`get_scenario_attempt_v1` now locks its combined `(id, owner)` lookup
    with `FOR SHARE`, closing a READ COMMITTED read-skew window.** The RPC
    previously ran one `SELECT` of `scenario_attempts` and, moments later, a
    separate `SELECT` of `scenario_decisions` — at READ COMMITTED isolation
    (Postgres's default and this project's), a concurrent
    `submit_scenario_decision_v1`/`abandon_scenario_attempt_v1` call could
    commit in the gap between those two statements, so the RPC could return
    the attempt's **pre-commit** `current_scene_id`/`serialized_engine_state`/
    `status` alongside the **post-commit** decision history — an internally
    inconsistent snapshot. The combined `(id, owner)` `SELECT` now ends with
    `FOR SHARE`, held for the rest of the RPC's own transaction, so a
    concurrent `FOR UPDATE` locker (both other RPCs already lock this way)
    cannot commit a mutation until this read completes. `FOR KEY SHARE` was
    deliberately rejected: it only conflicts with a concurrent `DELETE` or a
    key-column `UPDATE`, not with the ordinary non-key `UPDATE`s
    `submit_scenario_decision_v1`/`abandon_scenario_attempt_v1` perform, so it
    would not have closed this window. The RPC remains read-only with respect
    to stored data; it acquires a lock but writes nothing. The single
    combined `(id, owner)` predicate and the identical `attempt_not_found`
    outcome for an unknown id vs. a wrong owner are both unchanged. A real
    two-session exercise of the resulting blocking behavior — one session
    holding this lock open mid-transaction while a second session's
    concurrent mutation blocks until the first commits or rolls back —
    requires two concurrent database connections and so cannot be exercised
    inside this project's single-connection, `BEGIN`/`ROLLBACK`-scoped
    verification script; the verification script instead proves (V17) that
    the installed function's source text actually contains the `FOR SHARE`
    clause (and does not use `FOR KEY SHARE`), and a real two-session
    concurrency exercise is documented as belonging in this project's
    upcoming throwaway-database concurrency test gate.
23. **A matching `request_fingerprint` alone is no longer sufficient to treat
    a decision submission as a safe idempotent retry.** Point 12 (§0-B)'s
    safe-retry branch compared only `request_fingerprint` against the
    already-committed row for the same `(attempt_id, idempotency_key)` — that
    permitted the same attempt, same idempotency key, and same *supplied*
    fingerprint to be replayed with a genuinely **different**
    `selected_option_id`/state/sequence/scene/terminal-ending and still be
    silently treated as a safe replay of the original decision.
    `submit_scenario_decision_v1`'s idempotency-key lookup now also loads
    `sequence_number`, `expected_scene_id`, `selected_option_id`,
    `state_before`, `state_after`, `resulting_scene_id`, `is_terminal`, and
    `terminal_ending_id` from the existing row, and treats the retry as safe
    only when `request_fingerprint` matches **and** every one of those eight
    stored fields is `IS NOT DISTINCT FROM` the corresponding current
    parameter; any single disagreement raises the existing, focused
    `idempotency_key_conflict:` exception instead of returning a stale
    replay. `terminal_result_snapshot` needed no ninth decision-table column:
    for a terminal submission, `state_after.terminalResult` is already
    required (point 12/16 above) to equal `terminal_result_snapshot` exactly,
    and `state_after` itself is one of the eight compared fields, so an
    inconsistent `terminal_result_snapshot` cannot hide behind an unchanged
    `state_after`. This binding check still runs before the ordinary
    attempt-status/sequence/scene/state-before rejection checks, exactly as
    the fingerprint-only check did. On the Python side, `submit_decision(...)`
    now **always** computes the canonical fingerprint itself, via
    `compute_request_fingerprint(...)`, from this call's own validated,
    normalized inputs — a caller-supplied `request_fingerprint` argument is
    still format-validated (stripped, then required to be exactly 64
    lowercase hexadecimal characters) but is now used **only** as an extra
    consistency check against that computed value; a mismatch raises
    `ScenarioPersistenceValidationError` with a new, focused
    `request_fingerprint_mismatch:` message **without calling the RPC at
    all**, and the value actually sent to the RPC is always the value this
    module computed, never an independently-trusted caller value.
24. **A terminal decision's `state_after.currentSceneId` must now be
    EXPLICITLY present as a JSON null, not merely absent.** Point 16 (§0-B/
    §0-C)'s terminal-state check accepted `(p_state_after -> 'currentSceneId')
    IS NULL OR jsonb_typeof(...) = 'null'` — but the `->` jsonb operator also
    evaluates to a SQL `NULL` when the key is simply **missing**, so a
    `state_after` object that omitted `currentSceneId` entirely was silently
    treated as equivalent to an explicit `"currentSceneId": null`. The check
    now requires `jsonb_typeof(p_state_after -> 'currentSceneId') = 'null'`
    exactly — `jsonb_typeof` of a SQL `NULL` (a missing key) is itself SQL
    `NULL`, which `IS DISTINCT FROM` the text `'null'`, so a missing key now
    raises the identical `state_lifecycle_mismatch:` exception a
    wrong-typed/wrong-valued key already raised. The non-terminal rule
    (`currentSceneId` must be a JSON **string**) is unchanged.
    `utils/scenario_persistence.py` already required the `currentSceneId` key
    to be present (via `REQUIRED_SERIALIZED_STATE_KEYS`) before this
    correction and needed no Python change here.
25. **`_require_nonempty_str(...)` no longer stringifies non-string values.**
    This private helper backs every scene id, option id, ending id, and
    engine-version identifier this module validates, and previously computed
    `str(value or "")` — meaning an integer, a `bool`, a `uuid.UUID` object,
    or any other non-string object with a truthy `str(...)` representation
    would be silently converted into *some* string and could then pass the
    non-empty/trim checks that followed, even though these fields are meant
    to be caller/engine-supplied string identifiers, never arbitrary
    stringifiable objects. It now requires `isinstance(value, str)` up front
    and raises `ScenarioPersistenceValidationError` immediately for anything
    else, before any `.strip()` or emptiness check runs. Legitimate string
    inputs are normalized exactly as before (stripped, then rejected if
    empty) — this narrows what is *accepted*, not what a valid string must
    look like.

The verification script's total check count grew from 60 (V1–V60) to 63
(V1–V63): one new read-only introspection case for point 22 above (V17, run
before the transactional block, alongside the other read-only structural
checks), one new transactional case for point 24 above (V37: a terminal
`state_after` with `currentSceneId` removed entirely is rejected), and one
new transactional case for point 23 above (V45: reusing the same idempotency
key and the same fingerprint, but with a different `selected_option_id`, is
rejected as a conflict rather than treated as a safe replay). Every check
from V17 onward was renumbered sequentially to make room; the header
comment, section-range comments, and the final summary `NOTICE` were all
updated to match.

Everything else in §1–§16 below — the evidence base, the ownership rationale
(§2.1), the V66/V67 convention-matching (§2.4), the one-active-attempt design
(§7.1), decision ordering (§8), the security/grants model (§10, superseded in
the specific ways point 9 above describes; otherwise unaffected by points 1–2
above beyond `get_scenario_attempt_v1` needing the same `service_role`-only
`EXECUTE` grant every other RPC here has), and the immutability model (§11,
extended by point 10 above to also cover `INSERT`) — was carried into the
implementation as originally written and is not restated here.

---

Status (original, as first drafted): **Architecture only.** No migration, RPC
implementation, Python persistence code, or database change is created by
this document. `V66`/`V67` (already applied to production and verified) are
unmodified and are treated here as a fixed, load-bearing foundation.

Labeling convention used throughout: every non-trivial decision is tagged as one of

- **Fact** — directly observed in the repository, cited with an exact path.
- **Evidence-based conclusion** — a design implication drawn from one or more Facts.
- **Assumption** — not verifiable from the repository as inspected; stated explicitly
  so it can be confirmed or corrected before implementation.
- **Recommendation** — the concrete choice this document commits to (never left as
  an open menu of equally-weighted options).

---

## 1. Executive Recommendation

Persist Scenario Simulator learner attempts in exactly two new tables,
`public.scenario_attempts` and `public.scenario_decisions`, mutated **only**
through four new `SECURITY INVOKER`, `service_role`-only RPCs, following the
identical security posture already approved and hardened for `public.scenarios`
and `public.scenario_versions` in `V66`/`V67`. Ownership uses the repository's
existing normalized `user_email` identity — not `auth.uid()` — because that is
the only identity actually flowing through this application's authenticated
request path today. Every attempt is permanently pinned to one immutable
`scenario_versions.id` at creation. Decisions are strictly append-only,
sequence-numbered, and idempotent via a Python-generated UUIDv4 key compared
against a stored request fingerprint. `utils/scenario_engine.py` remains the
only place graph transitions, scoring, domain performance, and ending
evaluation are computed; SQL's job is exclusively concurrency-safe persistence,
ownership enforcement, ordering, idempotency, and immutability — never
recomputation of engine results.

---

## 2. Repository Evidence

### 2.1 Learner identity and ownership (the central open question)

- **Fact.** `utils/access_control.py` implements a custom HMAC-signed session
  token (`make_signed_session`/`verify_signed_session`, lines 90–120), not
  Supabase Auth. The signed payload's identity field is `user_email`
  (`verify_signed_session`, line 117: `email = str(payload.get("user_email") or
  payload.get("email") or "").strip().lower()`), and this is what
  `get_current_user_email()` (line 530) returns and what every authenticated
  page/RPC caller in this app is expected to use.
- **Fact.** `st.session_state["auth_user_id"]` exists in the session payload
  (`_hydrate_session_from_payload`, line 307) and is carried through the signed
  token, but it is only ever stored/round-tripped — it is never used as a
  foreign key, join key, or lookup key anywhere in `utils/access_control.py`,
  `utils/question_selection.py`, or the existing `exam_attempts`/
  `question_attempts` persistence path. There is no evidence in this repository
  that `auth_user_id` corresponds to a Supabase Auth `auth.users.id` /
  `auth.uid()` value with real referential meaning today.
- **Fact.** `utils/question_selection.py`'s `resolve_or_create_exam_attempt_id`
  (line 602) and `verify_exam_attempt_ownership` (line 549) — the existing,
  battle-tested attempt-ownership pattern in this codebase — key ownership
  exclusively on `user_email`, normalized via `_normalized(...)` (case-insensitive,
  whitespace-trimmed), matching `access_control.py`'s own `.strip().lower()`
  convention.
- **Fact.** The Python backend always connects to Supabase using the
  `service_role` key (`create_supabase_admin_client`, line 59); there is no
  browser-issued Supabase JWT anywhere in the authenticated request path for
  this application's own tables. `V66`'s migration header and `V67`'s hardening
  both explicitly document this: `service_role` bypasses RLS, so RLS policies
  keyed on `auth.uid()` would not even be reachable from this app's own traffic.
- **Evidence-based conclusion.** The correct V1 ownership identifier is
  **`user_email`** (trimmed, lowercased), exactly as already used for
  `exam_attempts`/`question_attempts`. `auth.uid()` is not appropriate — there
  is no repository evidence that a real Supabase Auth session ever reaches
  these tables, and inventing one here would silently diverge from every other
  persistence path in the app.
- **Recommendation.** `scenario_attempts.user_email` is the ownership column,
  normalized identically to the existing convention. The server proves an
  attempt belongs to the requesting learner by extracting `user_email` from the
  **already-verified, HMAC-signature-checked session payload** (server-side,
  via `verify_signed_session`/`get_current_user_email()`) and passing that
  value — never a browser-supplied email string, never a client-editable
  form field, never a raw query parameter — as the RPC's ownership parameter.
  This is exactly the "trusted `user_email` from signed server session, never
  browser" decision already ratified in `SCENARIO_PERSISTENCE_ARCHITECTURE.md`
  §4 (SIM-PERSIST-01A) and is reaffirmed here rather than revisited.
- **Assumption.** A future migration to stable Supabase Auth UUIDs remains
  possible (as already documented in `SCENARIO_PERSISTENCE_ARCHITECTURE.md`)
  but is explicitly out of scope; `scenario_attempts` does not add a speculative
  nullable `auth_user_id`/`user_id` column in V1, to avoid a half-populated
  column with no enforced meaning.

### 2.2 Existing attempt/session/idempotency conventions

- **Fact.** `exam_attempts`/`question_attempts` predate this repository's
  migration history (no `CREATE TABLE ... exam_attempts` migration file exists
  under `supabase/migrations/`) and are mutated directly from Python via
  `supabase.table("exam_attempts").insert(...)` / `.upsert(...)`
  (`utils/question_selection.py` lines 650, 737) — **not** through RPCs. This is
  a *weaker* mutation boundary than the one already established for
  `scenario_versions`/`publish_scenario_version_v1`.
- **Fact.** `question_attempts` idempotency relies on a plain unique constraint
  `(exam_attempt_id, question_id)` plus a client-side `upsert(...,
  on_conflict="exam_attempt_id,question_id")` (line 737) — safe for that
  workflow because every retry of the same question always carries the same
  final answer, so re-upserting is harmless. This pattern does **not** by
  itself distinguish "identical retry" from "conflicting reuse of the same key
  with different input," which this task explicitly requires — see §6.
- **Evidence-based conclusion.** Scenario attempts/decisions must **not**
  copy the `exam_attempts`/`question_attempts` direct-table-mutation pattern.
  `SCENARIO_PERSISTENCE_ARCHITECTURE.md` (SIM-PERSIST-01) already concluded
  RPC-only mutation is required here because decision submission needs
  transactional row locking, strict ordering, and true idempotency-conflict
  detection that a bare `upsert` cannot provide. `V66`/`V67` already established
  and hardened the RPC-only, `SECURITY INVOKER`, hardened-`search_path`,
  `service_role`-only-`EXECUTE` pattern for scenario definitions; this document
  extends that exact pattern to attempts/decisions rather than inventing a new
  one.
- **Fact.** Concurrency-safe advisory locking precedent:
  `supabase/migrations/20260624240000_v45_question_version_publication_gate.sql`
  lines 210 and 486 use `PERFORM pg_advisory_xact_lock(hashtext(<id>::text))`
  before mutating a row that is also protected by `SELECT ... FOR UPDATE`.
- **Fact.** Idempotency-key precedent:
  `supabase/migrations/20260625120000_v46_stripe_checkout_claims.sql` defines
  `billing_checkout_claims.idempotency_key text NOT NULL` with
  `UNIQUE (idempotency_key)` (line 20), and `claim_billing_checkout_v1` (line 58)
  looks up the existing row by that key **before** deciding whether to insert
  a new one or return the prior result (line 95–98).
- **Fact.** Partial-unique-index "at most one active X" precedent:
  `supabase/migrations/20260629120000_v46_free_mock_curation_foundation.sql`
  lines 75–81 define `idx_free_mock_sets_one_draft` and
  `idx_free_mock_sets_one_published` as `UNIQUE (exam_name, language_code) WHERE
  status = 'draft' | 'published'` — the exact structural precedent for
  "at most one `in_progress` attempt per (learner, version)" in §7.
- **Fact.** Immutability-trigger precedent: `V66`'s
  `guard_scenario_version_immutability_v1()`/`trg_guard_scenario_version_immutability`
  and `V67`'s `guard_scenario_current_published_version_v1()` establish this
  repository's pattern for enforcing a one-way state transition (`draft ->
  published`, guarded pointer changes) via a `BEFORE UPDATE OR DELETE` trigger
  plus a transaction-local `set_config(...)` guard read via `current_setting(...,
  true)`. This document reuses that exact mechanism for attempt lifecycle
  transitions (§5) and decision append-only enforcement (§8).

### 2.3 Scenario engine's state, serialization, and identity model

- **Fact.** `utils/scenario_engine.py` `ENGINE_VERSION = "SCENARIO_ENGINE_V1"`
  (line 17) and `TERMINAL_SENTINEL = "EVALUATE_ENDING"`
  (`utils/scenario_schema.py` line 22) are the two literal sentinel values a
  persistence layer must recognize.
- **Fact.** The engine's only authoritative, persistable replay input is
  `ScenarioDecisionInput(sequence_number: int, scene_id: str, option_id: str)`
  (lines 55–74) — "Replay trusts only these three fields; every other aspect of
  a run … is always recomputed." `replay_scenario_run(content, decisions)`
  (line 412) is the general reconstruction primitive (works for empty, partial,
  or complete histories); `build_terminal_result(run)` (line 340) is the only
  function that enforces completion.
- **Fact.** `replay_serialized_run(content, payload)` (line 602) verifies four
  identity fields before trusting any decision history:
  `simulationId`, `version`, `canonicalContentSha256`, `engineVersion`
  (`_verify_serialized_identity`, lines 573–599) — and explicitly ignores every
  other serialized field (`state`, `flags`, `currentSceneId`, `isComplete`,
  `terminalResult`) for reconstruction purposes.
- **Fact.** `ScenarioDecisionRecord` (lines 44–52) additionally carries
  `domain_id`, `is_correct`, `next_scene`, `state_after`, and `flags_after` per
  decision — these are all *engine-computed audit/derived* fields, never
  replay inputs.
- **Fact.** `ScenarioTerminalResult` (lines 90–101) carries `ending_id`,
  `score_band`, `narrative`, `recommended_review`, `final_state`, `flags`,
  `domain_performance`, `engine_version`, and `canonical_content_sha256` — the
  complete, engine-computed terminal snapshot.
- **Fact.** `deserialize_decision_history(value)` (line 546) is the strict
  parser for a JSON-decoded `decisionHistory` array (exactly `sequenceNumber`/
  `sceneId`/`optionId` per entry, no missing/extra fields, gap-free/duplicate-
  free/ascending sequence numbers, never sorted or normalized).
- **Evidence-based conclusion.** The engine already defines the exact minimal
  replay contract this persistence layer must satisfy: **decisions
  (`sequence_number`, `scene_id`, `option_id`) are the only data that must be
  durable and correctly ordered for the run to be perfectly reconstructible.**
  Everything else the engine computes (`state`, `flags`, `domain_performance`,
  `ending`) is legitimate to cache for cheap reads, but must never be treated
  as more authoritative than a fresh `replay_scenario_run` call against the
  stored decisions and the pinned `scenario_versions.content_snapshot`.

### 2.4 V66/V67 conventions this design must match exactly

- **Fact.** `public.scenarios`/`public.scenario_versions`
  (`supabase/migrations/20260718170000_v66_scenario_definition_persistence_foundation.sql`)
  use: `uuid PRIMARY KEY DEFAULT gen_random_uuid()`; `timestamptz NOT NULL
  DEFAULT now()`; named `CHECK` constraints enforcing `BTRIM(x) = x AND x <>
  ''` normalization; a composite `UNIQUE (scenario_id, id)` on the child table
  specifically to support a composite FK from the parent that also proves
  "belongs to the same scenario"; `SECURITY INVOKER` + `SET search_path =
  public, pg_catalog` on every function; `REVOKE ALL ... FROM PUBLIC, anon,
  authenticated` before an exact `GRANT` to `service_role`; RLS enabled with
  zero policies (hardened further in `V67`, which also removed
  `service_role` `DELETE` and confirmed zero `TRUNCATE`/`REFERENCES`/`TRIGGER`
  grants).
- **Fact.** `V67`
  (`supabase/migrations/20260719003000_v67_harden_scenario_definition_security.sql`)
  additionally established: an explicit precondition block that fails atomically
  if a prerequisite object is missing; a single `BEGIN; ... COMMIT;` wrapper for
  migration atomicity; a documented, honestly-scoped transaction-local guard
  (`certbound.publish_scenario_version_guard`) that is explicitly **not** a
  defense against a database administrator or arbitrary trusted SQL in the same
  transaction — only against an ordinary buggy/raw `service_role` API call
  bypassing the intended RPC.
- **Recommendation.** Every new object in this design (tables, triggers, RPCs,
  grants) follows these exact conventions with no deviation, so this remains
  one coherent security and operational model rather than two.

---

## 3. Current Runtime Contract Recap (unchanged, cited for traceability)

`utils/scenario_engine.py` is already complete and is not modified by this
design:

| Capability | Function |
|---|---|
| Start a fresh run | `start_scenario_run(content)` |
| Apply one decision | `apply_decision(run, option_id)` |
| Reconstruct from ordered history (any length) | `replay_scenario_run(content, decisions)` |
| Enforce/extract completion | `build_terminal_result(run)` |
| Strict JSON history parsing | `deserialize_decision_history(value)` |
| Verify identity + replay a serialized payload | `replay_serialized_run(content, payload)` |
| JSON-safe serialization | `serialize_run_snapshot(run)` / `serialize_terminal_result(result)` |

This document assumes the Python persistence layer (not created here) calls
these functions directly and passes their **outputs** to the RPCs below as
plain parameters — SQL never re-derives any of them.

---

## 4. Proposed Entity Model

Exactly two new tables, matching the four core entities already named in
`SCENARIO_PERSISTENCE_ARCHITECTURE.md` (two of which — `scenarios`,
`scenario_versions` — already exist as `V66`/`V67`):

1. `public.scenarios` — **exists** (V66/V67, unmodified).
2. `public.scenario_versions` — **exists** (V66/V67, unmodified).
3. `public.scenario_attempts` — **new** (this design).
4. `public.scenario_decisions` — **new** (this design).

No Creative Studio table (company/character/relationship/dialogue/asset) is
introduced. **Recommendation:** the only forward-compatible extension point
needed for Creative Studio is that `scenario_attempts`/`scenario_decisions`
reference stable, already-immutable IDs (`scenario_versions.id`,
`scenario_attempts.id`) — any future creative asset table can attach to those
IDs later via its own foreign key without ever modifying attempt/decision
history. Nothing here anticipates or requires that schema now.

---

## 5. `public.scenario_attempts`

### 5.1 Purpose

One row per learner "run" of one pinned scenario version — the durable
lifecycle record. Analogous in role to `exam_attempts`, but RPC-mutated and far
more tightly constrained because attempts must be exactly replayable.

### 5.2 Columns

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | NOT NULL | `gen_random_uuid()` | PK. |
| `user_email` | `text` | NOT NULL | — | Ownership key. **Recommendation** (§2.1). |
| `scenario_id` | `uuid` | NOT NULL | — | FK to `scenarios.id`, denormalized for direct "all attempts for scenario X across versions" queries without a join. |
| `scenario_version_id` | `uuid` | NOT NULL | — | FK to `scenario_versions.id`. **Immutable after insert** (§5.5). This is the version-pinning field. |
| `status` | `text` | NOT NULL | `'in_progress'` | `in_progress` \| `completed` \| `abandoned` only (§5.4). |
| `engine_version` | `text` | NOT NULL | — | Copied from `scenario_engine.ENGINE_VERSION` at start; pinned. |
| `canonical_content_sha256` | `text` | NOT NULL | — | Copied from the pinned `scenario_versions.canonical_content_sha256` at start; pinned. Redundant with the FK on purpose — lets identity be re-verified without a join, mirroring `replay_serialized_run`'s own identity contract. |
| `current_scene_id` | `text` | NULL | — | **Cache**, not authoritative. Null once `is_complete`. Recomputed/verified by Python via `replay_scenario_run` at every resume; the RPC also updates it transactionally on every decision (§9). |
| `decision_count` | `int` | NOT NULL | `0` | **Cache** of `count(scenario_decisions WHERE attempt_id = this)`. Doubles as "next expected sequence number minus one." |
| `ending_id` | `text` | NULL | — | **Cache**, populated only at completion, from `ScenarioTerminalResult.ending_id`. Never set for `in_progress`/`abandoned`. |
| `score_band` | `text` | NULL | — | **Cache**, same rule as `ending_id`. |
| `final_state_snapshot` | `jsonb` | NULL | — | **Cache** of `dict(final_state)`, populated only at completion. |
| `domain_performance_snapshot` | `jsonb` | NULL | — | **Cache** of serialized `domain_performance`, populated only at completion. |
| `started_at` | `timestamptz` | NOT NULL | `now()` | Immutable. |
| `updated_at` | `timestamptz` | NOT NULL | `now()` | Bumped by every RPC-driven mutation. |
| `completed_at` | `timestamptz` | NULL | — | Set exactly once, only by `complete_scenario_attempt_v1`/the terminal path of `submit_scenario_decision_v1`. |
| `abandoned_at` | `timestamptz` | NULL | — | Set exactly once, only by `abandon_scenario_attempt_v1`. |

### 5.3 Keys, constraints, indexes

- `PRIMARY KEY (id)`.
- `CONSTRAINT scenario_attempts_user_email_normalized CHECK (user_email = BTRIM(LOWER(user_email)) AND user_email <> '')` — matches the app-wide `.strip().lower()` convention exactly (§2.1); normalization is enforced in the database, not only in Python, per this task's "do not rely only on Python" requirement.
- `CONSTRAINT scenario_attempts_status_valid CHECK (status IN ('in_progress', 'completed', 'abandoned'))`.
- `FOREIGN KEY (scenario_id) REFERENCES scenarios (id) ON DELETE RESTRICT`.
- `CONSTRAINT scenario_attempts_scenario_version_fk FOREIGN KEY (scenario_id, scenario_version_id) REFERENCES scenario_versions (scenario_id, id) ON DELETE RESTRICT` — the **same composite-FK trick already used** for `scenarios.current_published_version_id` (`V66` §3): guarantees `scenario_id` can never disagree with the version's true parent scenario, at the schema level, for free.
- `CONSTRAINT scenario_attempts_status_timestamps_consistent CHECK ((status = 'completed') = (completed_at IS NOT NULL) AND (status = 'abandoned') = (abandoned_at IS NOT NULL) AND (status = 'in_progress') = (completed_at IS NULL AND abandoned_at IS NULL))` — status and its timestamp are always in lock-step; satisfies "completion and abandonment timestamps must be internally consistent."
- `CONSTRAINT scenario_attempts_completed_at_after_started CHECK (completed_at IS NULL OR completed_at >= started_at)`.
- `CONSTRAINT scenario_attempts_abandoned_at_after_started CHECK (abandoned_at IS NULL OR abandoned_at >= started_at)`.
- `CONSTRAINT scenario_attempts_terminal_cache_requires_completion CHECK (status = 'completed' OR (ending_id IS NULL AND score_band IS NULL AND final_state_snapshot IS NULL AND domain_performance_snapshot IS NULL))` — the cache columns can never be populated for a non-completed attempt.
- **Partial unique index (one-active-attempt guarantee, §7):**
  `CREATE UNIQUE INDEX idx_scenario_attempts_one_in_progress ON scenario_attempts (user_email, scenario_version_id) WHERE status = 'in_progress';` — structurally identical to `idx_free_mock_sets_one_draft`/`idx_free_mock_sets_one_published` (§2.2).
- Index `(user_email, scenario_id, status)` for "list my attempts for scenario X."
- Index `(scenario_version_id)` for "how many attempts exist against version Y" (operational/reporting queries, e.g. before considering ever deprecating a version).

### 5.4 Lifecycle (minimum states for V1)

```
in_progress ──(complete)──> completed   [terminal, immutable]
in_progress ──(abandon)───> abandoned   [terminal, immutable]
```

- **Recommendation.** Exactly these three states. No `expired`, no `paused`,
  no `archived` — the task explicitly asks for "the minimum lifecycle states
  required for V1," and nothing in the engine or the existing app requires more.
  A future auto-abandon-after-N-days policy (if wanted) is an *operational*
  decision about *when* to call `abandon_scenario_attempt_v1`, not a new status.
- `completed`/`abandoned` are **permanently terminal** — never reopened. A
  learner who wants to redo a scenario always gets a **new** attempt row
  (§7 explains why this composes safely with the one-active-attempt index).
- **No hard delete** is defined or needed for V1 (per the task). `ON DELETE
  RESTRICT` on every inbound FK additionally makes an accidental cascading
  delete structurally impossible even if someone tried.

### 5.5 Immutability enforcement (database, not Python)

A single `BEFORE UPDATE` trigger, `guard_scenario_attempt_mutation_v1()` /
`trg_guard_scenario_attempt_mutation`, modeled directly on `V66`'s
`guard_scenario_version_immutability_v1()`:

- Rejects any change to `user_email`, `scenario_id`, `scenario_version_id`,
  `engine_version`, `canonical_content_sha256`, or `started_at` — these are
  "immutable ownership," "immutable `scenario_version_id`," and "immutable
  attempt start identity" from the task's requirements, enforced unconditionally
  (not even the RPCs are allowed to change them — there is no legitimate reason
  to ever touch them after `INSERT`).
- Rejects any change at all once `OLD.status IN ('completed', 'abandoned')`
  — full immutability, no administrative-field exception. **Recommendation:**
  V1 needs no "narrowly justified administrative field" exception; if a future
  admin correction workflow is genuinely needed (e.g. fixing a mis-recorded
  `ending_id` after a proven engine bug), it should be a new, explicitly
  reviewed RPC with its own audit trail — not a quiet carve-out in this trigger.
- Permits the two one-way transitions
  (`in_progress -> completed`, `in_progress -> abandoned`) **only** when a
  transaction-local guard is set, following the exact `certbound.publish_
  scenario_version_guard` pattern: `certbound.attempt_lifecycle_guard` set via
  `set_config(...)` to the attempt's own `id`, set only inside `complete_
  scenario_attempt_v1`/`abandon_scenario_attempt_v1`/the terminal path of
  `submit_scenario_decision_v1` immediately before the transitioning `UPDATE`.
  Same honestly-documented scope as `V67`: an application/RPC mutation-boundary
  safeguard for normal `service_role` API usage, not a defense against
  arbitrary trusted SQL in the same transaction.
- Permits ordinary `in_progress` updates that only touch `current_scene_id`,
  `decision_count`, `updated_at` (the per-decision cache bump) with **no**
  guard requirement — this is routine, high-frequency traffic and does not
  cross a lifecycle boundary.

---

## 6. `public.scenario_decisions`

### 6.1 Purpose

The append-only, canonical, ordered record of what a learner actually chose.
Per §2.3, this table **alone** (plus the pinned `scenario_versions.content_
snapshot`) is sufficient to perfectly reconstruct a run via `replay_scenario_
run`. Everything else on this table is audit/reporting convenience, not a
second source of truth.

### 6.2 Columns

| Column | Type | Null | Default | Canonical / Audit / Non-stored |
|---|---|---|---|---|
| `id` | `uuid` | NOT NULL | `gen_random_uuid()` | Surrogate PK. |
| `attempt_id` | `uuid` | NOT NULL | — | FK to `scenario_attempts.id`. |
| `sequence_number` | `int` | NOT NULL | — | **Canonical.** Matches `ScenarioDecisionInput.sequence_number` exactly. |
| `scene_id` | `text` | NOT NULL | — | **Canonical.** Matches `ScenarioDecisionInput.scene_id`. |
| `option_id` | `text` | NOT NULL | — | **Canonical.** Matches `ScenarioDecisionInput.option_id`. |
| `domain_id` | `text` | NOT NULL | — | **Audit.** Copied from `ScenarioDecisionRecord.domain_id` as computed by Python; cheap denormalization for reporting queries (e.g. domain accuracy) without replaying every attempt. Re-derivable at any time via replay; never trusted over a fresh replay for scoring decisions. |
| `is_correct` | `boolean` | NOT NULL | — | **Audit.** Same rule as `domain_id`. |
| `next_scene` | `text` | NOT NULL | — | **Audit.** Copied from `ScenarioDecisionRecord.next_scene`; lets `complete_scenario_attempt_v1` cheaply verify "was this actually the terminal decision" (`next_scene = 'EVALUATE_ENDING'`, `TERMINAL_SENTINEL`) without a full replay inside SQL. |
| `idempotency_key` | `uuid` | NOT NULL | — | Python-generated UUIDv4 (§7). |
| `request_fingerprint` | `text` | NOT NULL | — | Deterministic fingerprint of `(sequence_number, scene_id, option_id)` (§7). Conflict-detection evidence, not itself replay input. |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Audit timestamp only; ordering is by `sequence_number`, never by `created_at` (clock skew/retry timing must never affect replay order). |

**Explicitly NOT stored** (per the task's "redundant data that should not be
stored" requirement):

- `state_before` / `state_after` per decision — fully derivable by replaying
  decisions `1..N` against the pinned content in milliseconds; storing it on
  every row would duplicate the engine's own state-machine output at every
  step for no resume/audit benefit beyond what `scenario_attempts`'s single
  cached `final_state_snapshot` (populated once, at completion) already gives.
- `flags_after` per decision — same reasoning as state.
- The scenario content itself, or any subset of it — attempts/decisions
  reference `scenario_version_id`; the immutable `content_snapshot` lives
  exactly once, on `scenario_versions` (already true per `V66`).
- A raw request payload / HTTP metadata — not needed for replay or ownership;
  out of scope for a learning product's decision ledger.

### 6.3 Keys, constraints, indexes

- `PRIMARY KEY (id)`.
- `FOREIGN KEY (attempt_id) REFERENCES scenario_attempts (id) ON DELETE RESTRICT`.
- `CONSTRAINT scenario_decisions_sequence_number_positive CHECK (sequence_number >= 1)`.
- `CONSTRAINT scenario_decisions_scene_id_normalized CHECK (scene_id = BTRIM(scene_id) AND scene_id <> '')`.
- `CONSTRAINT scenario_decisions_option_id_normalized CHECK (option_id = BTRIM(option_id) AND option_id <> '')`.
- `CONSTRAINT scenario_decisions_domain_id_normalized CHECK (domain_id = BTRIM(domain_id) AND domain_id <> '')`.
- `CONSTRAINT scenario_decisions_next_scene_normalized CHECK (next_scene = BTRIM(next_scene) AND next_scene <> '')`.
- `CONSTRAINT scenario_decisions_fingerprint_format CHECK (request_fingerprint ~ '^[0-9a-f]{64}$')` — SHA-256 hex, same format convention as `canonical_content_sha256` (`V66` §2).
- **`UNIQUE (attempt_id, sequence_number)`** — the core ordering/no-duplicate/no-gap-at-the-constraint-level guarantee (gaps are still separately prevented by the RPC's insertion logic, §8; this constraint prevents *duplicates and out-of-order re-insertion* at the storage layer even if application logic had a bug).
- **`UNIQUE (attempt_id, idempotency_key)`** — idempotency scope (§7).
- Index `(attempt_id, sequence_number)` — already provided by the first unique constraint's implicit index; supports `ORDER BY sequence_number` for full-history reads with no extra index needed.

### 6.4 Immutability enforcement (append-only)

**Recommendation — grant-layer enforcement, not just a trigger.**
`scenario_decisions` has no legitimate `UPDATE` or `DELETE` path at all, ever,
by design (there is no "draft decision" concept the way there is a "draft
scenario version"). So:

- `service_role` is granted **`SELECT, INSERT` only** on `scenario_decisions`
  — no `UPDATE`, no `DELETE` grant exists at all, at the grant layer, which is
  a stronger and simpler guarantee than a trigger (a trigger can be dropped or
  disabled by a sufficiently privileged actor; a privilege that was never
  granted cannot be "used" by `service_role` at all without a separate `GRANT`
  first).
- **Defense in depth**, matching this repository's own `V66 -> V67` lesson
  (grants alone still need a second gate because grants can be *found* to be
  wrong in production, as `V67` had to fix): a `BEFORE UPDATE OR DELETE`
  trigger, `guard_scenario_decision_immutability_v1()` /
  `trg_guard_scenario_decision_immutability`, unconditionally rejects both
  operations — no guard variable, no exception, because none is ever needed.
  This directly satisfies "append-only `scenario_decisions`" and "immutable
  idempotency records" without relying only on Python.

---

## 7. Concurrency, One Active Attempt, and Idempotency

### 7.1 One active attempt per (learner, scenario version)

- **Recommendation.** Enforced by `idx_scenario_attempts_one_in_progress`
  (§5.3), a database constraint, not an application check — the same class of
  guarantee `idx_free_mock_sets_one_draft`/`_one_published` already provides
  elsewhere in this repository.
- **Concurrent start requests / duplicate browser submissions:**
  `start_or_resume_scenario_attempt_v1` (§9.1) first attempts
  `INSERT ... ON CONFLICT (user_email, scenario_version_id) WHERE status =
  'in_progress' DO NOTHING` (Postgres supports targeting a specific partial
  unique index this way), then unconditionally re-`SELECT`s the row for
  `(user_email, scenario_version_id, status = 'in_progress')`. Exactly one of
  two concurrent callers' `INSERT`s wins the index; the other's `INSERT` becomes
  a no-op and its subsequent `SELECT` deterministically returns the winner's
  row. Both callers therefore always return the same, single `in_progress`
  attempt — never a duplicate, never an error surfaced to the caller for the
  ordinary "double-click start" case.
- **Transaction retries:** the RPC is written so that re-executing the entire
  function body from scratch after any transient failure (e.g. a dropped
  connection before the client saw the response) is always safe — it is
  naturally idempotent for the *start* operation because "does an in_progress
  row already exist" is re-checked fresh on every call; no separate retry-token
  is needed for *starting* an attempt (only for *decisions*, §7.2, where the
  operation is not naturally idempotent because it appends new data).
- **Start-versus-resume:** there is no separate "resume" RPC — `start_or_
  resume_scenario_attempt_v1` **is** both operations, distinguished only by
  whether an existing `in_progress` row for `(user_email, scenario_version_id)`
  is found. This avoids a race between a hypothetical separate "does one exist"
  check and a separate "create" call. A caller cannot "resume" an attempt for a
  *different, no-longer-current* `scenario_version_id` through this RPC by
  design — resuming an old in-progress attempt whose scenario has since been
  republished still targets that attempt's own originally pinned
  `scenario_version_id` (§5.2), never the scenario's current pointer, because
  the RPC takes the resolved `scenario_version_id` as its lookup key, not the
  scenario's "current" pointer, once an attempt already exists for an older
  pinned version. (New attempts, with no existing `in_progress` row, are always
  created against whatever `scenarios.current_published_version_id` resolves
  to *at that moment* — see §9.1.)
- **Retakes:** starting a **new** attempt for a scenario version that already
  has an existing `completed`/`abandoned` attempt for the same learner is
  always allowed and always creates a new row — the partial index only
  constrains `in_progress` rows, so completed/abandoned history never blocks a
  retake, and unlimited completed attempts (already decided in
  `SCENARIO_PERSISTENCE_ARCHITECTURE.md` §3) fall out of this naturally with no
  extra logic.

### 7.2 Decision-submission idempotency

- **Uniqueness scope — Recommendation.** `(attempt_id, idempotency_key)`, not
  global. A UUIDv4 has effectively zero real collision risk, but scoping to
  `attempt_id` (i) matches the existing `billing_checkout_claims` precedent's
  intent (uniqueness scoped to the entity the key protects a mutation on), and
  (ii) means a key value is never meaningful outside the one attempt it was
  issued for, which simplifies reasoning about retries.
- **Safe retry vs. conflicting reuse — Recommendation.** `submit_scenario_
  decision_v1` first looks up `(attempt_id, idempotency_key)` (inside the same
  locked transaction, before any status/sequence validation — matching
  `claim_billing_checkout_v1`'s "look up existing row before validating"
  order, §2.2):
  - **Not found:** proceed with normal validation and insertion (§8).
  - **Found, and its stored `request_fingerprint` equals a freshly computed
    fingerprint of the caller's `(p_sequence_number, p_scene_id, p_option_id)`:**
    this is a **safe retry** (e.g. a dropped HTTP response after the database
    write actually committed). Return the already-committed row's result
    unchanged — no new row is inserted, no state is re-derived, no error is
    raised.
  - **Found, but the fingerprint differs:** this is a **conflicting reuse** of
    the same key with different input (a client bug, or a key accidentally
    reused across two different decisions). Raise a focused, distinguishable
    exception (e.g. `idempotency_key_conflict: ...`) and change nothing.
- **Required fingerprinting mechanism — Recommendation.** `request_fingerprint
  = sha256(sequence_number::text || ':' || scene_id || ':' || option_id)`,
  computed identically by Python (for the value it sends) and by the RPC (for
  the value it stores and later compares against) — a single, explicit,
  reviewable formula, not an opaque hash of the whole request payload. Format
  is enforced by `scenario_decisions_fingerprint_format` (§6.3), the same
  64-lowercase-hex convention `V66` already uses for `canonical_content_
  sha256`.
- **Concurrent duplicate requests** (two simultaneous calls with the *same*
  idempotency key, neither having committed yet): the RPC's row lock on the
  parent `scenario_attempts` row (`SELECT ... FOR UPDATE`, §9.2) serializes
  them — the second caller's transaction blocks until the first commits, then
  proceeds through the same "found, fingerprint matches" safe-retry path
  above. No advisory lock is additionally required for this specific race
  because the attempt-row lock already provides full serialization for that
  attempt; an advisory lock (`pg_advisory_xact_lock(hashtext(user_email ||
  scenario_version_id))`, mirroring `V45`'s pattern) is still recommended
  **inside `start_or_resume_scenario_attempt_v1`** specifically, because that
  RPC's very first action (deciding whether a row exists at all) happens
  *before* any row yet exists to lock with `FOR UPDATE`.

---

## 8. Decision Ordering

- **Sequence numbering — Recommendation.** 1-based, exactly matching
  `ScenarioDecisionRecord.sequence_number`/`ScenarioDecisionInput.sequence_
  number`. **Sequence assignment belongs inside the database RPC**, not the
  client: `submit_scenario_decision_v1` computes
  `v_next_sequence := attempt.decision_count + 1` from the row it already holds
  under `FOR UPDATE` (§9.2), and that is the value actually inserted — never a
  client-supplied sequence number taken at face value. The caller (Python) does
  still pass its own *expected* sequence number and *expected* current
  `scene_id` (because it independently knows both, having just run the engine
  locally); the RPC requires these to match its own authoritative view before
  proceeding, and rejects with a focused "sequence/scene mismatch" error if
  they do not. This gives the caller fast, clear feedback on a genuine race
  (e.g. two browser tabs) without ever trusting the caller's numbers as
  authoritative.
- **Uniqueness constraint:** `UNIQUE (attempt_id, sequence_number)` (§6.3) —
  belt-and-suspenders against ever writing two rows for the same position even
  if the locking above were somehow bypassed.
- **Expected current-scene validation:** the RPC also independently reads
  `scenario_attempts.current_scene_id` (its own cache, kept correct because
  only this RPC ever advances it, under the same row lock) and requires it to
  equal the caller's supplied expected current scene — mirroring exactly the
  check `replay_scenario_run` itself performs (`utils/scenario_engine.py` line
  447: `if run.current_scene_id != decision.scene_id`). This is *not* SQL
  reimplementing graph logic — it is SQL checking that Python's already-computed
  expectation matches the persisted cache, a pure string equality check.
- **Prevention of skipped/duplicated sequence numbers:** structurally
  impossible in the normal path, because the RPC is the only writer and always
  computes `decision_count + 1` under a row lock; the `UNIQUE (attempt_id,
  sequence_number)` constraint is the final backstop if that invariant were
  ever violated by a future code change.
- **Concurrent submissions:** the same `SELECT ... FOR UPDATE` on the parent
  attempt row (§9.2) that protects idempotency (§7.2) also fully serializes
  concurrent decision submissions for one attempt — the second caller's
  transaction blocks, then re-validates its expected sequence/scene against
  the *now-advanced* row and correctly fails with a clear mismatch error if its
  view was stale (e.g. two tabs both showing the same scene and both racing to
  submit different options) rather than silently double-advancing the attempt.

---

## 9. RPC Set (V1)

Exactly four RPCs. Reads use direct `service_role` `SELECT` (no RPC needed for
reads — see §10.3 for the ownership caveat this implies).

### 9.1 `start_or_resume_scenario_attempt_v1`

- **Parameters:** `p_user_email text`, `p_simulation_id text`.
- **Returns:** `TABLE (attempt_id uuid, scenario_id uuid, scenario_version_id
  uuid, status text, current_scene_id text, decision_count int, started_at
  timestamptz, resumed boolean)`.
- **Locks:** `pg_advisory_xact_lock(hashtext(p_user_email || ':' ||
  p_simulation_id))` first (no row exists yet to lock conventionally on the
  very first call for a learner/scenario pair); then `SELECT ... FOR UPDATE`
  on any matching `scenarios`/`scenario_versions` rows it reads.
- **Validation order:** (1) normalize/validate `p_user_email` non-empty; (2)
  resolve `scenarios` by `simulation_id = p_simulation_id`, reject if missing
  or `is_active = false`; (3) look for an existing `scenario_attempts` row
  `WHERE user_email = normalized AND scenario_id = resolved.id AND status =
  'in_progress'` — if found, that attempt's **own** `scenario_version_id` is
  authoritative for the resume path (never re-pin to "current"); (4) if none
  found, require `scenarios.current_published_version_id IS NOT NULL` (reject
  "scenario has never been published" clearly), then `INSERT ... ON CONFLICT
  (user_email, scenario_version_id) WHERE status = 'in_progress' DO NOTHING`
  pinning `scenario_version_id`, `engine_version` (a literal RPC-side constant
  matching `ENGINE_VERSION` — see §11 open decision), and `canonical_content_
  sha256` copied from the resolved `scenario_versions` row; (5) re-`SELECT` to
  return whichever row now exists.
- **Idempotency behavior:** naturally idempotent (§7.1) — no explicit
  idempotency key parameter needed for this RPC.
- **Transaction boundary:** the whole RPC is one implicit transaction (a
  single RPC call = one Postgres transaction under Supabase/PostgREST, as
  already relied upon by every existing RPC in this repository).
- **Permitted lifecycle transitions:** none directly (creates a new row in
  `in_progress`, or returns an existing one unchanged).
- **Expected failure modes:** unknown/inactive scenario; scenario has no
  published version yet; empty/invalid `user_email`.
- **Concurrency behavior:** see §7.1.

### 9.2 `submit_scenario_decision_v1`

- **Parameters:** `p_attempt_id uuid`, `p_user_email text`, `p_idempotency_key
  uuid`, `p_expected_sequence_number int`, `p_expected_current_scene_id text`,
  `p_scene_id text`, `p_option_id text`, `p_domain_id text`, `p_is_correct
  boolean`, `p_next_scene text`, `p_is_terminal boolean`, `p_ending_id text
  DEFAULT NULL`, `p_score_band text DEFAULT NULL`, `p_final_state jsonb
  DEFAULT NULL`, `p_domain_performance jsonb DEFAULT NULL`.
- **Returns:** `TABLE (decision_id uuid, attempt_id uuid, sequence_number int,
  attempt_status text, current_scene_id text, decision_count int,
  idempotent_replay boolean)`.
- **Locks:** `SELECT ... FOR UPDATE` on the `scenario_attempts` row by
  `p_attempt_id` — this single lock is what serializes §7.2 and §8 together.
- **Validation order:** (1) lock and fetch the attempt row, reject unknown
  `p_attempt_id`; (2) verify ownership: `attempt.user_email = normalize(p_user_
  email)`, reject otherwise with a focused ownership error (never leak whether
  the id exists to a non-owner beyond a generic not-found/forbidden result);
  (3) reject if `attempt.status <> 'in_progress'` (covers "no decision after
  completion/abandonment" and "no reopening"); (4) idempotency lookup by
  `(p_attempt_id, p_idempotency_key)` — if found, branch to the safe-retry or
  conflict path (§7.2) and return early without re-validating anything below;
  (5) verify `p_expected_sequence_number = attempt.decision_count + 1` and
  `p_expected_current_scene_id = attempt.current_scene_id`, reject with a
  focused sequence/scene-mismatch error otherwise (§8); (6) compute and store
  `request_fingerprint`; (7) `INSERT` the `scenario_decisions` row; (8) `UPDATE
  scenario_attempts` bumping `decision_count`, `current_scene_id` (to
  `p_next_scene`, or `NULL` if `p_is_terminal`), `updated_at`; (9) if
  `p_is_terminal`, additionally set the transaction-local `certbound.attempt_
  lifecycle_guard` and transition `status -> 'completed'`,
  `completed_at = clock_timestamp()`, and the four terminal cache columns from
  `p_ending_id`/`p_score_band`/`p_final_state`/`p_domain_performance`.
- **Idempotency behavior:** as specified in §7.2, evaluated inside the same
  locked transaction, before any other validation that could otherwise reject a
  legitimate retry for a spurious reason (e.g. a retry whose `p_expected_
  sequence_number` looks "wrong" only because the *first* attempt's write
  already committed).
- **Transaction boundary:** one RPC call = one transaction; the decision
  insert and (when terminal) the attempt completion happen atomically together
  — see §9.5 for why this is the recommended design over a separate completion
  call.
- **Permitted lifecycle transitions:** `in_progress -> in_progress` (normal
  decision) or `in_progress -> completed` (terminal decision), never anything
  else.
- **Expected failure modes:** unknown attempt; ownership mismatch; attempt not
  `in_progress`; idempotency-key conflict; sequence/scene mismatch.
- **Concurrency behavior:** see §7.2/§8.

### 9.3 `complete_scenario_attempt_v1`

- **Purpose — Recommendation:** a narrow **reconciliation** RPC, not the
  primary completion path (§9.5). Exists only to finalize an attempt whose
  terminal decision was already durably recorded (`next_scene = 'EVALUATE_
  ENDING'` on its last `scenario_decisions` row) but whose `scenario_attempts.
  status` never flipped to `completed` — e.g. the client crashed after
  `submit_scenario_decision_v1`'s `INSERT` committed but before its response
  (and thus the terminal `UPDATE` that was meant to happen in the *same*
  transaction) was ever observed. Because both writes are in one transaction in
  §9.2, this should be rare, but the reconciliation path must exist rather than
  leaving such an attempt permanently stuck as `in_progress`.
- **Parameters:** `p_attempt_id uuid`, `p_user_email text`, `p_ending_id text`,
  `p_score_band text`, `p_final_state jsonb`, `p_domain_performance jsonb`.
- **Returns:** `TABLE (attempt_id uuid, status text, completed_at
  timestamptz)`.
- **Locks:** `SELECT ... FOR UPDATE` on the attempt row.
- **Validation order:** (1) lock/fetch, reject unknown; (2) ownership check;
  (3) reject if `status <> 'in_progress'` (idempotent no-op if already
  `completed` with matching `ending_id` — same safe-retry philosophy as §7.2 —
  but a hard error if already `completed` with a **different** `ending_id`,
  or if `abandoned`); (4) require the attempt's own last `scenario_decisions`
  row (`ORDER BY sequence_number DESC LIMIT 1`) to have `next_scene =
  'EVALUATE_ENDING'` — reject otherwise ("no terminal decision recorded yet");
  (5) set the guard, transition to `completed`.
- **Idempotency behavior:** re-calling with the same `p_ending_id` on an
  already-`completed` attempt is a safe no-op; a different `p_ending_id` is a
  hard conflict (this should never legitimately happen, since the terminal
  decision's `next_scene` is fixed — a differing `p_ending_id` indicates a
  client bug, not a legitimate retry).
- **Transaction boundary:** one call, one transaction.
- **Permitted lifecycle transitions:** `in_progress -> completed` only.
- **Expected failure modes:** unknown attempt; ownership mismatch; not
  `in_progress` and mismatched; no terminal decision recorded.
- **Concurrency behavior:** same row-lock serialization as §9.2.

### 9.4 `abandon_scenario_attempt_v1`

- **Parameters:** `p_attempt_id uuid`, `p_user_email text`.
- **Returns:** `TABLE (attempt_id uuid, status text, abandoned_at
  timestamptz)`.
- **Locks:** `SELECT ... FOR UPDATE` on the attempt row.
- **Validation order:** (1) lock/fetch, reject unknown; (2) ownership check;
  (3) reject if `status <> 'in_progress'` (idempotent no-op if already
  `abandoned`; hard error if `completed` — a completed attempt can never be
  abandoned after the fact); (4) set the guard, transition to `abandoned`,
  `abandoned_at = clock_timestamp()`.
- **Idempotency behavior:** re-calling on an already-`abandoned` attempt is a
  safe no-op (no idempotency *key* parameter needed — the operation itself is
  naturally idempotent given the identical target state).
- **Transaction boundary:** one call, one transaction.
- **Permitted lifecycle transitions:** `in_progress -> abandoned` only.
- **Expected failure modes:** unknown attempt; ownership mismatch; already
  `completed`.
- **Concurrency behavior:** row-lock serialized against a concurrent
  `submit_scenario_decision_v1`/`complete_scenario_attempt_v1` call for the
  same attempt — whichever transaction commits first wins; the loser's
  transition-precondition check (`status <> 'in_progress'`) then correctly
  fails the loser with a clear error instead of corrupting state.

### 9.5 Completion design decision (explicit, per the task's requirement)

**Recommendation: completion happens primarily *inside* `submit_scenario_
decision_v1`** (its `p_is_terminal` branch, §9.2), **not** through a mandatory
separate call. Rationale: the alternative (always requiring a second,
separate `complete_scenario_attempt_v1` call after the terminal decision) opens
a real window where the terminal decision is durably recorded but the attempt
is still `in_progress` if the second call never arrives — exactly the failure
mode `complete_scenario_attempt_v1` then has to exist to repair anyway. Folding
completion into the same transaction as the terminal decision removes that
window in the common case; `complete_scenario_attempt_v1` remains only as the
narrow, idempotent repair path for the rare case where even that combined
write's *response* was lost after it committed.

### 9.6 Read access (no RPC)

- **Recommendation.** `service_role` `SELECT` directly on both tables (granted
  in §10) is sufficient for reads — there is no scoring, ordering, or
  concurrency concern on a pure read. Python is responsible for **always**
  filtering by the caller's own normalized `user_email` in every read query
  (`WHERE user_email = :normalized_email`) before returning any row to a
  specific learner — see §10.3 for why this is a real, explicit requirement
  and not automatic.

---

## 10. Database Security

Follows `V67` exactly; nothing here loosens it.

### 10.1 RLS

- `ALTER TABLE scenario_attempts ENABLE ROW LEVEL SECURITY;` /
  `ALTER TABLE scenario_decisions ENABLE ROW LEVEL SECURITY;` — enabled, **zero
  policies**, exactly like `scenarios`/`scenario_versions` after `V67`'s
  explicit "must have zero `pg_policies` rows" hardening. No `auth.uid()`
  policy is added, for the same reason `auth.uid()` is not the ownership
  identifier at all (§2.1).

### 10.2 Grants

| Table/Function | `PUBLIC`/`anon`/`authenticated` | `service_role` |
|---|---|---|
| `scenario_attempts` | none | `SELECT, INSERT, UPDATE` — **no `DELETE`** (no hard delete in V1, §5.4) |
| `scenario_decisions` | none | `SELECT, INSERT` — **no `UPDATE`, no `DELETE`** (append-only, §6.4) |
| `start_or_resume_scenario_attempt_v1` | `EXECUTE` revoked | `EXECUTE` |
| `submit_scenario_decision_v1` | `EXECUTE` revoked | `EXECUTE` |
| `complete_scenario_attempt_v1` | `EXECUTE` revoked | `EXECUTE` |
| `abandon_scenario_attempt_v1` | `EXECUTE` revoked | `EXECUTE` |

`service_role` needs direct table `SELECT`/`INSERT`/`UPDATE` (on
`scenario_attempts`) and `SELECT`/`INSERT` (on `scenario_decisions`) — **not**
only RPC `EXECUTE` — because: (a) reads (§9.6) go straight to the tables, and
(b) the RPCs themselves run `SECURITY INVOKER` (§10.3), so the RPC's own SQL
statements execute with the *caller's* privileges, meaning `service_role`
itself must hold the underlying table grants for the RPC's `INSERT`/`UPDATE`
statements to succeed at all — exactly the same reasoning already documented
in `V66`'s migration header for `publish_scenario_version_v1`.

### 10.3 RPC security model and the read-access ownership caveat

- **Recommendation.** `SECURITY INVOKER` on all four RPCs, `SET search_path =
  public, pg_catalog` on all four, matching `V66`/`V67` with no exception —
  nothing about attempts/decisions needs `SECURITY DEFINER`, and this
  repository has already explicitly decided (`SCENARIO_PERSISTENCE_ARCHITECTURE.
  md` §8) to prefer `SECURITY INVOKER` "unless a specific repository constraint
  proves otherwise"; no such constraint exists here.
- **Important caveat, stated explicitly because it is easy to miss:**
  `service_role` bypasses RLS, and reads in this design (§9.6) go directly to
  the tables rather than through an RPC. That means the **only** thing standing
  between "learner A's Python session" and "learner B's attempt row" on a read
  is Python remembering to add `WHERE user_email = :normalized_email` to every
  query. This is not a new risk introduced here — it is the same model
  `exam_attempts`/`question_attempts` already operate under today (§2.2) — but
  unlike writes (enforced by the RPC's explicit ownership check, §9.2–§9.4), a
  read has **no database-enforced ownership boundary at all**. This is called
  out as a residual risk in §16, not silently accepted.

### 10.4 Preconditions and atomicity

Following `V67`'s pattern exactly: the proposed migration (§14) opens with an
explicit precondition block (fails atomically if `scenarios`, `scenario_
versions`, or `publish_scenario_version_v1` are missing — this feature is
meaningless without them) and wraps the entire file in `BEGIN; ... COMMIT;`.

---

## 11. Immutability — Consolidated Summary

| Requirement | Mechanism |
|---|---|
| Append-only `scenario_decisions` | No `UPDATE`/`DELETE` grant to `service_role` **and** `trg_guard_scenario_decision_immutability` (§6.4) |
| Completed `scenario_attempts` immutable | `trg_guard_scenario_attempt_mutation` rejects all changes once `status IN ('completed','abandoned')` (§5.5) |
| Abandoned `scenario_attempts` immutable | Same trigger, same rule |
| Immutable ownership (`user_email`) | Same trigger, unconditional column-level rejection |
| Immutable `scenario_version_id` | Same trigger, unconditional column-level rejection |
| Immutable attempt start identity (`started_at`, etc.) | Same trigger, unconditional column-level rejection |
| Immutable idempotency records | Covered by decision append-only rules above — an idempotency key and its fingerprint live only on an immutable `scenario_decisions` row, never updated in place |

None of this relies only on Python, per the task's explicit requirement — every
row above is a database-level `CHECK`, `UNIQUE`, `FOREIGN KEY`, grant absence,
or trigger, independent of any application code path.

---

## 12. Migration Plan (proposed, not created)

- **Recommendation — next migration identity: `V68`.** `V66`/`V67` are the two
  most recent Scenario Simulator migrations; `V68` is the next distinct feature
  number for this repository's convention (one number per feature wave, as
  already discussed when `V64` was found to already belong to an unrelated,
  previously-applied Sales Cloud Consultant migration during `SIM-PERSIST-02`).
  Proposed filenames (not created):
  - `supabase/migrations/<timestamp>_v68_scenario_attempt_persistence_foundation.sql`
  - `supabase/tests/v68_scenario_attempt_persistence_schema_preflight.sql`
  - `supabase/tests/v68_scenario_attempt_persistence_verification.sql`
- **Migration file purpose.** Create `scenario_attempts`/`scenario_decisions`
  (tables, constraints, indexes, both immutability triggers) and the four RPCs
  in §9, with grants exactly as §10.2.
- **Preflight requirements.** Read-only script confirming: `scenarios`,
  `scenario_versions`, `publish_scenario_version_v1` already exist (this
  feature's hard prerequisite); neither new table nor any of the four new RPC
  names, nor either new trigger function, already exists; report (informational
  only, non-blocking) the current row counts of `scenarios`/`scenario_versions`
  so an operator can sanity-check "is this the same production project I
  think it is" before applying; report whether `supabase_migrations.schema_
  migrations` exists (it currently does not — see below) purely for operator
  context, never as a blocking condition.
- **Migration requirements.** Explicit precondition block (§10.4); `BEGIN; ...
  COMMIT;` atomicity; every object named and commented exactly as specified in
  §5–§9; no data backfill (there is no pre-existing data to backfill); no
  touching of `scenarios`/`scenario_versions`/`publish_scenario_version_v1`
  beyond reading them in the precondition check and referencing them in new
  foreign keys.
- **Verification requirements.** A `BEGIN; ... ROLLBACK;` transactional script
  exercising: happy-path start -> N decisions -> terminal decision -> completed;
  resume of a partially-decided attempt; one-active-attempt enforcement
  (concurrent-start simulation via two sequential `INSERT ... ON CONFLICT`
  calls inside the same test transaction); idempotent retry (same key, same
  input) returns the original result with no second row; conflicting reuse
  (same key, different input) raises the focused conflict error; sequence/scene
  mismatch rejected; decision on a `completed`/`abandoned` attempt rejected;
  direct `UPDATE`/`DELETE` of a `scenario_decisions` row rejected; direct
  `UPDATE` of a `completed`/`abandoned` `scenario_attempts` row rejected; grant
  assertions matching §10.2 exactly (mirroring the `V67` verification script's
  own structure and its focused-exception-matching lesson from
  `SIM-PERSIST-03A` — broad `WHEN OTHERS` must never count as proof of correct
  behavior).
- **Rollback considerations.** These are brand-new tables with no dependents
  yet (no third table references `scenario_attempts`/`scenario_decisions` in
  V1) — a rollback migration, if ever needed before any real learner data
  exists, is a straightforward `DROP FUNCTION`/`DROP TRIGGER`/`DROP TABLE` in
  reverse dependency order. Once real attempt data exists in production, this
  changes completely: these tables must never be rolled back by dropping them
  (that would destroy learner history), so any post-launch fix must be a
  forward-only corrective migration, exactly the `V66 -> V67` pattern this
  repository already has direct, recent experience with.
- **Production application risk — explicitly named, mirroring `V67`'s own
  origin story:** the same operational risk that produced `V67` exists again
  here — if this migration is ever manually applied via the SQL Editor instead
  of a tracked pipeline, its actual grants must be independently re-verified in
  production immediately, not assumed to match the file. Given `supabase_
  migrations.schema_migrations` still does not exist in production (per this
  task's stated context) and migration-history onboarding remains a separate,
  explicitly deferred project-wide task (not touched here), that same
  manual-application risk should be assumed to still apply to whatever `V68`
  eventually becomes. This document does not initialize or repair migration
  history.

---

## 13. Assumptions (not verifiable from the repository as inspected)

1. Python will compute `p_domain_id`/`p_is_correct`/`p_next_scene`/terminal
   fields via `utils/scenario_engine.py` immediately before calling
   `submit_scenario_decision_v1`, and will pass exactly those computed values
   — this design assumes that integration is built faithfully; it cannot be
   verified from the engine or SQL alone, since no such Python persistence
   glue exists yet in this repository.
2. `p_user_email` reaching every RPC will always have been extracted from a
   signature-verified session (`verify_signed_session`) by the calling code,
   never a raw, unverified request field — this is a convention this design
   depends on but cannot enforce from inside SQL itself.
3. No existing Streamlit page, worker, or script currently reads/writes an
   `exam_attempts`-style table under a name resembling `scenario_attempts` —
   confirmed absent via repository search as part of this task, but a future
   naming collision with an unrelated new feature is not something this
   document can prevent.
4. `scenario_versions.content_snapshot` (the immutable JSONB runtime document)
   is assumed to already be correctly populated for at least one published
   version of any scenario a learner might start against — this design does
   not re-verify the `V66`/`V67` publication contract, only depends on it.

## 14. Explicitly Deferred Work

- Any Creative Studio schema (companies, characters, dialogue, images).
- `auth.uid()`-based RLS policies or any Supabase Auth migration.
- A/B testing, experiment cohorts, multi-version-serving.
- Migration-history (`supabase_migrations.schema_migrations`) initialization
  or repair.
- An admin-correction workflow/RPC for completed attempts (§5.5 explicitly
  declines to add a quiet carve-out for this in V1).
- A rollback/current-version-reselection RPC for `scenarios.current_published_
  version_id` (already deferred in `V67`; unaffected by this document).
- Auto-abandon-after-timeout tooling (an operational job that would simply
  call the already-defined `abandon_scenario_attempt_v1`, not a schema change).

## 15. Open Questions Worth Resolving Before Implementation

Called out only where they could materially change security, integrity, or
implementation, per the task's instruction:

1. **Where does `ENGINE_VERSION` get compared?** This design pins
   `scenario_attempts.engine_version` from Python at start time but does not
   propose a SQL-side check that a *resumed* attempt's pinned `engine_version`
   still matches the currently-deployed engine before accepting a new decision
   — that comparison is assumed to happen in Python (which already imports
   `utils/scenario_engine.py` directly and can compare `ENGINE_VERSION`
   trivially) before it ever calls `submit_scenario_decision_v1`. If the
   engine's replay semantics ever change in a way that is not backward
   compatible for in-flight attempts, this needs an explicit decision (likely:
   force-abandon incompatible in-progress attempts on deploy) that is out of
   scope for this document but should not be forgotten.
2. **Exact wording of RPC exception labels** (e.g.
   `idempotency_key_conflict`, `sequence_mismatch`, `scene_mismatch`,
   `attempt_not_in_progress`, `ownership_mismatch`) is a naming decision for
   implementation time, not fixed here — but per the `SIM-PERSIST-03A` lesson,
   whatever labels are chosen must be used consistently between the RPC's
   `RAISE EXCEPTION` messages and the verification script's focused matching
   (never broad `WHEN OTHERS`).

## 16. Remaining Risks

- **Read-boundary ownership (§10.3)** is the single largest residual risk:
  it depends entirely on disciplined Python query construction, with zero
  database-level backstop, exactly like the existing `exam_attempts` pattern.
  A future hardening pass could add a thin "read" RPC (`SECURITY INVOKER`,
  taking `p_user_email` and filtering server-side) if this is judged
  insufficient — deliberately not proposed as mandatory here, to avoid adding
  RPC surface area beyond what this task's V1 scope actually requires.
- **Manual production application risk (§12)** — identical in kind to what
  produced the `V67` hotfix; the same post-application verification discipline
  must be repeated for whatever migration actually implements this design.
- **Transaction-local guard's honestly-scoped limitation (§5.5)** carries
  forward unchanged from `V66`/`V67`: it is not a defense against an actor with
  arbitrary trusted SQL access in the same transaction as a legitimate RPC call.
- **Engine-version compatibility across a deploy (§15.1)** is not resolved by
  this document and could allow an in-flight attempt to be resumed against a
  changed engine if Python-side version checking is ever skipped.

---

*This document is architecture and analysis only. No SQL was executed, no
migration was created, and no other file was modified while producing it.*
