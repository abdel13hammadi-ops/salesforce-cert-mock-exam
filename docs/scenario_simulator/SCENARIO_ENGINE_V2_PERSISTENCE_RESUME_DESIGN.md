# SCENARIO_ENGINE_V2 — Production Persistence and Resume Design

**Task ID:** SIM-PERSIST-V2-01
**Model:** Sonnet High
**Date:** 2026-07-31
**Baseline:** `6136673` — Complete Scenario Engine V2 vertical slice
**Scope:** Architecture and contract design only. No migration, RPC, application runtime, or UI code is created or modified by this document.

---

## 0. Executive recommendation

**Reuse the existing, already-hardened V68 persistence foundation almost unchanged.** `public.scenario_attempts`, `public.scenario_decisions`, and all four RPCs (`start_or_resume_scenario_attempt_v1`, `get_scenario_attempt_v1`, `submit_scenario_decision_v1`, `abandon_scenario_attempt_v1`) are already engine-version-parameterized (`engine_version text`, `scenario_content_sha256 text`, one opaque `jsonb` snapshot column) and were never actually coupled to Engine V1 at the SQL layer. Engine V2 attempts are dispatched by `engine_version = 'SCENARIO_ENGINE_V2'` on the same tables, through the same RPCs, alongside Engine V1 attempts, with **zero table or RPC-behavior changes** and **exactly one small, additive, backward-compatible RPC parameter** (`p_attempt_id`, described in §2) needed to resolve one genuine identity-ordering problem unique to Engine V2's deterministic option-order algorithm (§17 of `SCENARIO_SCHEMA_1_1_0_SPEC.md`).

This design is a concrete elaboration of **`SCENARIO_SCHEMA_1_1_0_SPEC.md` §19 "Persistence and replay boundary"**, which is already normative and already committed (`6136673`). Where this document adds implementation detail beyond §19, it is elaboration, not a contradiction; where anything below would conflict with §19, §19 wins and this document must be corrected, not the other way around.

| Decision requirement (task) | Recommendation |
|---|---|
| 1. Reuse existing tables or create new | **Reuse** `scenario_attempts` + `scenario_decisions` verbatim (§1) |
| 2. Persist full snapshots or replay decisions | **Decisions are authoritative; the stored snapshot envelope is a verified, server-computed cache** — never trusted over a fresh replay (§3, §19.2 of the spec) |
| 3. Store option orders or recompute | **Both** — recomputed as authoritative on every replay; cached per-scene inside the envelope for cheap reads and verified (fail-closed) against a fresh recomputation on every resume (§10) |
| 4. Store final outcome only or full debrief | **Store final outcome/terminal summary only; recompute full debrief trace on demand** via `build_debrief_trace` (§11, §13) |
| 5. RPC vs. direct client writes | **RPC only** (existing four V68 RPCs) — no direct table writes, no new RPCs (§9, §12) |
| 6. Concurrency mechanism | **Row lock (`SELECT ... FOR UPDATE`) + sequence/scene compare-and-swap + whole-envelope equality**, identical to the already-hardened V68 mechanism (§6) |
| 7. Idempotency mechanism | **Client-generated UUIDv4 idempotency key + server-computed request fingerprint**, identical to the already-hardened `utils/scenario_persistence.py` mechanism, reused via a new V2 adapter module (§5, §16) |
| 8. Engine V1 coexistence model | **Shared generic persistence with explicit `engine_version` dispatch** on the same tables/RPCs (§15) |
| 9. Is a migration required | **Yes, but minimal**: one additive, backward-compatible RPC parameter (`CREATE OR REPLACE FUNCTION`); zero table/column changes; zero breaking changes for Engine V1 (§2, §18) |
| 10. Exact next implementation task | **Slice A** — write the migration draft + `utils/scenario_persistence_v2.py` design spec for independent review (§19, Slice A) |

---

## 1. Existing persistence architecture assessment

### 1.1 What exists today (Fact, from direct inspection)

| Layer | File(s) |
|---|---|
| Scenario definitions (immutable, versioned) | `supabase/migrations/20260718170000_v66_scenario_definition_persistence_foundation.sql`, hardened by `…v67_harden_scenario_definition_security.sql` |
| Scenario attempts/decisions (learner runs) | `supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql` |
| Python persistence adapter (Engine V1 only) | `utils/scenario_persistence.py` |
| Learner orchestration (Engine V1, BA-201) | `utils/scenario_learner_controller.py` |
| Engine V1 (pure compute, no I/O) | `utils/scenario_engine.py` |
| Engine V2 (pure compute, no I/O) | `utils/scenario_engine_v2.py` |
| Prior V1 persistence design record | `scenario_content/docs/SCENARIO_ATTEMPT_PERSISTENCE_DESIGN.md` (SIM-PERSIST-04A→04F) |

### 1.2 `public.scenario_attempts` (as implemented, V68)

```
id                        uuid PRIMARY KEY DEFAULT gen_random_uuid()
user_email                text NOT NULL            -- ownership, lower(btrim)
scenario_id               uuid NOT NULL            -- FK scenarios.id
scenario_version_id       uuid NOT NULL            -- FK (scenario_id, id) -> scenario_versions, immutable
status                    text NOT NULL DEFAULT 'in_progress'   -- in_progress | completed | abandoned
current_scene_id          text NULL                -- cache
next_sequence_number      integer NOT NULL DEFAULT 1            -- cache
serialized_engine_state   jsonb NOT NULL           -- ONE opaque snapshot envelope (cache)
scenario_content_sha256   text NOT NULL            -- ^[0-9a-f]{64}$, pinned at creation
engine_version            text NOT NULL            -- pinned at creation
started_at / updated_at / completed_at / abandoned_at   timestamptz
terminal_ending_id        text NULL
terminal_result_snapshot  jsonb NULL
```

Unique partial index: at most one `in_progress` row per `(user_email, scenario_version_id)`.

### 1.3 `public.scenario_decisions` (as implemented, V68)

```
id                    uuid PRIMARY KEY DEFAULT gen_random_uuid()
attempt_id            uuid NOT NULL              -- FK scenario_attempts.id
sequence_number       integer NOT NULL           -- canonical
idempotency_key       uuid NOT NULL
request_fingerprint   text NOT NULL              -- 64 lowercase hex
expected_scene_id     text NOT NULL              -- canonical
selected_option_id    text NOT NULL              -- canonical
state_before          jsonb NOT NULL             -- envelope snapshot immediately before this decision
state_after           jsonb NOT NULL             -- envelope snapshot immediately after this decision
resulting_scene_id    text NULL                  -- NULL only for a terminal decision
is_terminal           boolean NOT NULL
terminal_ending_id    text NULL
created_at            timestamptz NOT NULL DEFAULT now()
UNIQUE (attempt_id, sequence_number)
UNIQUE (attempt_id, idempotency_key)
```

**Important, favorable finding:** the table's *implemented* shape (per the migration and `SCENARIO_ATTEMPT_PERSISTENCE_DESIGN.md` §0 point 4) **dropped** the originally-proposed `domain_id`/`is_correct`/`next_scene` audit columns. There is **no Engine-V1-specific column** on either table. Every column on both tables is engine-agnostic: identity (`scenario_id`, `scenario_version_id`, `engine_version`, `scenario_content_sha256`), lifecycle (`status`, timestamps), ordering (`sequence_number`), submission identity (`selected_option_id`, `expected_scene_id`), idempotency (`idempotency_key`, `request_fingerprint`), and one opaque `jsonb` snapshot pair per decision. **Additive columns are not merely sufficient — no schema change to either table is required at all.**

### 1.4 What is Engine-V1-specific (and must not be reused as-is)

Only the **Python adapter module**, `utils/scenario_persistence.py`, is Engine-V1-coupled, and only in one function:

- `validate_serialized_engine_state(...)` hard-codes `REQUIRED_SERIALIZED_STATE_KEYS = {simulationId, version, canonicalContentSha256, engineVersion, currentSceneId, state, flags, decisionHistory, isComplete, terminalResult}` — this is Engine V1's exact `serialize_run_snapshot(...)` shape (missing `schemaVersion`, `counters`, `optionDisplayOrderByScene`, which Engine V2 requires per spec §19.2).

Everything else in that module is **already engine-agnostic** and reusable as a *pattern* (not by direct import coupling, since the module deliberately has no dependency on either engine — see its own docstring): `generate_idempotency_key()`, `compute_request_fingerprint(...)` (operates on arbitrary `Mapping[str, Any]` `state_before`/`state_after`), the four RPC-calling functions, every `_require_*` strict-type helper, and — critically — the **SQL RPCs themselves**, which only ever inspect seven specific top-level JSON keys inside the envelope (`simulationId`, `version`, `canonicalContentSha256`, `engineVersion`, `currentSceneId`, `isComplete`, `terminalResult`) and treat everything else as an opaque, whole-object-compared blob.

**Conclusion:** the RPCs and tables can safely support Engine V2 unmodified (module 1.5's one addition aside). Only a **new, sibling Python adapter module**, mirroring the existing one's rigor, is needed for Engine V2's own envelope shape. This satisfies "do not recommend a new table merely for cleanliness" — no new table is recommended, and the one new Python module exists only because the *validation contract* (which JSON keys are required), not the *storage mechanism*, differs.

### 1.5 Existing Engine V1 usage (for contrast, unaffected by this design)

`utils/scenario_learner_controller.py` (BA-201) uses a two-stage **prepare/submit** pattern:

1. `prepare_ba201_decision(...)` — pure computation (load content, fetch attempt via `get_attempt`, replay, `apply_decision`, canonicalize JSON) — **no write**. Binds a caller-supplied `idempotency_key` into an immutable `PreparedScenarioDecision`.
2. `submit_prepared_ba201_decision(...)` — the **only** write, calling `utils.scenario_persistence.submit_decision(...)` with the exact prepared payload. Safe to retry verbatim with the identical prepared object and idempotency key.

This prepare/submit split is the proven pattern this design reuses for Engine V2's failure-recovery behavior (§13).

### 1.6 Existing "exam attempt" pattern (contrast, explicitly not reused)

`exam_attempts`/`question_attempts` predate migrations, are mutated by **direct table `.insert()`/`.upsert()`**, and have **no RPC, no row lock, no true idempotency-conflict detection** (only a harmless-retry-safe `UNIQUE` + `upsert`). `SCENARIO_ATTEMPT_PERSISTENCE_DESIGN.md` already explicitly rejected this pattern for scenario attempts. This design does not revisit that rejection — Engine V2 needs strict ordering and conflict detection (§17 option order depends on an exact, frozen sequence of decisions) that the exam-attempt pattern cannot provide.

---

## 2. Attempt identity

| Question | Answer |
|---|---|
| Database attempt ID | `scenario_attempts.id` (`uuid`) |
| Stable attempt identity used by §17 deterministic option ordering | The **same value**, passed to Engine V2 as `attempt_id: str` (`str(uuid)`) |
| Are these the same value? | **Yes, by construction, not by coincidence.** See below. |
| May attempt identity ever change? | **No.** Immutable from creation (already enforced by the existing `guard_scenario_attempt_mutation_v1` trigger, which rejects any change to attempt-identity columns). |
| How do retries/new attempts get new identities? | A **new** attempt (no existing `in_progress` row for `(user_email, scenario_version_id)`) always mints a fresh UUIDv4. A **resumed** attempt keeps its original id forever. |
| How is content hash pinned at creation? | Copied from `scenario_versions.canonical_content_sha256` into `scenario_attempts.scenario_content_sha256` at `INSERT`, verified byte-for-byte identical to the value Engine V2's `ScenarioContentV2.canonical_content_sha256` reports for the loaded content. |
| How is loading a different content version later prevented? | `scenario_attempts.scenario_version_id` is immutable (existing trigger); every resume loads content via that pinned id, never via "current published version"; `verify_replay_identity_v2(...)` (already implemented) is called on every resume and fails closed on any of `simulationId`/`version`/`schemaVersion`/`canonicalContentSha256`/`engineVersion` mismatch. |

### 2.1 The identity-ordering problem this design must solve

Engine V2's `start_scenario_run_v2(content, *, attempt_id: str)` needs `attempt_id` **before** it can compute the first scene's `optionDisplayOrder` (§17 seed material includes `attemptId`). The existing `start_or_resume_scenario_attempt_v1` RPC generates the new row's `uuid` **internally**, inside SQL, and the caller never sees it until after the row exists — so Python cannot call Engine V2 first (it doesn't have an id yet) and cannot call the RPC first either (the RPC needs `p_initial_serialized_state`, which needs the engine to have already run).

**Recommendation:** add one **optional, additive, backward-compatible** parameter, `p_attempt_id uuid DEFAULT NULL`, to `start_or_resume_scenario_attempt_v1`:

- When `NULL` (every existing Engine V1 caller, unchanged): the RPC generates the id internally exactly as it does today.
- When supplied (every Engine V2 caller): the RPC uses that exact value for the new row's `id` under the existing insert-guard mechanism (`certbound.scenario_attempt_insert_guard`), instead of generating one.
- The Engine V2 start flow therefore: (1) mint `attempt_id = str(uuid.uuid4())` in Python: (2) call `start_scenario_run_v2(content, attempt_id=attempt_id)` to get the real first-scene envelope (including the true `optionDisplayOrder`); (3) call `start_or_resume_scenario_attempt_v1(..., p_attempt_id=attempt_id, p_initial_serialized_state=<that envelope>)`.
- **Resume path is unaffected**: when an `in_progress` row already exists for `(user_email, scenario_version_id)`, `p_attempt_id` (if supplied) is **ignored** — the RPC returns the existing row's own id, exactly as today. A caller must never assume its freshly-minted id will be used on a resume.

This is the **only** SQL change this design requires. It is additive (new optional parameter, default preserves current behavior exactly), does not touch any existing column, and requires no verification-script rewrite beyond adding new cases for the new parameter (§18).

---

## 3. Source-of-truth principle applied to Engine V2

Per `SCENARIO_SCHEMA_1_1_0_SPEC.md` §19.2, the persisted snapshot ("envelope") is explicitly **server-computed only** — "Client-supplied state, counters, tiers, flags, routing, outcomes, display order → rejected; server computes." This document treats the envelope as a **verified cache**, never as independently authoritative:

- **Authoritative, minimal, sufficient for full reconstruction:** the ordered `scenario_decisions` rows' `(sequence_number, expected_scene_id, selected_option_id)` triples, plus the attempt's pinned identity (`scenario_version_id` → content, `engine_version`, `scenario_content_sha256`, `id` as `attempt_id`).
- **Cache (server-computed, envelope fields):** `state`, `counters`, `flags`, `routingResolutions`, `optionDisplayOrderByScene`, `selectedVariantIdByScene`, `currentSceneId`, `isComplete`, `terminalResult`. Every one of these is **always** re-derivable in milliseconds via `replay_scenario_run_v2(content, attempt_id=..., decisions=...)` (271-test focused suite runs in ~2.5s; a single attempt's replay — at most a handful of decisions — is sub-millisecond).
- **Why cache it at all, given the task's principle favors decisions-only:** (a) `submit_scenario_decision_v1`'s existing, already-hardened concurrency mechanism performs a **whole-envelope JSONB equality check** (`state_before IS NOT DISTINCT FROM serialized_engine_state`) as an extra compare-and-swap guard beyond the sequence/scene check (§6) — reusing this exact mechanism (rather than inventing a new one) is the lowest-risk way to get proven concurrency safety; (b) resuming an attempt to render the *current* scene without a replay call is a cheap, safe optimization; (c) `SCENARIO_SCHEMA_1_1_0_SPEC.md` §19.2 already mandates this envelope shape normatively — this design does not deviate from an already-ratified spec section.
- **The tradeoff, stated explicitly:** every cached field is **re-verified against a fresh replay on every resume** (§10) and is **never trusted** if it disagrees — a mismatch is a fail-closed replay error (§8), never a silent repair. The only genuinely authoritative, replay-independent data is the ordered decision triples and the pinned identity columns. This satisfies the task's principle exactly: derived values are cached for performance/CAS, never treated as a second source of truth.

---

## 4. Attempt status contract

Reuse the existing three-state model verbatim — no new status is needed for Engine V2:

| Status | Decisions may be submitted? | Resume allowed? | Results viewable? | Reversible? |
|---|---|---|---|---|
| `in_progress` | Yes | Yes (returns current scene) | No (not yet complete) | → `completed` (terminal decision) or → `abandoned` (explicit abandon) |
| `completed` | No (fails closed: `attempt_not_in_progress`) | Yes (returns completed result, not a further scene) | Yes | No — permanently terminal |
| `abandoned` | No | No — a learner who wants to continue always starts a **new** attempt (the one-active-attempt partial index permits this once the old row is no longer `in_progress`) | Optional (history only; no "continue" affordance) | No — permanently terminal |

No `expired`/`paused`/`archived` state is introduced. An auto-abandon-after-timeout policy, if ever wanted, is an operational job calling the existing `abandon_scenario_attempt_v1` — not a new status.

---

## 5. Decision record contract

Persisted, per accepted decision, exactly the existing `scenario_decisions` columns:

| Field | Source | Authoritative? |
|---|---|---|
| `attempt_id` | Path/context | Yes (identity) |
| `sequence_number` | **Computed by the RPC** as `next_sequence_number` under the row lock — never trusted from the client as an assignment, only checked as an *expectation* | Yes (canonical ordering) |
| `expected_scene_id` | Client-declared expectation, checked against the attempt's actual `current_scene_id` | Yes (canonical, part of the replay triple) |
| `selected_option_id` | Client-submitted stable option id | Yes (canonical, part of the replay triple) |
| `created_at` | `now()` | Audit only — **never** used for ordering (ordering is always by `sequence_number`) |
| `idempotency_key` | Client-generated UUIDv4 | Idempotency scope, not replay input |
| `state_before` / `state_after` | Server-computed envelope (§7) | **Audit/CAS cache only** — see §3 |
| `resulting_scene_id` / `is_terminal` / `terminal_ending_id` | Server-computed | Audit/CAS cache mirroring `state_after`'s own fields, used for the existing idempotent-replay-without-re-reading-the-attempt-row optimization (§6) |

**Decision on the task's explicit list:** evaluation tier, resolved next scene, and corrective entered/skipped are **not** given their own columns. They are already present, in full per-decision detail, inside `DebriefTraceEntry` (Engine V2's own in-memory dataclass) and are recomputed by `build_debrief_trace(run)` whenever needed (§11) — persisting them as separate columns would duplicate data that is (a) cheap to recompute, (b) sensitive (evaluation tiers/debrief seeds must never leak to the client per §20 of the spec — see §14 below), and (c) exactly the kind of "engine-derived judgment" `SCENARIO_ATTEMPT_PERSISTENCE_DESIGN.md` §0 point 4 already declined to let SQL record for Engine V1. A **replay checksum** is not separately stored either — the envelope's own whole-object equality check (§3, §6) already serves that purpose; a redundant separate hash would be one more thing that could silently drift from the data it summarizes.

---

## 6. Concurrency design

**Mechanism (reused verbatim from the already-hardened V68 implementation):**

1. `SELECT ... FOR UPDATE` on the `scenario_attempts` row by `attempt_id` inside `submit_scenario_decision_v1` — serializes every concurrent submission attempt for one attempt.
2. Idempotency-key lookup happens **first**, inside that lock, before any other check (§7) — a safe retry never races a genuinely new decision.
3. Compare-and-swap checks, in order, all inside the same lock: `expected_sequence_number = attempt.next_sequence_number`; `expected_scene_id = attempt.current_scene_id`; `state_before` (the caller's own recomputed envelope) `IS NOT DISTINCT FROM` the attempt's currently stored `serialized_engine_state`.
4. On success: `INSERT` the decision row (guarded, insert-only) and `UPDATE` the attempt row (sequence/scene/envelope/status) **in the same transaction** — including the terminal transition to `completed`, if applicable, in that same statement.
5. `get_scenario_attempt_v1` uses `SELECT ... FOR SHARE` on the combined `(id, owner)` lookup, so a concurrent read can never observe a torn (partially-committed) view across the attempt row and its decisions.
6. `start_or_resume_scenario_attempt_v1` uses `pg_advisory_xact_lock(hashtext(user_email || ':' || scenario_version_id))` before any row may yet exist to lock conventionally, plus `INSERT ... ON CONFLICT (user_email, scenario_version_id) WHERE status = 'in_progress' DO NOTHING` against the partial unique index.

**Guarantees this provides for Engine V2, unchanged from Engine V1:**

- Only one decision can ever advance a given `expected_sequence_number` (row lock + CAS check).
- The losing concurrent request's CAS check fails with a focused `sequence_mismatch`/`scene_mismatch`/`state_before_mismatch` error — it can never silently overwrite the winner.
- `UNIQUE (attempt_id, sequence_number)` makes a sequence gap or duplicate structurally impossible even if application logic had a bug.
- `UNIQUE (attempt_id, idempotency_key)` plus the eight-field binding check (not fingerprint-only — see the existing SIM-PERSIST-04F correction) makes duplicate rows from a retried request impossible.
- The terminal transition (`in_progress → completed`) and the terminal decision's `INSERT` are one atomic transaction — an attempt can never end up `completed` with no matching terminal decision row, or vice versa, so "attempt status cannot be completed twice inconsistently" and "final outcome cannot diverge" both hold by construction.

No new concurrency primitive is introduced for Engine V2 — the recommendation is to reuse this exact mechanism.

---

## 7. Serialization contract (envelope shape)

Per `SCENARIO_SCHEMA_1_1_0_SPEC.md` §19.2, the Engine V2 envelope (used as `p_initial_serialized_state`, and as both `state_before`/`state_after` on every decision) is a plain JSON object:

```json
{
  "simulationId": "cb-sc-001-onboarding-handoff-vslice",
  "version": "0.2.0-vslice",
  "schemaVersion": "1.1.0",
  "canonicalContentSha256": "…64 lowercase hex…",
  "engineVersion": "SCENARIO_ENGINE_V2",
  "currentSceneId": "SC001-C02",
  "state": { "customerConfidence": 72.0 },
  "counters": { "correctiveScenesExperienced": 1 },
  "flags": ["flag-verbal-handoff-only"],
  "decisionHistory": [
    { "sequenceNumber": 1, "sceneId": "SC001-C01", "optionId": "opt-sc001-c01-a" }
  ],
  "routingResolutions": [
    { "sequenceNumber": 1, "nextSceneId": "SC001-C02", "enteredCorrective": false, "skippedCorrective": false }
  ],
  "optionDisplayOrderByScene": { "SC001-C02": ["opt-sc001-c02-b", "opt-sc001-c02-a"] },
  "selectedVariantIdByScene": { "SC001-C02": null },
  "isComplete": false,
  "terminalResult": null
}
```

Rules, all inherited from the general "no raw Python objects" constraint (task §17) and already how the V1 adapter behaves:

- **Never persist** `MappingProxyType`, `frozenset`, bare tuples, dataclass instances, or exception objects — every field above is built by an explicit `serialize_run_snapshot_v2(run: ScenarioRunV2Snapshot) -> dict` function (to be implemented in Slice D, not this task) that converts: `frozenset[str]` → sorted `list[str]` for `flags`; `Mapping[str, tuple[str, ...]]` → `dict[str, list[str]]` for `optionDisplayOrderByScene`; `tuple[DebriefTraceEntry, ...]` → **not included** in the envelope at all (see below); `Mapping[str, float]`/`Mapping[str, int]` → plain `dict`.
- **`attemptId` is deliberately excluded** from the envelope — it is always the attempt row's own `id` column, never duplicated inside the JSON (avoids a second place it could drift).
- **`decisionHistory` is the one field inside the envelope that is genuinely authoritative** (the "learner truth" triples, per spec wording) — everything else in the envelope is a server-computed cache of what replaying that same `decisionHistory` against the pinned content would produce. `decisionHistory`'s growing duplication across every row's `state_after` mirrors the already-accepted Engine V1 precedent (SIM-PERSIST-04B point 4) and is what lets the RPC's whole-envelope CAS check (§6) work without a second replay call from inside SQL.
- **Full per-decision debrief detail is never put in the envelope**: no `evaluationTier`, `debriefSeed`, `stateDelta`, `flagsSet`/`flagsCleared`, `presentedDialogueVariantId`/`nextDialogueVariantId`, or `competencyTags` per decision. These live only in `DebriefTraceEntry`, recomputed on demand from `decisionHistory` via `build_debrief_trace(replay_scenario_run_v2(...))` (§11) — never persisted, matching §14's "no hidden scoring/routing data returned to the client" and minimizing what a database compromise could expose.
- **Terminal result** stored is the minimal completion summary needed for §13/§11 (see below), not the full `ScenarioTerminalResultV2` (which itself embeds all `decisions: tuple[DebriefTraceEntry, ...]`):

```json
{
  "endingId": "ending-verbal-handoff-success",
  "displayScore": 82,
  "engineVersion": "SCENARIO_ENGINE_V2",
  "canonicalContentSha256": "…same 64 hex as attempt's pinned value…"
}
```

  `terminal_result_snapshot` (the separate JSONB column already on `scenario_attempts`) stores exactly this same minimal object — identical to `state_after.terminalResult` (required, per the existing RPC's `terminal_result_mismatch`/`terminal_ending_mismatch` checks, to be exactly equal).

- **Other serialization forms needed:**
  - *Decision input* (client → service): `{ "attemptId": "...", "expectedSequenceNumber": 2, "expectedSceneId": "SC001-C02", "selectedOptionId": "opt-sc001-c02-b", "idempotencyKey": "..." }` — the only five fields, matching `ScenarioDecisionInputV2`'s "no evaluation tier/state/flags/routing field" structural guarantee.
  - *Replay identity* (service-internal, never persisted as its own row): `{ simulation_id, version, schema_version, canonical_content_sha256, engine_version }` — passed directly to the already-implemented `verify_replay_identity_v2(...)`.
  - *Attempt summary* (service → client, for "my attempts" listings): `{ attemptId, status, currentSceneTitle?, startedAt, updatedAt, completedAt?, outcomeTitle? }` — no internal identifiers beyond what the learner needs to resume or review.
  - *Learner-safe current scene*: exactly `LearnerSceneView` (already implemented, already excludes hidden fields — see `TestLearnerSafeViews` in `tests/test_scenario_engine_v2.py`), serialized field-by-field to JSON.
  - *Completed result* (service → client): exactly `LearnerTerminalView` (`outcome_id`, `outcome_title`, `narrative`, `display_score` — already implemented, already the minimum four fields).

---

## 8. Start-attempt flow

```
1. Authenticate the learner -> verified user_email (existing signed-session pattern, unchanged).
2. Resolve the published scenario content for the requested simulationId
   (current published scenario_versions row via scenarios.current_published_version_id).
3. Load + validate schema 1.1.0 (build_scenario_content_v2) -- fails closed on any
   structural/semantic/graph violation; this is unconditional, not skippable.
4. Verify content.canonical_content_sha256 matches the resolved scenario_versions row's
   own stored hash (defense in depth beyond the compiler's own publish-time check).
5. Verify content.required_engine_version == ENGINE_VERSION ("SCENARIO_ENGINE_V2"),
   matching the resolved scenario_versions.engine_version.
6. Check for an existing in_progress attempt for (user_email, scenario_version_id).
   If found -> this is a RESUME (go to section 9), never a fresh start, regardless of
   what the client requested.
7. Otherwise: mint attempt_id = str(uuid.uuid4()).
8. Call start_scenario_run_v2(content, attempt_id=attempt_id) -- pure computation,
   no write yet. This resolves the first scene's dialogue variant and, per SS17,
   its deterministic (or authored) option display order, using the now-known
   attempt_id as seed material.
9. serialize_run_snapshot_v2(run) -> initial envelope (SS7).
10. Call start_or_resume_scenario_attempt_v1(p_attempt_id=attempt_id, ...) (SS2) --
    one RPC call, one transaction. Because of step 6's own pre-check plus the RPC's
    own re-check under its advisory lock, at most one row is ever created for this
    (user_email, scenario_version_id) pair even under a concurrent double-click.
11. Return build_learner_scene_view(run) -- never the full run/envelope.
```

**Initialization state is reconstructed, not separately persisted** beyond the envelope: the RPC stores exactly the envelope Python already computed in step 9 (a plain `INSERT`, no engine logic in SQL) — the "is it persisted or reconstructed" question resolves to "computed by Python, persisted as a cache, always reconstructible by replaying zero decisions" (`replay_scenario_run_v2(content, attempt_id=X, decisions=())` reproduces the identical envelope).

---

## 9. Resume-attempt flow

```
1. Authenticate the learner -> verified user_email.
2. Load the attempt via get_scenario_attempt_v1(user_email, attempt_id) --
   ScenarioAttemptNotFoundError is raised identically for "does not exist" and
   "belongs to someone else" (existing behavior, unchanged, unchanged for V2).
3. Load the PINNED content via attempt.scenario_version_id (never "current"
   published version) -- reject content mismatch: if the pinned scenario_versions
   row cannot be loaded, or its own stored engine_version/canonical_content_sha256
   disagree with the attempt's own pinned copies, fail closed
   (ScenarioLearnerVersionUnavailableError-equivalent for V2).
4. Deserialize attempt.decisions (ordered scenario_decisions rows) into
   ScenarioDecisionInputV2 triples using ONLY (sequence_number, expected_scene_id,
   selected_option_id) -- never state_before/state_after.
5. verify_replay_identity_v2(content, pinned_simulation_id=..., pinned_version=...,
   pinned_schema_version=..., pinned_canonical_content_sha256=...,
   pinned_engine_version=...) -- fails closed on ANY identity mismatch (already
   implemented; this design just specifies exactly when it must be called).
6. run = replay_scenario_run_v2(content, attempt_id=attempt.attempt_id,
   decisions=triples) -- reconstructs state, flags, counters, routing history,
   corrective entries/skips, dialogue variants, option display order, and
   (if the last decision was terminal) the terminal result, entirely fresh.
7. Verify attempt.status matches run.is_complete
   (status == 'completed' iff run.is_complete) and, if complete,
   attempt.terminal_ending_id == run.terminal_result.outcome_id --
   any disagreement is a corrupted-history failure (SS9.1 below), never
   silently repaired.
8. If not complete: return build_learner_scene_view(run).
   If complete: return build_learner_terminal_view(run).
```

### 9.1 Replay failure behavior (fail closed, never silently repaired)

| Condition | Behavior |
|---|---|
| Invalid sequence (gap, not starting at 1, non-monotonic) | `_validate_decision_sequence_v2` (already implemented) raises `ScenarioRunStateV2Error` before any engine state is touched — surfaced as a resume failure, attempt is **not** auto-corrected or auto-truncated |
| Missing decision | Indistinguishable from "invalid sequence" (a gap) — same fail-closed path |
| Duplicate sequence | `_validate_decision_sequence_v2` raises explicitly (`duplicate sequenceNumber`) |
| Scene mismatch (a stored `expected_scene_id` disagrees with where replay actually is) | `replay_scenario_run_v2` raises `ScenarioRunStateV2Error` (`expected current scene ... got replay step for ...`) |
| Option mismatch (a stored `selected_option_id` is not valid for that scene) | `apply_decision_v2` (called internally by replay) raises `ScenarioRunStateV2Error` (`option ... is not valid for scene ...`) |
| Hash mismatch (pinned content hash disagrees with a freshly loaded/rehashed document) | `verify_replay_identity_v2` raises `ScenarioReplayV2Error` naming every mismatched field |
| Outcome mismatch (persisted `terminal_ending_id` disagrees with freshly replayed outcome) | New, explicit resume-layer check (step 7 above) — not currently a single Engine V2 function, must be added as part of the resume **service** in Slice E, not inside the engine itself |
| Corrupted history (any of the above, or an unparseable envelope) | The resume service surfaces a distinct, operator-visible error class (e.g. `ScenarioLearnerStateError`, mirroring the existing Engine-V1-equivalent exception) — the learner sees a generic "progress could not be restored" message; **no code path attempts to reconstruct a best-effort state from a partial/corrupt history** |

Every one of these is a **read-only** failure: resume never writes anything to `scenario_attempts`/`scenario_decisions` on any of these paths. An operator (not this design) decides case-by-case whether an unrecoverable attempt should be manually abandoned.

---

## 10. Submit-decision flow

Request from the client contains **only**: `attemptId`, `expectedSequenceNumber`, `expectedSceneId`, `selectedOptionId`, `idempotencyKey` (§7) — no state, no flags, no evaluation tier, matching `ScenarioDecisionInputV2`'s own structural minimalism and §20 of the spec.

```
1. Authenticate ownership -- verified user_email, matched against the attempt's
   own stored user_email (RPC-side check, unchanged mechanism).
2. Load + verify pinned content identity exactly as resume steps 3/5 (SS9) --
   this MUST happen before the engine is invoked, so a content/engine mismatch
   is caught before any scoring/replay work.
3. Load ordered decisions and replay (SS9 steps 4-6) to reconstruct the CURRENT
   run -- this is the "replay inside the application service" answer to the
   task's SS9 question (see rationale below).
4. Verify the reconstructed run.current_scene_id == request.expectedSceneId and
   run.expected_sequence_number == request.expectedSequenceNumber -- a client-side
   staleness check performed BEFORE calling apply_decision_v2, giving a fast,
   clear "your view is stale, please refresh" response without ever reaching SQL
   for an obviously-doomed request.
5. Apply the submitted decision through Engine V2:
   next_run = apply_decision_v2(run, ScenarioDecisionInputV2(expectedSequenceNumber,
   expectedSceneId, selectedOptionId)) -- the ONLY place option validity, state
   deltas, flag changes, routing, corrective budget, classification, and
   completion are computed. Raises ScenarioRunStateV2Error for an invalid/
   unavailable option -- surfaced as a 4xx-equivalent to the client, no write
   attempted.
6. serialize_run_snapshot_v2(run) -> state_before_envelope (already computed once
   at step 3/replay end -- reuse it, do not recompute) and
   serialize_run_snapshot_v2(next_run) -> state_after_envelope.
7. Compute request_fingerprint (reused, unmodified, from utils.scenario_persistence
   -- see SS16) over the same ten fields the existing mechanism already covers.
8. Call submit_scenario_decision_v1(...) -- ONE RPC call, one transaction. Inside
   the RPC (existing, unmodified logic): lock the attempt row FOR UPDATE; check
   idempotency-key binding (SS7 below) first; verify status == in_progress;
   verify expected_sequence_number/expected_scene_id/state_before against the
   attempt's own current authoritative values (a second, DB-side CAS check --
   defense in depth against a stale service-layer replay, e.g. from a second
   application instance that read a slightly older copy of the decisions table);
   insert the scenario_decisions row (guarded, insert-only); update the attempt
   row's sequence/scene/envelope/status/terminal fields (atomically, in the same
   transaction, if terminal).
9. Return build_learner_scene_view(next_run) if not complete, else
   build_learner_terminal_view(next_run).
```

**Where does replay happen — the task's explicit question:** **Inside the application service (Python), not inside the database RPC.** This is the only physically possible answer, since Engine V2 is pure Python with no SQL equivalent (the task explicitly forbids pretending PostgreSQL can execute the engine). The RPC's own SQL-side checks (step 8's CAS) are a **narrower, redundant, defense-in-depth verification** — they never *compute* a transition, they only confirm that the service's already-computed `expectedSequenceNumber`/`expectedSceneId`/`state_before` still match what is currently persisted, exactly like the existing Engine V1 flow. If the application-layer replay (step 3) and the RPC's own persisted state disagree (e.g., a concurrent decision from another request already advanced the attempt between step 3 and step 8), the RPC's CAS check fails with a focused `sequence_mismatch`/`scene_mismatch`/`state_before_mismatch` — the service then re-fetches, re-replays, and either informs the client of the conflict (their view was stale) or — for the exact retry case (§13) — recognizes it as a safe idempotent replay via the idempotency-key path, which is checked **before** any of those CAS checks (§6, step 2).

---

## 11. Completion semantics

- **Terminal result comes only from Engine V2**: `apply_decision_v2` sets `run.is_complete = True` and `run.terminal_result` internally via `classify_outcome(...)` — no other code path ever decides an ending.
- **Final outcome persisted exactly once**: the terminal `submit_scenario_decision_v1` call is the only write that can ever set `scenario_attempts.status = 'completed'`, and it does so in the same transaction as the terminal decision's own `INSERT` (§6) — there is no separate "complete" RPC call to forget or race.
- **No later decisions accepted**: enforced by the existing `attempt_not_in_progress` check (status ≠ `in_progress` rejects any further `submit_scenario_decision_v1` call).
- **Repeated completion request returns the same result**: the existing idempotency-key mechanism (§6, §13) already guarantees this for the exact terminal submission; a *separate* "get my completed result" read (`get_scenario_attempt_v1` or a resume call) always returns the same persisted `terminal_ending_id`/`terminal_result_snapshot`, since a `completed` attempt is permanently immutable (existing trigger).
- **Persisted final outcome must match replay**: enforced at resume time (§9, step 7) — a completed attempt is always re-verified against a fresh replay before its result is trusted for display; a mismatch is a fail-closed corrupted-history error, never silently trusted.
- **Display score and debrief trace — recommendation: recompute, do not cache the full trace.** `display_score` (a single `int`) is cheap enough to include in `terminal_result_snapshot` (§7) purely for fast "my results" listings without a replay; the **full** `DebriefTraceEntry` history (evaluation tiers, debrief seeds, state deltas, dialogue variants — everything §14 says must never leak prematurely) is **never** persisted and is always recomputed on demand via `build_debrief_trace(replay_scenario_run_v2(...))` when a learner or instructor actually opens the debrief view. This keeps the sensitive, larger payload out of the database entirely, consistent with §3's cache-vs-authoritative boundary and with the task's "never treat derived Engine V2 state as authoritative" instruction taken to its natural conclusion for the highest-sensitivity derived data.

---

## 12. Content versioning

| Concern | Design |
|---|---|
| `simulationId` | Stable identifier across all versions of one scenario; used to resolve "current published version" for a **new** attempt only |
| Content version (`content.version`) | Immutable per `scenario_versions` row; inside the envelope (`version`), compared on every replay |
| Schema version | `content.schema_version` (e.g. `"1.1.0"`); inside the envelope (`schemaVersion`), compared on every replay — this is new relative to Engine V1's envelope, per spec §19.2, and requires no new column (opaque JSON) |
| Engine version | `scenario_attempts.engine_version` column (existing), pinned at creation, dispatch key for V1 vs. V2 (§15) |
| Canonical content hash | `scenario_attempts.scenario_content_sha256` column (existing) **and** inside the envelope (`canonicalContentSha256`) — doubly pinned, matching the existing Engine V1 convention exactly |
| Publication status | A **new** attempt only ever starts against `scenarios.current_published_version_id`; a **resumed** attempt always uses its own pinned `scenario_version_id`, regardless of whether the scenario has since been republished |
| Later content revisions | Never affect an existing attempt — its `scenario_version_id` FK is immutable; a new published version only affects **future** new-attempt starts |
| Deleted/retired content | Out of scope for V1 of this design (no soft-delete/retire concept exists yet on `scenario_versions`); `ON DELETE RESTRICT` on every relevant FK makes an attempt-breaking hard delete structurally impossible today |
| Backward compatibility | A learner **always** resumes against the exact content they started (immutable FK + `verify_replay_identity_v2` fail-closed check) — this is the single strongest guarantee this design makes, and it is already fully supported by existing, unmodified schema |

---

## 13. Idempotency design

Reuse the existing mechanism (§6, `utils/scenario_persistence.py`) exactly, via a new V2-facing adapter (§16):

| Case | Behavior |
|---|---|
| Same attempt, same sequence, same scene, same option, same idempotency key (genuine retry, e.g. browser network timeout) | RPC finds the existing `(attempt_id, idempotency_key)` row; its stored eight fields (sequence, scene, option, `state_before`, `state_after`, `resulting_scene_id`, `is_terminal`, `terminal_ending_id`) all match the freshly-recomputed request — **safe retry**, returns the original accepted result unchanged, no new row |
| Same key, but any of those eight fields differ (conflicting reuse) | `idempotency_key_conflict:` — fails closed, no row changed |
| Same attempt/sequence/scene, different option, **different** idempotency key | Treated as a genuinely new decision attempt at that sequence number — if the sequence has already advanced (the first one committed), this fails with `sequence_mismatch`/`scene_mismatch`, exactly as any other stale submission would; it is never silently merged with the earlier one |
| Different attempt entirely reusing the same idempotency key value | No conflict — uniqueness is scoped to `(attempt_id, idempotency_key)`, matching the existing `billing_checkout_claims`-style precedent already used elsewhere in this codebase |

**Recommended client contract (mirroring the existing prepare/submit pattern, §1.5):** the idempotency key is generated **once**, client-side, immediately before the first submission attempt of a specific intentional decision, and is reused verbatim on every retry of that *same* decision. A **new** intentional decision (a different `selectedOptionId`, even at the same scene) always gets a **new** key. The response for a safe retry is **the reconstructed current state**, not a bare "duplicate rejected" — matching the task's stated preference ("return the original accepted result... or safely return the reconstructed current state") and the already-implemented behavior (`idempotent_replay: true` in the RPC's own return row).

---

## 14. Security and RLS

No RLS policy is written by this document (per the task's explicit instruction) — the design **inherits** the existing, already-hardened V68 posture unchanged:

- RLS is **enabled** on both tables; **zero policies** — the security boundary is "only `service_role` has any grant at all," not row policies, exactly as `scenarios`/`scenario_versions` already operate.
- Learners access their own attempts only via `get_scenario_attempt_v1(user_email, attempt_id)`, which performs a combined `(id, owner)` lookup under `FOR SHARE` — an unknown id and a wrong-owner id are indistinguishable to the caller (existing, already-closed residual risk from `SCENARIO_ATTEMPT_PERSISTENCE_DESIGN.md` §16).
- Learners **never** insert `scenario_decisions` rows directly, and **never** update `scenario_attempts.status`/terminal fields directly — every mutation is through the four RPCs; `service_role` itself holds no `DELETE` on either table and no `UPDATE` at all on `scenario_decisions` (append-only by grant absence, defense-in-depth by trigger).
- Learner-facing application code calls only the Python adapter's four functions (never `client.table(...)` for these tables) — this design's V2 adapter must preserve that same discipline (§16).
- `service_role` use is already minimized to exactly what each RPC's `SECURITY INVOKER` body needs; no `SECURITY DEFINER` is introduced.
- Admin/support access is **explicit and auditable** by virtue of going through the same RPCs with an operator's own verified identity as `p_user_email` — this design does **not** propose a separate "admin bypass" RPC; an admin who needs to inspect another learner's attempt should do so via direct, audited database access (existing operational practice), not a new client-facing code path.
- **No hidden scores, flags, or internal routing data returned to the client**: `build_learner_scene_view`/`build_learner_terminal_view` (already implemented and already tested to exclude `evaluationTier`, `debriefSeed`, `stateChanges`, `setFlags`, `clearFlags`, `correctiveRoute`, `budgetCondition`, `severeCaps`, `moderateCaps` — see `TestLearnerSafeViews`) are the **only** two functions any resume/submit/completion service response is built from. The full envelope (§7) and full debrief trace (§11) are for server-side/database storage only and must never be serialized directly into an API/UI response.

---

## 15. Engine V1 compatibility

**Recommendation: shared generic persistence with explicit `engine_version` dispatch** (option 1 of the three the task offers), because:

- Both tables are already fully generic (§1.3–1.4) — no additive column is even needed.
- Every published `scenario_versions` row already pins exactly one `engine_version`; a scenario is never simultaneously "V1" and "V2" — dispatch is structural (which version a learner's `scenario_version_id` points to), not a runtime branch inside a shared code path.
- The two RPCs' own existing `engine_version_mismatch` check (already implemented, part of the `_ERROR_PREFIX_MAP` in `utils/scenario_persistence.py`) already prevents an Engine V1 attempt from ever being resumed/advanced against V2 content or vice versa.
- Engine V1's own code, tests, and the existing `utils/scenario_persistence.py`/`utils/scenario_learner_controller.py` modules are **completely untouched** by this design — a new, sibling `utils/scenario_persistence_v2.py` (§16) and a new, sibling learner-controller-equivalent for Engine V2 content are added *alongside* them, never replacing or modifying them.
- This is lower-risk than either alternative: "additive V2 fields on existing tables" is unnecessary (nothing is missing), and "separate V2 persistence model" (new tables) would duplicate the entire hardened concurrency/idempotency/immutability mechanism for no structural benefit, doubling the audit and migration surface for zero new guarantee.

---

## 16. Application persistence adapter (Slice D scope, described here for completeness)

A new module, **`utils/scenario_persistence_v2.py`** (not created by this task), mirrors `utils/scenario_persistence.py`'s structure exactly:

- Reuses, unchanged, by direct import or byte-for-byte copy (a follow-on refactor could extract a shared base — out of scope here): `generate_idempotency_key()`, `compute_request_fingerprint(...)`, all four RPC-calling functions' *shape* (same four RPC names, same parameters plus the one additive `p_attempt_id`), and every strict `_require_*` helper.
- Replaces only `validate_serialized_engine_state(...)` with a V2-specific `validate_serialized_engine_state_v2(...)` whose `REQUIRED_SERIALIZED_STATE_KEYS_V2` is `{simulationId, version, schemaVersion, canonicalContentSha256, engineVersion, currentSceneId, state, counters, flags, decisionHistory, routingResolutions, optionDisplayOrderByScene, selectedVariantIdByScene, isComplete, terminalResult}` (§7) — `schemaVersion`/`counters`/`optionDisplayOrderByScene`/`selectedVariantIdByScene` are the only additions relative to V1's set; `routingResolutions`/`selectedVariantIdByScene` are optional per spec wording and validated as optional-but-well-typed-when-present.
- The two internal consistency-check functions (`_validate_initial_state_consistency`/`_validate_decision_snapshot_consistency`) are reused **unchanged** — they only ever touch the seven SQL-relevant keys (§1.4), which are identical between V1 and V2's envelopes.

---

## 17. Data integrity constraints

| Constraint | Enforced where | Status |
|---|---|---|
| Unique attempt id | `scenario_attempts.id PRIMARY KEY` | Already exists |
| Unique attempt + sequence | `UNIQUE (attempt_id, sequence_number)` | Already exists |
| Unique attempt + idempotency key | `UNIQUE (attempt_id, idempotency_key)` | Already exists |
| Non-null content identity | `scenario_content_sha256 NOT NULL`, `engine_version NOT NULL`, `scenario_version_id NOT NULL` | Already exists |
| Valid status check | `CHECK (status IN ('in_progress','completed','abandoned'))` | Already exists |
| Nonnegative revision | N/A — this design uses `next_sequence_number`/envelope equality as the CAS token, not a separate revision integer (§6); `next_sequence_number >= 1` is implied by `CHECK (sequence_number >= 1)` on decisions plus the "next = count + 1" invariant | Already exists (equivalent) |
| Completed attempts require outcome | `CHECK` linking `status='completed'` to non-null `terminal_ending_id`/`terminal_result_snapshot` | Already exists |
| Active attempts cannot have final outcome | Same `CHECK`, inverse direction | Already exists |
| Sequence values begin at 1 | `CHECK (sequence_number >= 1)` (decisions) + engine-side `_require_strict_int`/`>= 1` check (application layer, defense-in-depth) | Already exists + application-layer mirror |
| Immutable attempt identity fields | `guard_scenario_attempt_mutation_v1` trigger | Already exists |
| **New for this design** | One additive RPC parameter (`p_attempt_id`) — no new `CHECK`/`UNIQUE` needed; the existing `PRIMARY KEY` already prevents a caller-supplied id from colliding with an existing row (insert simply fails, surfaced as a backend error, vanishingly unlikely given UUIDv4) | Proposed, Slice A/B |

**Application-layer only (not enforceable in PostgreSQL):** Engine V2's own content/graph/semantic validation (schema 1.1.0, reachability, formula acyclicity, etc.) — SQL cannot and must not attempt any of this; envelope shape validation (`validate_serialized_engine_state_v2`) — SQL only checks the seven identity/lifecycle keys, not the full shape; §17 option-order algorithm correctness — entirely a Python/engine concern.

---

## 18. Failure recovery

| Scenario | Behavior |
|---|---|
| Request times out **after** commit | Client retries with the same idempotency key → safe replay (§13), returns the already-committed result |
| Request times out **before** commit | No row was ever written; a retry with the same idempotency key proceeds as a normal new submission (idempotency-key lookup finds nothing) |
| Application crashes during replay (before calling the RPC) | No write attempted; safe to retry the entire submit flow from scratch |
| Application crashes **after** the RPC's decision insert but before the response is read by the caller | The RPC's insert + attempt-update (and, if terminal, completion) already committed atomically (§6) — a subsequent retry with the same idempotency key returns that same committed result; no reconciliation RPC is needed because, unlike the Engine V1 design's original (now-superseded) two-RPC completion proposal, this design's single-transaction terminal completion (already the implemented V68 behavior) removes the window a reconciliation RPC would exist to repair |
| Database transaction rolls back | Nothing was persisted; identical to "crashes before commit" from the client's perspective — safe retry |
| Content file/row unavailable (e.g., `scenario_versions.content_snapshot` cannot be loaded) | Start/resume fails closed before any engine call; no attempt row is created/advanced |
| Content hash mismatch | Fails closed at load time (§8 step 4, §9 step 3) — no engine call, no write |
| Replay failure | §9.1 — fails closed, read-only, no repair attempted |
| Duplicate retry | §13 — safe idempotent replay |
| Partial completion attempt (terminal decision submitted, but the attempt somehow still shows `in_progress`) | Should not occur given the single-transaction design (§6, §11); if ever observed in production despite that guarantee, it indicates a genuine bug requiring a manual, audited, one-off correction — this design deliberately does **not** propose an automatic reconciliation RPC for a failure mode the atomic-transaction design is specifically built to prevent, mirroring `SCENARIO_ATTEMPT_PERSISTENCE_DESIGN.md`'s own explicit V1 decision (§0 point 1) not to add one speculatively |

---

## 19. Observability

Minimal structured events (no hidden answer quality, no free-text learner content):

| Event | Fields (all non-sensitive identifiers/enums only) |
|---|---|
| `scenario_v2_attempt_started` | `attemptId`, `simulationId`, `scenarioVersionId`, `engineVersion` |
| `scenario_v2_decision_accepted` | `attemptId`, `sequenceNumber`, `sceneId` (not the learner's narrative reasoning — just the id), `isTerminal` |
| `scenario_v2_decision_duplicate_retry` | `attemptId`, `sequenceNumber`, `idempotencyKey` (safe to log — a random UUID, not learner content) |
| `scenario_v2_decision_conflict_rejected` | `attemptId`, `conflictType` (`sequence_mismatch` \| `scene_mismatch` \| `state_before_mismatch` \| `idempotency_key_conflict`) |
| `scenario_v2_resume_succeeded` | `attemptId`, `resumedAtSceneId`, `decisionCount` |
| `scenario_v2_replay_mismatch` | `attemptId`, `mismatchFields` (e.g. `["canonicalContentSha256"]`) — **never** the actual content or state values |
| `scenario_v2_attempt_completed` | `attemptId`, `outcomeId`, `displayScore` |
| `scenario_v2_content_mismatch` | `attemptId` (if known), `expectedEngineVersion`, `actualEngineVersion` (or equivalent for hash/version) |

Explicitly **not logged**: evaluation tiers, debrief seeds, state deltas, flag names/values, corrective routing detail, or any free-text scenario content — all of this is exactly what §14/§3 already keep out of both the client response and any cache the client could inspect; logging it would reopen the same leak through an operational side channel.

---

## 20. Migration plan (proposed, not created)

| Item | Scope |
|---|---|
| Tables affected | **None** — zero new tables, zero altered columns |
| Indexes affected | **None** |
| Unique constraints affected | **None** |
| Status checks affected | **None** |
| RLS changes | **None** — inherits existing zero-policy, service-role-only posture unchanged |
| RPCs/functions affected | **One**: `CREATE OR REPLACE FUNCTION start_or_resume_scenario_attempt_v1(...)` adding `p_attempt_id uuid DEFAULT NULL` (§2) — additive, backward-compatible, same function name/return shape, only the new optional parameter and the internal id-selection branch change |
| Rollback strategy | `CREATE OR REPLACE FUNCTION` with the prior signature/body (drop the new parameter and its branch) — no data migration, no table change, so rollback is a pure code revert of the function definition; no existing row is ever affected by adding an optional parameter |
| Compatibility with current data | Full — every existing Engine V1 attempt/decision row is untouched; every existing Engine V1 caller (which never passes `p_attempt_id`) is unaffected because the parameter defaults to `NULL`, preserving today's internal-id-generation code path exactly |

This is, by a wide margin, the smallest possible migration that resolves the one genuine gap (§2.1) between the existing V68 foundation and what Engine V2's §17 option-order algorithm needs.

---

## 21. Implementation slices (isolated, ordered)

| Slice | Scope | Risk level |
|---|---|---|
| **A** | Persistence contract + migration draft: write the actual `CREATE OR REPLACE FUNCTION` diff for §2's additive parameter; write `serialize_run_snapshot_v2`/`replay_serialized_run_v2`-equivalent function *signatures* (not yet implemented) as a contract for Slice D | Low (design/paper only) |
| **B** | Migration/RLS/RPC independent review of Slice A's draft — mirrors the existing SIM-PERSIST-04C/04E/04F review discipline; must explicitly re-verify the additive parameter cannot regress any existing Engine V1 verification case | Low-medium (review only, no live DB) |
| **C** | Disposable database migration validation: apply Slice A's migration to a throwaway/local Postgres instance; run the existing V68 verification script unmodified (must still pass 100%) plus new cases for `p_attempt_id` supplied/omitted | Medium (touches a real, disposable database) |
| **D** | Application persistence adapter: implement `utils/scenario_persistence_v2.py` (§16) and `serialize_run_snapshot_v2`/`deserialize_decision_history_v2`/`replay_serialized_run_v2`-equivalent functions inside or alongside `utils/scenario_engine_v2.py` | Low-medium (pure Python, engine already hardened + reviewed) |
| **E** | Start/resume service: the Engine-V2-facing equivalent of `start_or_resume_ba201_attempt`/resume logic (§8, §9) | Low-medium |
| **F** | Idempotent submission service: the Engine-V2-facing equivalent of `prepare_ba201_decision`/`submit_prepared_ba201_decision` (§10, §13) | Medium (the highest-stakes correctness surface) |
| **G** | Completion service: terminal-result retrieval/display wiring (§11) | Low |
| **H** | End-to-end disposable database smoke: a real (throwaway) Postgres instance, start → N decisions → terminal → resume-after-crash simulation → idempotent-retry simulation → concurrent-tab simulation, using Engine V2 fixtures already in `tests/fixtures/scenario_engine_v2_vslice_1_1_0.json` | Medium-high (closest to production behavior) |
| **I** | Focused review and local milestone commit — mirrors the just-completed `SIM-ENGINE-V2-REVIEW-02`/`SIM-ENGINE-V2-COMMIT-01` pattern for this new persistence layer | Low |

**Recommended exact next implementation task: Slice A** — draft the additive RPC parameter change and the exact Python serialization function signatures this document specifies, as a standalone, reviewable unit, before any live database is touched (Slice C) or any application service code is written (Slices D–G). This keeps the one piece of genuinely new SQL isolated and independently reviewable, exactly as the task's own ordering requires ("keep high-risk database/security work isolated from routine Python service work").

---

## 22. Stop conditions considered

None of the task's stop conditions were triggered:

- Engine V2 and the resume/replay contract do not disagree — `replay_scenario_run_v2`/`verify_replay_identity_v2`/`build_debrief_trace` already implement exactly the guarantees this design depends on.
- No persistence requirement in this task conflicts with `SCENARIO_SCHEMA_1_1_0_SPEC.md` §17 (option order) or §19 (persistence/replay boundary) — this design is a direct, non-contradictory elaboration of §19.
- The one genuine gap found (§2.1, attempt-identity ordering) has a small, additive, non-breaking resolution rather than requiring a redesign or a "cannot proceed" stop.

---

## 23. Remaining uncertainties (flagged, not resolved here)

1. Exact wording of any new RPC exception path for `p_attempt_id` collision (astronomically unlikely with UUIDv4, but must still fail with a focused, distinguishable error, not a generic `500`) — a naming decision for Slice A, not fixed here, mirroring the existing design doc's own §15.2 precedent of deferring exact exception-label wording to implementation time.
2. Whether a future non-Python runtime will ever need to read/verify this envelope directly (rather than only via `utils/scenario_persistence_v2.py`) — if so, the envelope's JSON Schema should be formally published alongside `SCENARIO_SCHEMA_1_1_0_SPEC.md` §19.2; not required for the current single-Python-service architecture.
3. Whether `routingResolutions`/`selectedVariantIdByScene` (optional per spec §19.2) should be made mandatory once Slice D is implemented, for simpler validation — a Slice D implementation-time choice, not a persistence-architecture question.

---

## 24. Files modified / created

**Modified:** none.
**Created:** this document only — `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_RESUME_DESIGN.md`.

No database migration, RPC, application runtime code, or UI code was created or modified while producing this document. No file was staged, committed, pushed, or deployed.
